"""Credit card statements (faturas): reading, preview and import.

Same flow as the CSV import: upload, preview kept in the session, then confirm.

Rules applied on top of the usual ones (closed months, audit log, category rules):
- the money goes to a credit card account (type "credit"), which may go negative;
- invoice payments are not expenses: they are skipped, or registered as a transfer
  from the account that paid them;
- balances carried over from the previous invoice are skipped (already counted);
- refunds (estornos) cancel the purchases they refer to in the same file; whatever
  is left becomes income in the "Estornos" category;
- category: the user's rules decide; then fees go to "Juros e tarifas"; then the AI
  suggestion; then built-in keywords; finally "Outros". Installments keep the category of
  what was bought and get the tag "parcelado". Every choice can be changed in the preview;
- an installment k/N with k < N creates a monthly recurring item for the remaining
  installments (k+1 to N) that ends on the date of the last one. Later invoices fill
  those scheduled slots instead of duplicating them.
"""
import csv
import datetime
import io
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils import timezone

from . import invoice_ai, services
from .csvio import _decode, parse_amount, parse_date
from .models import (
    CARD_FUTURE_DAYS, KIND_PLURAL, MAX_AMOUNT, MIN_AMOUNT, MIN_DATE, Account, CardInvoice, Category, CategoryRule, RecurringTransaction,
    Transaction, Transfer,
)

MAX_LINES = 500
MAX_CSV_BYTES = 512 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
# An installment dated this long before the invoice was bought months ago: it is
# booked on the invoice date so it lands in the month it is actually charged.
OLD_DATE_DAYS = 45
ZERO = Decimal("0.00")

INSTALLMENT_CATEGORY = "Parcelas"
FEE_CATEGORY = "Juros e tarifas"
SUBSCRIPTION_CATEGORY = "Assinaturas"
REFUND_CATEGORY = "Estornos e descontos"
FALLBACK_CATEGORY = "Outros"

# Categories this module may create: name -> (kind, colour, icon).
CARD_CATEGORIES = {
    INSTALLMENT_CATEGORY: ("expense", "plum", "credit-card"),
    FEE_CATEGORY: ("expense", "red", "receipt"),
    SUBSCRIPTION_CATEGORY: ("expense", "blue", "tv"),
    REFUND_CATEGORY: ("income", "green", "hand-coins"),
}

# Built-in hints used when no rule matches. Checked in order; each keyword must start a
# word of the description (accents and case ignored).
KEYWORDS = (
    (FEE_CATEGORY, ("iof", "juros", "multa", "anuidade", "tarifa", "encargo")),
    ("Restaurantes", ("ifood", "uber eats", "rappi", "restaurante", "lanchonete", "pizzaria", "padaria", "burger",
                      "mcdonald", "subway", "churrascaria", "sushi", "cafeteria", "bar e ")),
    (SUBSCRIPTION_CATEGORY, ("spotify", "netflix", "amazonprime", "amazon prime", "prime video", "disney", "hbo",
                             "youtube", "deezer", "globoplay", "paramount", "crunchyroll", "apple.com", "icloud",
                             "google", "microsoft", "xbox", "playstation", "chatgpt", "openai")),
    ("Compras", ("amazon", "shopee", "mercado livre", "mercadolivre", "magalu", "magazine luiza", "shein",
                 "aliexpress", "americanas", "casas bahia", "tiktok", "renner", "riachuelo", "zara", "centauro",
                 "netshoes", "kabum", "lojas")),
    ("Mercado", ("supermerc", "supermarket", "mercado", "carrefour", "muffato", "atacad", "assai", "pao de acucar",
                 "hortifruti", "acougue", "sacolao", "emporio", "super ")),
    ("Transporte", ("uber", "99app", "99 pop", "cabify", "posto", "combust", "ipiranga", "estacion", "pedagio",
                    "sem parar", "veloe", "conectcar", "metro", "onibus")),
    ("Saúde", ("farmac", "drogari", "droga ", "raia", "pague menos", "panvel", "hospital", "clinica", "laborat",
               "odonto", "unimed", "wellhub", "gympass", "smart fit", "smartfit")),
    ("Contas da casa", ("copel", "sanepar", "enel", "cemig", "sabesp", "energia", "internet", "telefonica", "claro",
                        "vivo")),
)

PAYMENT_WORDS = ("pagamento recebido", "pagamento efetuado", "pagamento da fatura", "pagamento de fatura",
                 "pagamento fatura", "pagto fatura")
PREVIOUS_WORDS = ("valor pendente do mes anterior", "saldo anterior", "fatura anterior", "saldo em atraso",
                  "saldo financiado", "saldo restante")
FEE_RE = re.compile(r"\b(iof|juros|multa|anuidade|tarifa|encargos?)\b")
INSTALLMENT_RE = re.compile(r"(?:\s*-\s*|\s+)?(?:parcela\s*)?(\d{1,2})\s*/\s*(\d{1,2})\s*$", re.IGNORECASE)
REFUND_PREFIX_RE = re.compile(r"^(estorno|credito|reembolso|devolucao)( de| do| da)?\s*")
PLAN_SUFFIX_RE = re.compile(r"\s*\(parcelado em \d+x\)\s*$", re.IGNORECASE)

STATUS_LABELS = {
    "import": "Importar", "duplicate": "Duplicata", "ignore": "Ignorada", "payment": "Pagamento", "error": "Erro",
}
SOURCE_LABELS = {"csv-nubank": "CSV do Nubank", "csv-padrao": "CSV padrão de fatura", "csv-ia": "CSV lido por IA",
                 "pdf-ia": "PDF lido por IA"}


class InvoiceError(Exception):
    """A problem with the file as a whole."""


# --- text helpers ------------------------------------------------------------

def plain(text):
    """Lower case, no accents, single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def match_key(text):
    return re.sub(r"[^a-z0-9]", "", plain(text))


def same_merchant(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    return shorter >= 5 and (a.startswith(b) or b.startswith(a))


def split_installment(text):
    """("Loja X - Parcela 3/10") -> ("Loja X", 3, 10); not an installment -> (text, None, None)."""
    text = " ".join((text or "").split())
    match = INSTALLMENT_RE.search(text)
    if match:
        k, n = int(match.group(1)), int(match.group(2))
        base = text[:match.start()].rstrip(" -")
        if 1 <= k <= n and n >= 2 and base:
            return base, k, n
        if k == n == 1 and base:
            return base, None, None
    return text, None, None


def guess_type(description, amount):
    text = plain(description)
    if amount < 0 and (text.startswith("pagamento") or any(w in text for w in PAYMENT_WORDS)):
        return "payment"
    if amount > 0 and any(w in text for w in PREVIOUS_WORDS):
        return "previous_balance"
    if amount < 0:
        return "refund"
    if FEE_RE.search(text):
        return "fee"
    return "purchase"


def settle_type(kind, amount, description):
    if kind not in invoice_ai.LINE_TYPES:
        kind = guess_type(description, amount)
    if amount < 0 and kind in ("purchase", "fee", "previous_balance"):
        kind = "refund"
    if amount > 0 and kind in ("refund", "other_credit", "payment"):
        kind = "purchase"
    return kind


def keyword_category(text):
    words = plain(text)
    for name, keywords in KEYWORDS:
        for keyword in keywords:
            if re.search(r"(?<![a-z0-9])" + re.escape(keyword), words):
                return name
    return None


def category_spec(name):
    """(kind, colour, icon) used when the category has to be created."""
    if name in CARD_CATEGORIES:
        return CARD_CATEGORIES[name]
    for default_name, kind, colour, icon in services.DEFAULT_CATEGORIES:
        if default_name.lower() == name.lower():
            return kind, colour, icon
    return "both", "gray", "tag"


def spec_accepts(name, kind):
    return category_spec(name)[0] in ("both", kind)


def plan_key(item):
    """The shop an installment plan belongs to: its payee (the name on the statement), else its name."""
    return match_key(item.payee) if item.payee else match_key(PLAN_SUFFIX_RE.sub("", item.name))


SUFFIX_RE = re.compile(r"\s*\(\d{1,2}/\d{1,2}\)\s*$")


def final_name(row, typed):
    """Description chosen in the preview; installments keep their "(k/n)" suffix."""
    typed = " ".join((typed or "").split())
    if not typed or typed == row["name"]:
        return row["name"][:120], row["base"]
    base = SUFFIX_RE.sub("", typed)[:110] or row["base"]
    return (f"{base} ({row['k']}/{row['n']})" if row["n"] else base[:120]), base


plan_name = services.plan_name


# --- reading -----------------------------------------------------------------

def make_line(day, description, amount, kind=None, installment=None, card="", merchant="", ai_category="", error=""):
    description = " ".join(str(description or "").split()) or "(sem descrição)"
    base, k, n = split_installment(description)
    if installment:
        k, n = installment
    return {
        "date": day, "description": description[:200], "base": base[:120], "amount": amount, "k": k, "n": n,
        "type": settle_type(kind, amount, description), "card": str(card or "")[:8],
        "merchant": str(merchant or "")[:40], "ai_category": str(ai_category or "")[:50], "error": error,
    }


def is_nubank(text):
    first = next((line for line in text.splitlines() if line.strip()), "")
    return [cell.strip().strip('"').lower() for cell in first.split(",")] == ["date", "title", "amount"]


def read_nubank(text):
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    lines = []
    for number, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) < 3:
            raise InvoiceError(f"A linha {number} do CSV está incompleta.")
        try:
            day = datetime.date.fromisoformat(row[0].strip())
        except ValueError:
            raise InvoiceError(f"Data inválida na linha {number}: {row[0]}") from None
        try:
            amount = parse_amount(row[2])
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise InvoiceError(f"Valor inválido na linha {number}: {row[2]}") from None
        if amount != 0:
            lines.append(make_line(day, row[1], amount))
    return {"issuer": "Nubank", "holder": "", "closing_date": None, "due_date": None, "total": None, "source": "csv-nubank",
            "lines": lines}


# --- the app's own statement CSV ("CSV padrão de fatura") ------------------------
# Produced by an AI chat from a PDF (see standard_prompt), or by hand:
#
#   # emissor: Itaú
#   # titular: Fulano de Tal
#   # fechamento: 31/10/2026
#   # vencimento: 07/11/2026
#   # total: 1127,65
#   data;descricao;valor;parcela;total_parcelas;cartao;tipo;categoria
#   29/05/2026;CV PHONE CENTER LT;262,00;5;12;0891;compra;

STANDARD_TYPES = {
    "compra": "purchase", "tarifa": "fee", "juros": "fee", "estorno": "refund", "pagamento": "payment",
    "saldo_anterior": "previous_balance", "saldo anterior": "previous_balance", "credito": "other_credit",
    "desconto": "other_credit",
}


def _standard_parts(text):
    """(metadata dict, data lines). Code fences from a chat answer are dropped."""
    meta, body = {}, []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        if stripped.startswith("#"):
            key, sep, value = stripped[1:].partition(":")
            if sep:
                meta[plain(key)] = value.strip()
            continue
        body.append(line)
    return meta, body


def _standard_header(body):
    if not body:
        return ";", []
    delimiter = ";" if ";" in body[0] else ","
    return delimiter, [plain(cell).strip('"') for cell in body[0].split(delimiter)]


def is_standard(text):
    _, columns = _standard_header(_standard_parts(text)[1])
    return {"data", "descricao", "valor"} <= set(columns)


def _small(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def read_standard(text):
    meta, body = _standard_parts(text)
    delimiter, columns = _standard_header(body)
    lines = []
    for row in csv.reader(io.StringIO("\n".join(body[1:])), delimiter=delimiter):
        if not any(cell.strip() for cell in row):
            continue
        cell = dict(zip(columns, (c.strip() for c in row)))
        day = _iso(cell.get("data"))
        kind = plain(cell.get("tipo", ""))
        lines.append({
            "date": day.isoformat() if day else "", "description": cell.get("descricao", ""),
            "amount": cell.get("valor", ""), "installment_number": _small(cell.get("parcela")),
            "installment_total": _small(cell.get("total_parcelas")), "card_last_digits": cell.get("cartao", ""),
            "merchant_category": "", "suggested_category": cell.get("categoria", ""),
            "line_type": STANDARD_TYPES.get(kind, kind),
        })
    closing, due = _iso(meta.get("fechamento")), _iso(meta.get("vencimento"))
    return from_ai({
        "issuer": meta.get("emissor", ""), "holder": meta.get("titular", ""),
        "closing_date": closing.isoformat() if closing else "", "due_date": due.isoformat() if due else "",
        "invoice_total": meta.get("total", ""), "lines": lines,
    }, "csv-padrao")


def standard_prompt(user):
    """Text the user sends to an AI chat together with the PDF statement."""
    names = ", ".join(ai_categories(user)) or "Outros"
    return (
        "Converta a fatura de cartão de crédito em anexo para o formato abaixo. Responda só com o conteúdo, "
        "sem explicações, dentro de um bloco de código.\n\n"
        "# emissor: <banco do cartão>\n"
        "# titular: <nome do titular>\n"
        "# fechamento: <data de fechamento ou de emissão, DD/MM/AAAA>\n"
        "# vencimento: <DD/MM/AAAA>\n"
        "# total: <total a pagar desta fatura>\n"
        "data;descricao;valor;parcela;total_parcelas;cartao;tipo;categoria\n\n"
        "Regras:\n"
        "1. Uma linha por lançamento, na ordem da fatura, de todos os cartões (titular, adicionais, virtuais), "
        "incluindo anuidade, tarifas, juros e IOF.\n"
        "2. Não inclua o quadro de resumo, as compras parceladas das próximas faturas, limites, simulações e opções "
        "de pagamento. Se a própria lista de lançamentos trouxer o saldo da fatura anterior ou o pagamento dela, "
        "inclua essas linhas. Ignore linhas sem valor.\n"
        "3. data: DD/MM/AAAA. Se a fatura mostrar só dia e mês, descubra o ano pela data de fechamento (um mês "
        "posterior ao do fechamento é do ano anterior).\n"
        "4. descricao: como está na fatura, sem o sufixo da parcela e sem ponto e vírgula.\n"
        "5. valor: com vírgula decimal, sem R$ e sem separador de milhar. Negativo (com - na frente) para "
        "estornos, descontos, créditos e pagamentos.\n"
        "6. parcela e total_parcelas: para 05/12, LT05/12 ou Parcela 1/2, use 5 e 12 (ou 1 e 2). Deixe vazio se "
        "não for parcelado; 1/1 não é parcelado.\n"
        "7. cartao: os 4 últimos dígitos do cartão em que o lançamento aparece, ou vazio.\n"
        "8. tipo: compra, tarifa (IOF, juros, multa, anuidade), estorno, pagamento, saldo_anterior ou credito "
        "(descontos e outros créditos).\n"
        f"9. categoria: escolha uma destas, ou deixe vazio se nenhuma servir: {names}.\n"
    )


def _iso(value):
    """ISO (2026-10-31) or Brazilian (31/10/2026) date, else None."""
    try:
        return parse_date(str(value or "").strip()[:10])
    except (ValueError, TypeError):
        return None


def _money(value):
    if value in (None, ""):
        return None
    text = str(value).strip().replace("−", "-")
    if text.endswith("-"):
        text = "-" + text[:-1]
    try:
        amount = parse_amount(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def from_ai(data, source):
    lines = []
    for item in data.get("lines", []):
        if not isinstance(item, dict):
            continue
        day, amount = _iso(item.get("date")), _money(item.get("amount"))
        error = ""
        if day is None:
            error = "A data deste lançamento não foi reconhecida."
        if amount is None:
            error, amount = "O valor deste lançamento não foi reconhecido.", ZERO
        elif amount == 0:
            continue
        k, n = item.get("installment_number"), item.get("installment_total")
        installment = (k, n) if isinstance(k, int) and isinstance(n, int) and 1 <= k <= n and n >= 2 else None
        lines.append(make_line(day, item.get("description"), services.q2(amount), kind=item.get("line_type"),
                               installment=installment, card=item.get("card_last_digits"),
                               merchant=item.get("merchant_category"), ai_category=item.get("suggested_category"),
                               error=error))
    total = _money(data.get("invoice_total"))
    return {"issuer": str(data.get("issuer") or "").strip()[:60], "holder": str(data.get("holder") or "").strip()[:80],
            "closing_date": _iso(data.get("closing_date")),
            "due_date": _iso(data.get("due_date")), "total": abs(services.q2(total)) if total is not None else None,
            "source": source, "lines": lines}


def ai_categories(user):
    names = [c.name for c in Category.objects.filter(user=user) if c.accepts("expense")]
    known = {n.lower() for n in names}
    names += [n for n, spec in CARD_CATEGORIES.items() if spec[0] != "income" and n.lower() not in known]
    return names


def read_upload(file_name, raw, user):
    """Parse an uploaded statement into an invoice dict with ``lines``."""
    lower = (file_name or "").lower()
    today = timezone.localdate()
    try:
        if lower.endswith(".pdf"):
            if len(raw) > MAX_PDF_BYTES:
                raise InvoiceError("O PDF é grande demais (limite de 10 MB).")
            if not raw[:1024].lstrip().startswith(b"%PDF"):
                raise InvoiceError("Este arquivo não parece ser um PDF.")
            if not invoice_ai.available():
                raise InvoiceError("A leitura automática de PDF está desligada. Mande o PDF para a sua IA com a skill "
                                   "ou com o texto pronto ao lado e cole a resposta no campo \u201cOu cole o CSV\u201d.")
            invoice = from_ai(invoice_ai.read_pdf(raw, ai_categories(user), today), "pdf-ia")
        elif lower.endswith((".csv", ".txt")):
            if len(raw) > MAX_CSV_BYTES:
                raise InvoiceError("O CSV é grande demais (limite de 512 KB).")
            text = _decode(raw)
            if text is None:
                raise InvoiceError("Não foi possível ler o arquivo. Salve-o como CSV UTF-8.")
            if is_nubank(text):
                invoice = read_nubank(text)
            elif is_standard(text):
                invoice = read_standard(text)
            elif invoice_ai.available():
                invoice = from_ai(invoice_ai.read_text(text, ai_categories(user), today), "csv-ia")
            else:
                raise InvoiceError("Formato de CSV não reconhecido. Envie o CSV do Nubank ou o CSV padrão de fatura "
                                   "(veja como gerar ao lado).")
        else:
            raise InvoiceError("Envie a fatura em PDF ou CSV.")
    except invoice_ai.InvoiceAIError as exc:
        raise InvoiceError(str(exc)) from None
    if not invoice["lines"]:
        raise InvoiceError("Nenhum lançamento foi encontrado no arquivo.")
    if len(invoice["lines"]) > MAX_LINES:
        raise InvoiceError(f"Lançamentos demais (limite de {MAX_LINES}).")
    invoice["file_name"] = (file_name or "")[:120]
    return invoice


# --- preview -------------------------------------------------------------------

def find_card_account(user, issuer):
    """The user's credit card account that best matches the issuer, if any."""
    cards = list(Account.objects.filter(user=user, type="credit", archived=False))
    wanted = match_key(issuer)
    if wanted:
        for account in cards:
            if wanted in match_key(account.name):
                return account
    return None


def _row(index, line):
    signed = line["amount"]
    return {
        "i": index, "date": line["date"], "orig_date": line["date"], "description": line["description"],
        "base": line["base"], "name": line["base"], "amount": abs(signed), "signed": signed, "remaining": abs(signed),
        "refunded": ZERO, "k": line["k"], "n": line["n"], "type": line["type"], "card": line["card"],
        "merchant": line["merchant"], "ai_category": line["ai_category"],
        "kind": "expense" if signed > 0 else "income", "status": "import", "reason": "", "errors": [], "notes": [],
        "category": "", "category_source": "", "plan": None, "recurring_id": None, "slot": None,
        **({"status": "error", "errors": [line["error"]]} if line["error"] else {}),
    }


def _net_refunds(rows):
    credits = [r for r in rows if r["status"] == "import" and r["signed"] < 0 and r["type"] in ("refund", "other_credit")]
    charges = [r for r in rows if r["status"] == "import" and r["signed"] > 0]
    for credit in credits:
        key = match_key(REFUND_PREFIX_RE.sub("", plain(credit["base"])).strip("\"' "))
        left = credit["amount"]
        candidates = [c for c in charges if c["remaining"] > 0 and same_merchant(key, match_key(c["base"]))]
        candidates.sort(key=lambda c: (c["remaining"] != left, -c["remaining"]))
        for charge in candidates:
            if left <= 0:
                break
            take = min(left, charge["remaining"])
            charge["remaining"] -= take
            charge["refunded"] += take
            left -= take
        if left == 0:
            credit["status"], credit["reason"] = "ignore", "Estorno compensado com a compra correspondente nesta fatura."
        else:
            if left < credit["amount"]:
                credit["notes"].append(f"Parte do estorno ({credit['amount'] - left:.2f}) foi descontada da compra.")
                credit["amount"] = left
            credit["kind"] = "income"
    for charge in charges:
        if charge["refunded"] > 0:
            if charge["remaining"] == 0:
                charge["status"], charge["reason"] = "ignore", "Compra estornada nesta mesma fatura."
            else:
                charge["notes"].append(f"Valor líquido: estorno de {charge['refunded']:.2f} já descontado.")
                charge["amount"] = charge["remaining"]


def _pick_category(row, rules, existing):
    """First acceptable candidate: (name, source)."""
    kind = row["kind"]
    text = f"{row['description']} {row['base']}".lower()
    candidates = []
    if kind == "income":
        candidates += [(r.category.name, "regra") for r in rules if r.pattern.lower() in text and r.category.accepts(kind)][:1]
        candidates.append((REFUND_CATEGORY, "estorno"))
    else:
        candidates += [(r.category.name, "regra") for r in rules if r.pattern.lower() in text and r.category.accepts(kind)][:1]
        if row["type"] == "fee":
            candidates.append((FEE_CATEGORY, "tarifa ou juros"))
        if row["ai_category"]:
            candidates.append((row["ai_category"], "sugestão da IA"))
        guess = keyword_category(row["description"])
        if guess:
            candidates.append((guess, "palavra-chave"))
    candidates.append((FALLBACK_CATEGORY, "padrão"))
    for name, source in candidates:
        cat = existing.get(name.lower())
        if cat is not None:
            if cat.accepts(kind):
                return cat.name, source
        elif spec_accepts(name, kind) and (name in CARD_CATEGORIES or source != "sugestão da IA" or name == FALLBACK_CATEGORY):
            return name, source
    return FALLBACK_CATEGORY, "padrão"


def build_preview(user, invoice, account=None, date_mode="purchase"):
    """Rows (one per statement line) with what will happen to each, plus the reference date."""
    today = timezone.localdate()
    latest = today + datetime.timedelta(days=CARD_FUTURE_DAYS)
    due = invoice.get("due_date") or None
    dates = [line["date"] for line in invoice["lines"] if line["date"]]
    reference = invoice.get("closing_date") or (max(dates) if dates else today)
    reference = min(reference, today)
    rows = [_row(i, line) for i, line in enumerate(invoice["lines"])]

    for row in rows:
        if row["status"] != "import":
            continue
        if row["type"] == "payment":
            row["status"], row["reason"] = "payment", "Pagamento da fatura: não é despesa."
        elif row["type"] == "previous_balance":
            row["status"], row["reason"] = "ignore", "Saldo da fatura anterior: já foi contado no mês das compras."
    _net_refunds(rows)

    closed = services.locked_months(user)
    for row in rows:
        if row["status"] == "ignore" or row["date"] is None:
            continue
        if row["status"] == "import":
            if date_mode == "due":
                row["date"] = due or reference
            elif date_mode == "invoice":
                row["date"] = reference
            elif row["date"] < reference - datetime.timedelta(days=OLD_DATE_DAYS):
                row["date"] = reference
        if row["date"] != row["orig_date"]:
            row["notes"].append(f"Data da compra: {row['orig_date']:%d/%m/%Y}.")
        if row["status"] == "error":
            continue
        if row["date"] < MIN_DATE or row["date"] > latest:
            row["errors"].append(f"Data fora do intervalo permitido (no cartão, até {CARD_FUTURE_DAYS} dias no futuro).")
        elif services.month_start(row["date"]) in closed:
            row["errors"].append(f"O mês de {row['date']:%m/%Y} está fechado.")
        if row["status"] == "import" and (row["amount"] < MIN_AMOUNT or row["amount"] > MAX_AMOUNT):
            row["errors"].append("Valor fora do intervalo permitido.")
        if row["errors"]:
            row["status"] = "error"

    rules = list(CategoryRule.objects.filter(user=user, active=True).select_related("category"))
    existing = {c.name.lower(): c for c in Category.objects.filter(user=user)}
    for row in rows:
        if row["n"]:
            row["name"] = f"{row['base'][:110]} ({row['k']}/{row['n']})"
        if row["card"]:
            row["notes"].insert(0, f"Cartão final {row['card']}.")
        if row["status"] == "import":
            row["category"], row["category_source"] = _pick_category(row, rules, existing)

    importing = [r for r in rows if r["status"] == "import"]
    if account is not None:
        _match_plans(user, account, importing)
        # Without a due date on the invoice, the card's own due day says when it will be paid.
        from .planning import next_due_date
        due_hint = due or next_due_date(account, reference)
        _mark_duplicates(user, account, [r for r in importing if r["status"] == "import" and not r["recurring_id"]], due_hint)
        _match_recurring(user, account, [r for r in importing if r["status"] == "import" and not r["recurring_id"]
                                         and not r.get("typed") and not r["n"]])
        known_plans = {(plan_key(p), p.installment_total)
                       for p in RecurringTransaction.objects.filter(user=user, account=account, installment_total__isnull=False)}
    else:
        known_plans = set()
    _plan_installments([r for r in rows if r["status"] == "import"], known_plans)
    return rows, reference


def _match_plans(user, account, rows):
    plans = list(RecurringTransaction.objects.filter(user=user, account=account, installment_total__isnull=False))
    claimed = set()
    for row in rows:
        if not row["n"] or row["kind"] != "expense":
            continue
        best = None
        by_hand = []
        for plan in plans:
            if plan.installment_total != row["n"]:
                continue
            if not (plan.installment_first or 1) <= row["k"] <= plan.installment_total:
                continue
            diff = abs(plan.amount - row["amount"])
            if diff > max(Decimal("1.00"), plan.amount * Decimal("0.02")):
                continue
            if plan_key(plan) != match_key(row["base"]):
                # A purchase in installments typed by hand has the name you gave it, not the shop's:
                # same card, same number of installments and the same amount is enough.
                if not plan.payee:
                    by_hand.append((diff, plan))
                continue
            if best is None or diff < best[0]:
                best = (diff, plan)
        if best is None and by_hand:
            best = min(by_hand, key=lambda pair: pair[0])
            row["notes"].append(f"Reconhecida como a compra parcelada lançada à mão “{best[1].name}”.")
            row["learn_payee"] = True
        if best is None:
            continue
        plan = best[1]
        slot = services.add_months(plan.start_date, row["k"] - (plan.installment_first or 1), anchor_day=plan.start_date.day)
        if (plan.pk, slot) in claimed or Transaction.all_objects.filter(recurring=plan, recurring_date=slot).exists():
            row["status"] = "duplicate"
            row["reason"] = "Esta parcela já foi lançada pela recorrência da compra parcelada."
        else:
            claimed.add((plan.pk, slot))
            row["recurring_id"], row["slot"] = plan.pk, slot
            row["notes"].append(f"Ocupa a parcela prevista em {slot:%d/%m/%Y} na recorrência “{plan.name}”.")


def _mark_duplicates(user, account, rows, due=None):
    if not rows:
        return
    low = min(r["date"] for r in rows)
    high = max(r["date"] for r in rows)
    # Imported transactions keep the shop's name as payee, so a renamed one is still recognised.
    existing = Counter(
        (t.date, t.amount, match_key(t.payee or t.name))
        for t in Transaction.objects.filter(user=user, account=account, date__gte=low, date__lte=high).only(
            "date", "amount", "name", "payee")
    )
    for row in rows:
        key = next((k for k in ((row["date"], row["amount"], match_key(row["base"])),
                                (row["date"], row["amount"], match_key(row["name"]))) if existing[k] > 0), None)
        if key is not None:
            existing[key] -= 1
            row["status"], row["reason"] = "duplicate", "Já existe uma transação igual neste cartão."
    _mark_typed_by_hand(user, account, [r for r in rows if r["status"] == "import"], due)


TYPED_DAYS = 7      # a purchase typed by hand is dated near the purchase...
DUE_DAYS = 5        # ...or near the invoice due date (quick entry on a card books it there)
RECURRING_DAYS = 10


def close_amount(a, b):
    """Card charges and what was typed by hand may differ by cents (IOF, rounding, exchange)."""
    return abs(a - b) <= max(Decimal("0.10"), max(a, b) * Decimal("0.02"))


def _mark_typed_by_hand(user, account, rows, due=None):
    """Purchases already in this card (typed by hand, quick entry, a recurring item posted...):
    a similar amount dated near the purchase or near the due date. They are left unticked (not
    blocked): if it is another purchase, tick it."""
    if not rows:
        return
    dates = [d for r in rows for d in (r["date"], r["orig_date"]) if d] + ([due] if due else [])
    low = min(dates) - datetime.timedelta(days=TYPED_DAYS)
    high = max(dates) + datetime.timedelta(days=TYPED_DAYS)
    typed = list(Transaction.objects.filter(user=user, account=account, date__gte=low, date__lte=high)
                 .exclude(tags__name__startswith="Fatura ").only("id", "name", "amount", "date", "kind", "purchase_date"))
    used = set()
    for row in rows:
        when = [d for d in (row["orig_date"], row["date"]) if d]
        best = None
        for tx in typed:
            if tx.pk in used or tx.kind != row["kind"] or not close_amount(tx.amount, row["amount"]):
                continue
            gap = min(abs((t - d).days) for t in (tx.date, tx.purchase_date) if t for d in when)
            near = gap <= TYPED_DAYS or (due is not None and abs((tx.date - due).days) <= DUE_DAYS)
            if not near:
                continue
            score = (abs(tx.amount - row["amount"]), gap)
            if best is None or score < best[0]:
                best = (score, tx)
        if best is None:
            continue
        tx = best[1]
        used.add(tx.pk)
        row["typed"] = {"name": tx.name, "date": tx.date, "amount": tx.amount}
        row["notes"].insert(0, f"Parece já lançada: “{tx.name}” em {tx.date:%d/%m/%Y} ({tx.amount:.2f}). "
                               "Ficou desmarcada; marque se for outra compra.")


def _match_recurring(user, account, rows):
    """A recurring item of this card not posted yet (a subscription, say) that is on the invoice:
    the invoice line takes its place, so the item is not posted again on its date."""
    items = list(RecurringTransaction.objects.filter(user=user, account=account, active=True,
                                                     installment_total__isnull=True))
    if not items or not rows:
        return
    claimed = set()
    margin = datetime.timedelta(days=RECURRING_DAYS)
    for row in rows:
        when = row["orig_date"] or row["date"]
        for item in items:
            if item.kind != row["kind"] or not close_amount(item.amount, row["amount"]):
                continue
            slot = services.first_due_on_or_after(item, max(item.next_due, when - margin))
            if abs((slot - when).days) > RECURRING_DAYS or (item.end_date and slot > item.end_date):
                continue
            if (item.pk, slot) in claimed or Transaction.all_objects.filter(recurring=item, recurring_date=slot).exists():
                continue
            claimed.add((item.pk, slot))
            row["recurring_id"], row["slot"] = item.pk, slot
            row["notes"].append(f"É a recorrência “{item.name}” de {slot:%d/%m/%Y}: entra pela fatura e não será lançada de novo.")
            break


def _plan_installments(rows, known_plans):
    groups = defaultdict(list)
    for row in rows:
        if row["n"] and row["kind"] == "expense":
            groups[(match_key(row["base"]), row["n"])].append(row)
    for key, members in groups.items():
        if key in known_plans or any(r["recurring_id"] for r in members):
            continue
        last = max(members, key=lambda r: r["k"])
        if last["k"] >= last["n"]:
            continue
        remaining = last["n"] - last["k"]
        last["plan"] = {
            "name": plan_name(last["base"], last["n"]), "amount": last["amount"],
            "start": services.add_months(last["date"], 1), "end": services.add_months(last["date"], remaining),
            "first": last["k"] + 1, "total": last["n"], "remaining": remaining,
        }


def summarise(rows, invoice):
    # What will be imported as the page opens: purchases that look already registered start unticked.
    importing = [r for r in rows if r["status"] == "import" and not r.get("typed")]
    expense = sum((r["amount"] for r in importing if r["kind"] == "expense"), ZERO)
    income = sum((r["amount"] for r in importing if r["kind"] == "income"), ZERO)
    by_category = defaultdict(lambda: ZERO)
    by_card = defaultdict(lambda: ZERO)
    for r in importing:
        if r["kind"] == "expense":
            by_category[r["category"]] += r["amount"]
            by_card[r["card"] or "-"] += r["amount"]
    plans = [r["plan"] for r in importing if r["plan"]]
    # Statements differ: some list the carried balance and the payment among the lines (then every
    # line adds up to the total), others only list the payment (then the new charges do).
    lines_sum = sum((r["signed"] for r in rows), ZERO)
    charges_sum = sum((r["signed"] for r in rows if r["type"] not in ("payment", "previous_balance")), ZERO)
    total = invoice.get("total")
    if total is not None and abs(charges_sum - total) < abs(lines_sum - total):
        lines_sum = charges_sum
    return {
        "expense": expense, "income": income, "net": expense - income,
        "by_category": sorted(by_category.items(), key=lambda kv: -kv[1]),
        "by_card": sorted(by_card.items()) if len(by_card) > 1 else [],
        "counts": Counter(r["status"] for r in rows), "ready": len(importing),
        "typed": sum(1 for r in rows if r.get("typed") and r["status"] == "import"),
        "linked": sum(1 for r in rows if r["status"] == "import" and r["recurring_id"] and not r["n"]),
        "plans": len(plans), "plans_future": sum((p["amount"] * p["remaining"] for p in plans), ZERO),
        "lines_sum": lines_sum, "total": total,
        "total_ok": total is None or abs(lines_sum - total) < Decimal("0.05"),
        "payments": sum((r["amount"] for r in rows if r["status"] == "payment"), ZERO),
        "cards_seen": sorted({r["card"] for r in rows if r["card"]}),
    }


# --- session storage ------------------------------------------------------------

DATE_KEYS = {"date", "orig_date", "slot", "start", "end", "reference_date", "closing_date", "due_date"}
DECIMAL_KEYS = {"amount", "signed", "remaining", "refunded", "total"}


def to_session(value):
    if isinstance(value, dict):
        return {k: to_session(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_session(v) for v in value]
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def from_session(value, key=None):
    if isinstance(value, dict):
        return {k: from_session(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [from_session(v) for v in value]
    if value is not None and key in DATE_KEYS:
        return datetime.date.fromisoformat(value)
    if value is not None and key in DECIMAL_KEYS:
        return Decimal(value)
    return value


# --- import ----------------------------------------------------------------------

def category_options(user, rows):
    """Choices for the preview selects, per kind: [(value, label)]."""
    cats = sorted(Category.objects.filter(user=user).select_related("parent"), key=lambda c: c.full_name.lower())
    known = {c.name.lower() for c in cats}
    new_names = sorted({r["category"] for r in rows if r["status"] == "import" and r["category"].lower() not in known}
                       | {n for n in CARD_CATEGORIES if n.lower() not in known}, key=str.lower)
    options = {}
    for kind in ("expense", "income"):
        options[kind] = [(f"c:{c.pk}", c.full_name) for c in cats if c.accepts(kind)]
        options[kind] += [(f"n:{n}", f"{n} (nova)") for n in new_names if spec_accepts(n, kind)]
    return options


def default_choice(row, user_categories):
    cat = user_categories.get(row["category"].lower())
    return f"c:{cat.pk}" if cat else f"n:{row['category']}"


def resolve_account(user, preview):
    if preview.get("account_id"):
        account = Account.objects.filter(user=user, pk=preview["account_id"]).first()
        if account is None:
            raise ValidationError("O cartão escolhido não existe mais.")
        return account
    name = preview["new_account_name"]
    account = Account.objects.filter(user=user, name__iexact=name).first()
    if account is None:
        return Account.objects.create(user=user, name=name, type="credit")
    if not account.is_credit:
        raise ValidationError(f"Já existe a conta “{account.name}” e ela não é um cartão de crédito. Escolha outro nome.")
    return account


def _resolve_category(user, value, kind, cache):
    if value.startswith("c:"):
        cat = Category.objects.filter(user=user, pk=value[2:]).first() if value[2:].isdigit() else None
    elif value.startswith("n:") and value[2:].strip():
        name = value[2:].strip()[:50]
        cat = cache.get(name.lower()) or Category.objects.filter(user=user, name__iexact=name).first()
        if cat is None:
            spec_kind, colour, icon = category_spec(name)
            cat = Category.objects.create(user=user, name=name, kind=spec_kind, color=colour, icon=icon)
    else:
        cat = None
    if cat is None:
        raise ValidationError("Escolha uma categoria para cada lançamento.")
    if not cat.accepts(kind):
        raise ValidationError(f"A categoria “{cat.name}” não pode ser usada em {KIND_PLURAL[kind]}.")
    cache[cat.name.lower()] = cat
    return cat


def edited_row(row, choice):
    """The row with the date and amount typed in the preview (validated), plan dates following them."""
    label = f"\u201c{row['name']}\u201d"
    day, amount = row["date"], row["amount"]
    if choice.get("date"):
        day = _iso(choice["date"])
        latest = timezone.localdate() + datetime.timedelta(days=CARD_FUTURE_DAYS)
        if day is None or day < MIN_DATE or day > latest:
            raise ValidationError(f"{label}: data inválida (no cartão, até {CARD_FUTURE_DAYS} dias no futuro).")
    if choice.get("amount"):
        amount = _money(choice["amount"])
        if amount is None or amount != services.q2(amount) or not MIN_AMOUNT <= amount <= MAX_AMOUNT:
            raise ValidationError(f"{label}: o valor deve ser positivo, com no máximo 2 casas decimais.")
    if day == row["date"] and amount == row["amount"]:
        return row
    row = {**row, "date": day, "amount": amount}
    if row["plan"]:
        remaining = row["plan"]["remaining"]
        row["plan"] = {**row["plan"], "amount": amount, "start": services.add_months(day, 1),
                       "end": services.add_months(day, remaining)}
    return row


def apply_import(user, preview, choices, payment_account=None):
    """Save the chosen rows. ``choices`` maps row index -> {"include", "category", "rule"}.

    Returns the CardInvoice. Raises ValidationError (nothing is saved) on any problem.
    """
    rows = preview["rows"]
    with services.locked(user):
        account = resolve_account(user, preview)
        if payment_account is not None and payment_account.pk == account.pk:
            raise ValidationError("A conta que pagou a fatura deve ser diferente do cartão.")
        cache = {}
        chosen = []
        for row in rows:
            choice = choices.get(row["i"], {})
            if row["status"] in ("import", "duplicate") and choice.get("include"):
                category = _resolve_category(user, choice.get("category") or "", row["kind"], cache)
                chosen.append((edited_row(row, choice), category, choice.get("rule"), choice))
        payments = [r for r in rows if r["status"] == "payment" and not r["errors"]] if payment_account else []
        if not chosen and not payments:
            raise ValidationError("Nenhum lançamento foi marcado para importar.")
        services.ensure_unlocked(user, *{r["date"] for r, _, _, _ in chosen}, *{r["date"] for r in payments})
        effects = [(account, -r["amount"] if r["kind"] == "expense" else r["amount"]) for r, _, _, _ in chosen]
        effects += [(payment_account, -r["amount"]) for r in payments] + [(account, r["amount"]) for r in payments]
        services.ensure_effects(user, effects)

        reference = preview["reference_date"]
        label = (preview.get("issuer") or account.name)[:15]
        tag_name = f"Fatura {label} {reference:%m/%Y}"
        invoice = CardInvoice.objects.create(
            user=user, account=account, issuer=preview.get("issuer") or "", reference_date=reference,
            closing_date=preview.get("closing_date"), due_date=preview.get("due_date"), total=preview.get("total"),
            source=preview.get("source") or "", file_name=preview.get("file_name") or "",
        )
        plans_by_id = {}
        total = ZERO
        for row, category, make_rule, choice in chosen:
            name, base_name = final_name(row, choice.get("name"))
            typed_note = " ".join((choice.get("note") or "").split())
            note = " ".join(([typed_note] if typed_note else []) + row["notes"])[:1000]
            plan = None
            if row["recurring_id"]:
                plan = plans_by_id.get(row["recurring_id"]) or RecurringTransaction.objects.filter(
                    user=user, pk=row["recurring_id"]).first()
            tx = Transaction(
                user=user, name=name, amount=row["amount"], kind=row["kind"], date=row["date"],
                category=category, account=account, note=note, payee=row["base"][:80],
                recurring=plan, recurring_date=row["slot"] if plan else None,
                purchase_date=row.get("orig_date") or row["date"],
            )
            tx.save()
            services.set_tags(tx, [tag_name] + ([services.INSTALLMENT_TAG] if row["n"] else []))
            services.record(user, tx.pk, "imported", f"Importada da fatura: {tx.name} ({tx.amount:.2f})", None,
                            services.snapshot(tx))
            total += tx.amount if tx.kind == "expense" else -tx.amount
            if plan is not None:
                plans_by_id[plan.pk] = plan
                if row.get("learn_payee") and not plan.payee:
                    plan.payee = row["base"][:80]
                    plan.save(update_fields=["payee"])
                if plan.next_due == row["slot"]:
                    plan.next_due = services.following_date(plan, row["slot"])
                    if plan.end_date and plan.next_due > plan.end_date:
                        plan.active = False
                    plan.save(update_fields=["next_due", "active"])
            if row["plan"]:
                p = row["plan"]
                RecurringTransaction.objects.create(
                    user=user, name=plan_name(base_name, p["total"]), amount=p["amount"], kind="expense",
                    category=category, account=account, payee=row["base"][:80],
                    note=(f"Compra parcelada em {p['total']}x no cartão {account.name}. Parcelas {p['first']} a "
                          f"{p['total']}, criadas pela importação da fatura.")[:1000],
                    frequency=RecurringTransaction.MONTHLY, start_date=p["start"], end_date=p["end"],
                    next_due=p["start"], installment_first=p["first"], installment_total=p["total"],
                )
                invoice.plans_created += 1
            if make_rule:
                pattern = " ".join(row["base"].lower().split())[:60]
                if pattern and not CategoryRule.objects.filter(user=user, pattern__iexact=pattern).exists():
                    CategoryRule.objects.create(user=user, pattern=pattern, category=category)
        for row in payments:
            Transfer.objects.create(user=user, from_account=payment_account, to_account=account, amount=row["amount"],
                                    date=row["date"], note=f"Pagamento da fatura {label}"[:200])
        invoice.lines_imported = len(chosen)
        invoice.amount_imported = total
        invoice.save(update_fields=["lines_imported", "amount_imported", "plans_created"])
    return invoice

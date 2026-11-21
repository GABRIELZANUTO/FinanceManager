"""Quick entry: one line of text becomes a transaction.

    "Bolo 50,00 restaurante conta principal"
     name  amount  category    account (or card)

* Everything before the amount is the name; after it come the category and then the account.
  Where one ends and the other starts is found by trying every split and keeping the best match.
* Categories and accounts are matched by similarity (accents, case and small typos do not
  matter: "restaurante" finds "Restaurantes", "nubank" finds "Cartão Nubank"). Standard library
  only (difflib).
* A category with nothing similar is created. An account must exist.
* On a credit card the date is the due date of the next invoice: that is when it will be paid.
* "ontem"/"hoje" change the date, "receita" or an amount starting with "+" make it income.
"""
import datetime
import re
import unicodedata
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils import timezone

from . import planning, services
from .models import MAX_AMOUNT, MIN_AMOUNT, Account, Category, Transaction

CATEGORY_MIN = 0.78      # below this a category is considered "not found" (a new one is created)
ACCOUNT_MIN = 0.72
NEW_CATEGORY_SCORE = 0.5
SUGGEST_MIN = 0.68       # a new category close to an existing one ("supermercado" ~ Mercado): offer "usar esta"
MISSING_ACCOUNT_PENALTY = -1.0
AMOUNT_RE = re.compile(r"^[+-]?(?:R\$)?\d[\d.,]*$", re.IGNORECASE)
WORDS = {"ontem": "yesterday", "hoje": "today", "receita": "income", "despesa": "expense"}


def plain(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def similarity(typed, name):
    """0..1: how well what was typed matches a name (category or account)."""
    a, b = plain(typed), plain(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    words_a, words_b = a.split(), b.split()
    # every typed word found in the name ("nubank" in "cartao nubank"): very likely the one
    if all(any(w == x or (len(w) >= 3 and x.startswith(w)) for x in words_b) for w in words_a):
        return 0.95 if len(words_a) == len(words_b) else 0.9
    best = SequenceMatcher(None, a, b).ratio()
    # also against the last part of a sub-category ("Casa / Mercado") and word by word
    last = plain(name.split("/")[-1])
    best = max(best, SequenceMatcher(None, a, last).ratio())
    if len(words_a) == 1:
        best = max(best, max(SequenceMatcher(None, a, w).ratio() for w in words_b) * 0.95)
    return best


def best_match(typed, options, key):
    """(option, score) with the highest similarity, or (None, 0)."""
    scored = [(o, similarity(typed, key(o))) for o in options]
    return max(scored, key=lambda pair: pair[1]) if scored else (None, 0.0)


def parse_amount(word):
    """'50,00' / '1.234,56' / '+5000' / 'R$15' -> (Decimal, income?) or None."""
    if not AMOUNT_RE.match(word):
        return None
    income = word.startswith("+")
    text = re.sub(r"^[+-]?(R\$)?", "", word, flags=re.IGNORECASE)
    if "," in text:                       # Brazilian: 1.234,56
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.search(r"\.\d{3}$", text):
        text = text.replace(".", "")      # 1.234 (thousands)
    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return (value, income) if value > 0 else None


def parse(user, text, today=None, category_id=None):
    """Read one line. Returns a dict for the preview (nothing is saved). Raises ValidationError."""
    today = today or timezone.localdate()
    words = (text or "").split()
    if not words:
        raise ValidationError("Digite algo como: Bolo 50,00 restaurante conta principal")
    kind, date_word = "expense", None
    index, amount = None, None
    for i in range(len(words) - 1, -1, -1):          # the last number is the amount
        found = parse_amount(words[i])
        if found:
            index, (amount, income) = i, found
            if income:
                kind = "income"
            break
    if amount is None:
        raise ValidationError("Não achei o valor. Exemplo: Bolo 50,00 restaurante conta principal")
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        raise ValidationError("Valor fora do intervalo permitido.")

    def clean(part):
        nonlocal kind, date_word
        out = []
        for word in part:
            meaning = WORDS.get(plain(word))
            if meaning in ("yesterday", "today"):
                date_word = meaning
            elif meaning in ("income", "expense"):
                kind = meaning
            else:
                out.append(word)
        return out

    before, after = clean(words[:index]), clean(words[index + 1:])
    if not before and after:                          # "50 bolo restaurante": the name comes first
        before, after = after[:1], after[1:]
    name = " ".join(before)
    if not name:
        raise ValidationError("Falta a descrição. Exemplo: Bolo 50,00 restaurante conta principal")

    categories = [c for c in Category.objects.filter(user=user).select_related("parent") if c.accepts(kind)]
    accounts = list(Account.objects.filter(user=user, archived=False))
    category, account, new_category, category_score, account_score = _split(after, categories, accounts)
    suggestion = None
    if new_category:
        near, near_score = best_match(new_category, categories, lambda c: c.full_name)
        if near is not None and near_score >= SUGGEST_MIN:
            suggestion = near
    if category_id:      # the person picked an existing category in the preview ("usar esta")
        chosen = next((c for c in categories if c.pk == category_id), None)
        if chosen is None:
            raise ValidationError("Escolha uma das suas categorias.")
        category, new_category, suggestion = chosen, None, None

    notes = []
    if category is None and new_category is None:
        suggested = services.suggest_category(user, name, kind)
        if suggested:
            category, category_score = suggested, None
            notes.append("Categoria escolhida pelas suas regras.")
    purchase = today - datetime.timedelta(days=1) if date_word == "yesterday" else today
    target = account or services.default_account(user)
    date, purchase_date = planning.card_booking(user, target, purchase)
    if target.is_credit and date != purchase:
        notes.append(f"No cartão: entra na fatura que vence em {date:%d/%m/%Y} (compra em {purchase:%d/%m/%Y}).")
    elif target.is_credit and services.get_profile(user).card_date_mode == "invoice":
        notes.append("Cadastre o dia de vencimento do cartão (em Contas) para a compra ir para o mês da fatura.")
    return {
        "name": (name[:1].upper() + name[1:])[:120], "amount": amount, "kind": kind, "date": date,
        "category": category, "new_category": new_category, "category_score": category_score,
        "suggestion": suggestion,
        "account": target, "account_typed": account is not None, "purchase_date": purchase_date,
        "account_score": account_score, "notes": notes,
    }


def _split(words, categories, accounts):
    """Try every place where the category ends and the account starts; keep the best."""
    best = None
    for k in range(len(words) + 1):
        cat_text, acc_text = " ".join(words[:k]), " ".join(words[k:])
        score, category, new_category, account = 0.0, None, None, None
        cat_score = acc_score = None
        if cat_text:
            match, cat_score = best_match(cat_text, categories, lambda c: c.full_name)
            if match is not None and cat_score >= CATEGORY_MIN:
                category, score = match, cat_score
            else:
                new_category, score, cat_score = cat_text, NEW_CATEGORY_SCORE, None
        if acc_text:
            match, acc_score = best_match(acc_text, accounts, lambda a: a.name)
            if match is not None and acc_score >= ACCOUNT_MIN:
                account, score = match, score + acc_score
            else:
                score += MISSING_ACCOUNT_PENALTY
        candidate = (score, category, new_category, cat_score, account, acc_score)
        if best is None or candidate[0] > best[0]:
            best = candidate
    _, category, new_category, cat_score, account, acc_score = best
    return category, account, new_category, cat_score, acc_score


def save(user, parsed):
    """Create the category if it is new, then the transaction. Returns the transaction."""
    category = parsed["category"]
    if category is None and parsed["new_category"]:
        name = " ".join(parsed["new_category"].split())[:50]
        name = name[:1].upper() + name[1:]
        category = Category.objects.filter(user=user, name__iexact=name).first()
        if category is None:
            category = Category(user=user, name=name, kind=parsed["kind"])
            category.full_clean()
            category.save()
    if category is None:
        raise ValidationError("Escolha uma categoria: escreva-a depois do valor ou use Editar detalhes.")
    tx = Transaction(user=user, name=parsed["name"], amount=parsed["amount"], kind=parsed["kind"],
                     date=parsed["date"], category=category, account=parsed["account"],
                     purchase_date=parsed.get("purchase_date"),
                     note=planning.with_purchase_note("", parsed["date"], parsed.get("purchase_date")))
    tx.full_clean()
    return services.save_transaction(tx, tags=[])

"""CSV export and import (with a preview step)."""
import csv
import datetime
import io
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .models import KIND_PLURAL, MAX_AMOUNT, MIN_AMOUNT, MIN_DATE, Transaction
from .services import q2, suggest_category

HEADER = ["date", "type", "name", "category", "amount", "payee", "account", "tags", "note"]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_ROWS = 1000
MAX_BYTES = 512 * 1024


def safe_cell(value):
    """Neutralise spreadsheet formulas by prefixing risky text with an apostrophe."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def unsafe_cell(value):
    """Reverse safe_cell for values we exported ourselves."""
    if len(value) > 1 and value[0] == "'" and value[1] in "=+-@":
        return value[1:]
    return value


class Echo:
    def write(self, value):
        return value


def export_lines(queryset, bom=True):
    """Yield CSV text chunks for a streaming response."""
    writer = csv.writer(Echo())
    if bom:
        yield "﻿"
    yield writer.writerow(HEADER)
    for tx in queryset.iterator(chunk_size=500):
        yield writer.writerow([
            tx.date.isoformat(), tx.kind, safe_cell(tx.name), safe_cell(tx.category.name),
            f"{tx.amount:.2f}", safe_cell(tx.payee), safe_cell(tx.account.name if tx.account else ""),
            safe_cell("; ".join(t.name for t in tx.tags.all())), safe_cell(tx.note),
        ])


def _decode(raw):
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


class CsvImportError(Exception):
    """A problem with the file as a whole (not a single row)."""


# Portuguese column names accepted on import (the export keeps the English header).
HEADER_ALIASES = {
    "data": "date", "tipo": "type", "descrição": "name", "descricao": "name", "nome": "name", "categoria": "category",
    "valor": "amount", "favorecido": "payee", "conta": "account", "observação": "note", "observacao": "note",
    "nota": "note",
}
KIND_ALIASES = {"receita": "income", "despesa": "expense"}


def parse_date(text):
    """ISO (2025-03-31) or Brazilian (31/03/2025) date; raises ValueError."""
    text = text.strip()
    if "/" in text:
        day, month, year = text.split("/")
        return datetime.date(int(year), int(month), int(day))
    return datetime.date.fromisoformat(text)


def parse_amount(text):
    """1234.56, 1,234.56, 1234,56 or 1.234,56 (optionally with R$); raises InvalidOperation."""
    text = text.replace("R$", "").replace(" ", "").replace(" ", "")
    if "," in text and text.rfind(",") > text.rfind("."):
        text = text.replace(".", "").replace(",", ".")   # Brazilian: dot groups, comma decimals
    else:
        text = text.replace(",", "")
    return Decimal(text)


def parse(raw, user):
    """Parse CSV bytes into a list of row dicts, each with ``errors`` and ``duplicate`` flags."""
    if len(raw) > MAX_BYTES:
        raise CsvImportError("O arquivo é grande demais (limite de 512 KB).")
    text = _decode(raw)
    if text is None:
        raise CsvImportError("Não foi possível ler o arquivo. Salve-o como CSV UTF-8.")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise CsvImportError("O arquivo está vazio.")
    fields = {}
    for f in reader.fieldnames:
        key = (f or "").strip().lower()
        fields.setdefault(HEADER_ALIASES.get(key, key), f)
    missing = [h for h in ("date", "type", "name", "amount") if h not in fields]
    if missing:
        raise CsvImportError("Coluna(s) ausente(s): " + ", ".join(missing) + ". Esperado: " + ", ".join(HEADER) + ".")

    categories = {c.name.lower(): c for c in user.categories.all()}
    accounts = {a.name.lower(): a for a in user.accounts.all()}
    existing = {
        (t.date, t.kind, t.name.lower(), t.category.name.lower(), t.amount)
        for t in Transaction.objects.filter(user=user).select_related("category")
    }
    seen = set()
    rows = []
    for number, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            raise CsvImportError(f"Linhas demais (limite de {MAX_ROWS}).")
        def get(key):
            return unsafe_cell((raw_row.get(fields[key]) or "").strip()) if key in fields else ""

        if not any((v or "").strip() for v in raw_row.values() if isinstance(v, str)):
            continue
        errors = []
        row = {"line": number, "name": get("name"), "category": get("category"), "note": get("note"),
               "kind": KIND_ALIASES.get(get("type").lower(), get("type").lower()), "date": None, "amount": None,
               "payee": get("payee"), "account": get("account"),
               "tags": [t.strip() for t in get("tags").split(";") if t.strip()]}
        try:
            row["date"] = parse_date(get("date"))
            if row["date"] < MIN_DATE or row["date"] > timezone.localdate() + datetime.timedelta(days=1):
                errors.append("Data fora do intervalo permitido.")
        except ValueError:
            errors.append("A data deve estar no formato 31/03/2025 ou 2025-03-31.")
        if row["kind"] not in ("income", "expense"):
            errors.append("O tipo deve ser receita (income) ou despesa (expense).")
        try:
            amount = parse_amount(get("amount"))
            if not amount.is_finite() or amount < MIN_AMOUNT or amount > MAX_AMOUNT or amount != q2(amount):
                raise InvalidOperation
            row["amount"] = q2(amount)
        except InvalidOperation:
            errors.append("O valor deve ser um n\u00famero positivo com no m\u00e1ximo 2 casas decimais.")
        if not row["name"] or len(row["name"]) > 120:
            errors.append("A descri\u00e7\u00e3o \u00e9 obrigat\u00f3ria (m\u00e1ximo de 120 caracteres).")
        if len(row["payee"]) > 80:
            errors.append("Favorecido longo demais (m\u00e1ximo de 80 caracteres).")
        if row["account"] and row["account"].lower() not in accounts:
            errors.append(f"Conta desconhecida \u201c{row['account']}\u201d.")
        if len(row["tags"]) > 10 or any(len(t) > 30 for t in row["tags"]):
            errors.append("Use no m\u00e1ximo 10 tags de at\u00e9 30 caracteres.")
        row["rule_matched"] = False
        if not row["category"] and row["kind"] in ("income", "expense"):
            guess = suggest_category(user, f"{row['name']} {row['payee']}", row["kind"])
            if guess:
                row["category"] = guess.name
                row["rule_matched"] = True
        if not row["category"] or len(row["category"]) > 50:
            errors.append("A categoria é obrigatória (máximo de 50 caracteres), a menos que uma regra combine com a descrição.")
        if len(row["note"]) > 1000:
            errors.append("Observação longa demais.")
        cat = categories.get(row["category"].lower())
        if cat and row["kind"] in ("income", "expense") and not cat.accepts(row["kind"]):
            errors.append(f"A categoria “{cat.name}” não pode ser usada em {KIND_PLURAL[row['kind']]}.")
        row["new_category"] = bool(row["category"]) and cat is None
        row["errors"] = errors
        row["duplicate"] = False
        if not errors:
            key = (row["date"], row["kind"], row["name"].lower(), row["category"].lower(), row["amount"])
            row["duplicate"] = key in existing or key in seen
            seen.add(key)
        rows.append(row)
    if not rows:
        raise CsvImportError("O arquivo não tem linhas de dados.")
    return rows


def serialise(rows):
    return [
        {**r, "date": r["date"].isoformat() if r["date"] else None,
         "amount": str(r["amount"]) if r["amount"] is not None else None}
        for r in rows
    ]


def deserialise(rows):
    return [
        {**r, "date": datetime.date.fromisoformat(r["date"]) if r["date"] else None,
         "amount": Decimal(r["amount"]) if r["amount"] is not None else None}
        for r in rows
    ]

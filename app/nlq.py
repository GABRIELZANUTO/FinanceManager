"""Understand short natural phrases such as "mercado mês passado acima de 500" (or the English
"food last month over 500") as transaction filters."""
import datetime
import re
from decimal import Decimal, InvalidOperation

from .formatting import month_label
from .services import add_months, month_end, month_start

_PT_MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
              "novembro", "dezembro")
_EN_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december")
MONTHS = {}
for _names in (_PT_MONTHS, _EN_MONTHS):
    for _i, _name in enumerate(_names):
        MONTHS[_name] = _i + 1
        MONTHS.setdefault(_name[:3], _i + 1)
MONTHS["marco"] = 3  # without the cedilla
for _ambiguous in ("out", "ago"):  # "eating out", "two days ago": only the full month names count
    MONTHS.pop(_ambiguous, None)

STOP = {
    # português
    "o", "a", "os", "as", "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "com", "para", "pra", "meu",
    "minha", "meus", "minhas", "e", "que", "quanto", "mostrar", "mostre", "me", "um", "uma", "todos", "todas", "eu",
    # english
    "the", "an", "in", "on", "for", "my", "all", "of", "to", "and", "from", "show", "what", "did", "i", "with",
}
NUMBER = r"(\d[\d.,]*)"


def _num(text):
    """500, 1500, 1.500, 1.234,56, 12,50 or 1,234.56."""
    text = text.strip(".,")
    if "," in text and text.rfind(",") > text.rfind("."):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    else:
        text = text.replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _fmt(value):
    return format(value, "f").replace(".", ",")


def parse(query, categories, accounts, tags, today):
    """Return (params, parts). ``params`` are TransactionFilter query parameters, ``parts`` describe them."""
    categories = sorted(categories, key=lambda c: len(c["name"]))  # prefer "Restaurantes" over "Restaurantes / Café"
    text = " " + " ".join(query.lower().split()) + " "
    params, parts = {}, []

    def take(pattern):
        nonlocal text
        m = re.search(pattern, text)
        if m:
            text = text[:m.start()] + " " + text[m.end():]
        return m

    # amounts
    m = take(r"\b(?:entre|between)\s+" + NUMBER + r"\s+(?:e|and)\s+" + NUMBER)
    if m and _num(m.group(1)) is not None and _num(m.group(2)) is not None:
        lo, hi = sorted([_num(m.group(1)), _num(m.group(2))])
        params["min_amount"], params["max_amount"] = str(lo), str(hi)
        parts.append(f"entre {_fmt(lo)} e {_fmt(hi)}")
    m = take(r"\b(?:acima de|mais de|maior(?:es)? que|pelo menos|a partir de|over|above|more than|greater than|at least)\s+" + NUMBER)
    if m and _num(m.group(1)) is not None:
        params["min_amount"] = str(_num(m.group(1)))
        parts.append(f"acima de {_fmt(_num(m.group(1)))}")
    m = take(r"\b(?:abaixo de|menos de|menor(?:es)? que|no máximo|no maximo|até|ate|under|below|less than|at most)\s+" + NUMBER)
    if m and _num(m.group(1)) is not None:
        params["max_amount"] = str(_num(m.group(1)))
        parts.append(f"até {_fmt(_num(m.group(1)))}")

    # dates
    this = month_start(today)
    last_week = (today - datetime.timedelta(days=today.weekday() + 7), today - datetime.timedelta(days=today.weekday() + 1))
    this_week = (today - datetime.timedelta(days=today.weekday()), today)
    last_month = (add_months(this, -1), month_end(add_months(this, -1)))
    last_year = (datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31))
    this_year = (datetime.date(today.year, 1, 1), today)
    yesterday = today - datetime.timedelta(days=1)
    ranges = (
        (r"\b(?:hoje|today)\b", (today, today), "hoje"),
        (r"\b(?:ontem|yesterday)\b", (yesterday, yesterday), "ontem"),
        (r"\b(?:semana passada|última semana|ultima semana|last week)\b", last_week, "semana passada"),
        (r"\b(?:(?:n?esta|n?essa) semana|this week)\b", this_week, "esta semana"),
        (r"\b(?:mês passado|mes passado|último mês|ultimo mes|last month)\b", last_month, "mês passado"),
        (r"\b(?:(?:n?este|n?esse) m[eê]s|this month)\b", (this, today), "este mês"),
        (r"\b(?:ano passado|last year)\b", last_year, "ano passado"),
        (r"\b(?:(?:n?este|n?esse) ano|this year)\b", this_year, "este ano"),
    )
    for pattern, (start, end), label in ranges:
        if take(pattern):
            params["date_from"], params["date_to"] = start.isoformat(), end.isoformat()
            parts.append(label)
            break
    else:
        names = "|".join(sorted(MONTHS, key=len, reverse=True))
        m = take(r"\b(?:(?:em|de|in)\s+)?(" + names + r")(?:\s+(?:de\s+)?(\d{4}))?\b")
        if m:
            month = MONTHS[m.group(1)]
            year = int(m.group(2)) if m.group(2) else (today.year if month <= today.month else today.year - 1)
            first = datetime.date(year, month, 1)
            params["date_from"], params["date_to"] = first.isoformat(), month_end(first).isoformat()
            parts.append(month_label(first))

    # type and notes
    if take(r"\b(?:receitas?|recebi|recebido|ganhei|entradas?|income|earned|received|earnings)\b"):
        params["type"] = "income"
        parts.append("receitas")
    elif take(r"\b(?:despesas?|gastei|gastos?|paguei|saídas?|saidas?|expenses?|spent|spending|paid)\b"):
        params["type"] = "expense"
        parts.append("despesas")
    if take(r"\b(?:com (?:observaç(?:ão|ões)|observac(?:ao|oes)|notas?)|with notes?)\b"):
        params["has_notes"] = "1"
        parts.append("com observação")

    # tags (#tag) , accounts, categories, then free text
    for tag in tags:
        if "tag" not in params and take(r"(?:^|\s)#" + re.escape(tag["name"].lower()) + r"(?=\s)"):
            params["tag"] = str(tag["id"])
            parts.append(f"com a tag {tag['name']}")
    leftovers = [w for w in text.split() if w not in STOP]
    free = []
    for word in leftovers:
        cat = next((c for c in categories if c["name"].lower() == word or c["name"].lower().split(" / ")[-1] == word
                    or (len(word) >= 3 and c["name"].lower().startswith(word))), None)
        acct = next((a for a in accounts if a["name"].lower() == word), None)
        if cat and "category" not in params:
            params["category"] = str(cat["id"])
            parts.append(cat["name"])
        elif acct and "account" not in params:
            params["account"] = str(acct["id"])
            parts.append(acct["name"])
        else:
            free.append(word)
    if free:
        params["q"] = " ".join(free)
        parts.append(f"contendo “{params['q']}”")
    return params, parts

"""Period reports: income statement, categories, trends, net worth, payees, tags, cash flow."""
import datetime
from collections import defaultdict

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth, TruncYear

from . import services
from .csvio import safe_cell
from .models import Account, LedgerEntry, Transaction, Transfer
from .services import ZERO, add_months, month_end, month_start, q2

PRESETS = (
    ("this_month", "Este mês"), ("last_month", "Mês passado"), ("quarter", "Este trimestre"),
    ("year", "Este ano"), ("last_year", "Ano passado"), ("custom", "Período personalizado"),
)
REPORTS = (
    ("income_statement", "Demonstrativo de resultados"), ("categories", "Despesas por categoria"), ("trend", "Tendência"),
    ("net_worth", "Patrimônio"), ("payees", "Principais favorecidos"), ("tags", "Tags"), ("cash_flow", "Fluxo de caixa por conta"),
)
MAX_SPAN_DAYS = 3660


def resolve_period(preset, start, end, today):
    """Return (preset, start, end) for a preset name or an explicit custom range."""
    if preset == "last_month":
        first = add_months(month_start(today), -1)
        return preset, first, month_end(first)
    if preset == "quarter":
        first = datetime.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        return preset, first, month_end(add_months(first, 2))
    if preset == "year":
        return preset, datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)
    if preset == "last_year":
        return preset, datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    if preset == "custom" and start and end:
        if end < start:
            start, end = end, start
        if (end - start).days > MAX_SPAN_DAYS:
            start = end - datetime.timedelta(days=MAX_SPAN_DAYS)
        return "custom", start, end
    return "this_month", month_start(today), month_end(today)


def previous_range(start, end):
    days = (end - start).days + 1
    return start - datetime.timedelta(days=days), start - datetime.timedelta(days=1)


def change(current, previous):
    if not previous:
        return None
    return round(float((current - previous) / previous * 100), 1)


def _totals_by_category(user, kind, start, end):
    rows = (Transaction.objects.filter(user=user, kind=kind, date__gte=start, date__lte=end)
            .values("category_id", "category__name", "category__parent__name").annotate(t=Sum("amount")))
    out = {}
    for r in rows:
        name = f"{r['category__parent__name']} / {r['category__name']}" if r["category__parent__name"] else r["category__name"]
        out[r["category_id"]] = (name, q2(r["t"]))
    return out


def _category_rows(user, kind, start, end, prev_start, prev_end):
    now = _totals_by_category(user, kind, start, end)
    before = _totals_by_category(user, kind, prev_start, prev_end)
    total = sum((v[1] for v in now.values()), ZERO)
    rows = []
    for cid, (name, amount) in sorted(now.items(), key=lambda kv: -kv[1][1]):
        prev = before.get(cid, (name, ZERO))[1]
        rows.append({"name": name, "amount": amount, "share": round(float(amount / total * 100), 1) if total else 0.0,
                     "previous": prev, "change": change(amount, prev)})
    for cid, (name, prev) in before.items():
        if cid not in now:
            rows.append({"name": name, "amount": ZERO, "share": 0.0, "previous": prev, "change": -100.0})
    return rows, total


def build(user, start, end):
    prev_start, prev_end = previous_range(start, end)
    income_rows, income_total = _category_rows(user, "income", start, end, prev_start, prev_end)
    expense_rows, expense_total = _category_rows(user, "expense", start, end, prev_start, prev_end)
    prev_income, prev_expense = services.income_expense(user, prev_start, prev_end)
    net = income_total - expense_total

    statement = {
        "income": income_rows, "expenses": expense_rows,
        "income_total": income_total, "expense_total": expense_total, "net": net,
        "previous": {"income": prev_income, "expense": prev_expense, "net": prev_income - prev_expense},
        "changes": {"income": change(income_total, prev_income), "expense": change(expense_total, prev_expense),
                    "net": change(net, prev_income - prev_expense)},
        "savings_rate": round(float(net / income_total * 100), 1) if income_total else None,
    }

    # trend: monthly, or yearly for very long ranges
    span_months = (end.year - start.year) * 12 + end.month - start.month + 1
    yearly = span_months > 24
    base = Transaction.objects.filter(user=user, date__gte=start, date__lte=end)
    trunc = TruncYear if yearly else TruncMonth
    rows = (base.annotate(p=trunc("date")).values("p")
            .annotate(income=Sum("amount", filter=Q(kind="income")), expense=Sum("amount", filter=Q(kind="expense")))
            .order_by("p"))
    trend = []
    for r in rows:
        i, e = q2(r["income"] or ZERO), q2(r["expense"] or ZERO)
        label = f"{r['p']:%Y}" if yearly else f"{r['p']:%Y-%m}"
        trend.append({"period": label, "income": i, "expense": e, "net": i - e,
                      "savings_rate": round(float((i - e) / i * 100), 1) if i else None})

    # net worth at the end of every month in the range
    worth = []
    first = month_start(start)
    running = services.balance_for(user, before=start)
    # balance at the start of the range's first month boundary is built from monthly nets
    month_net = {r["m"]: q2(r["income"] or ZERO) - q2(r["expense"] or ZERO) for r in
                 base.annotate(m=TruncMonth("date")).values("m")
                 .annotate(income=Sum("amount", filter=Q(kind="income")), expense=Sum("amount", filter=Q(kind="expense")))}
    m = first
    while m <= end and len(worth) < 120:
        running += month_net.get(m, ZERO)
        worth.append({"period": m.strftime("%Y-%m"), "balance": running})
        m = add_months(m, 1)

    payees = [{"name": r["payee"], "amount": q2(r["t"]), "count": r["c"]} for r in
              base.filter(kind="expense").exclude(payee="").values("payee")
              .annotate(t=Sum("amount"), c=Count("id")).order_by("-t")[:10]]

    tag_rows = (Transaction.objects.filter(user=user, date__gte=start, date__lte=end, tags__user=user)
                .values("tags__id", "tags__name")
                .annotate(income=Sum("amount", filter=Q(kind="income")), expense=Sum("amount", filter=Q(kind="expense")),
                          c=Count("id")).order_by("-expense", "tags__name"))
    tags = [{"name": r["tags__name"], "income": q2(r["income"] or ZERO), "expense": q2(r["expense"] or ZERO),
             "count": r["c"]} for r in tag_rows]

    cash_flow = []
    for account in Account.objects.filter(user=user):
        before = _account_before(account, start)
        tx = Transaction.objects.filter(user=user, account=account, date__gte=start, date__lte=end).aggregate(
            i=Sum("amount", filter=Q(kind="income")), e=Sum("amount", filter=Q(kind="expense")))
        t_in = Transfer.objects.filter(user=user, to_account=account, date__gte=start, date__lte=end).aggregate(t=Sum("amount"))["t"]
        t_out = Transfer.objects.filter(user=user, from_account=account, date__gte=start, date__lte=end).aggregate(t=Sum("amount"))["t"]
        inflow, outflow = q2(tx["i"] or ZERO), q2(tx["e"] or ZERO)
        # Money lent, borrowed or paid back (debts module) moves like a transfer: not income nor expense.
        d_in, d_out = _debt_flows(account, start, end)
        t_in, t_out = q2((t_in or ZERO) + d_in), q2((t_out or ZERO) + d_out)
        cash_flow.append({"name": account.name, "opening": before, "inflow": inflow, "outflow": outflow,
                          "transfers_in": t_in, "transfers_out": t_out,
                          "closing": before + inflow - outflow + t_in - t_out})

    return {
        "start": start, "end": end, "previous_start": prev_start, "previous_end": prev_end,
        "statement": statement, "categories": expense_rows, "category_total": expense_total,
        "trend": trend, "yearly": yearly, "net_worth": worth, "payees": payees, "tags": tags, "cash_flow": cash_flow,
    }


def _debt_flows(account, start=None, end=None, before=None):
    """(money in, money out) of an account through debt entries in a period."""
    qs = LedgerEntry.objects.filter(account=account, planned=False)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    if before:
        qs = qs.filter(date__lt=before)
    sums = qs.aggregate(i=Sum("amount", filter=Q(amount__lt=0)), o=Sum("amount", filter=Q(amount__gt=0)))
    return q2(-(sums["i"] or ZERO)), q2(sums["o"] or ZERO)


def _account_before(account, day):
    tx = Transaction.objects.filter(account=account, date__lt=day).aggregate(
        i=Sum("amount", filter=Q(kind="income")), e=Sum("amount", filter=Q(kind="expense")))
    t_in = Transfer.objects.filter(to_account=account, date__lt=day).aggregate(t=Sum("amount"))["t"]
    t_out = Transfer.objects.filter(from_account=account, date__lt=day).aggregate(t=Sum("amount"))["t"]
    d_in, d_out = _debt_flows(account, before=day)
    return q2(account.opening_balance + (tx["i"] or ZERO) - (tx["e"] or ZERO) + (t_in or ZERO) - (t_out or ZERO)
              + d_in - d_out)


def _pct(value):
    return "" if value is None else f"{value}"


def csv_rows(report, data):
    """(header, rows) for one report; text cells are formula-safe."""
    s = data["statement"]
    if report == "income_statement":
        rows = [["Receitas", safe_cell(r["name"]), f"{r['amount']:.2f}", f"{r['previous']:.2f}", _pct(r["change"])] for r in s["income"]]
        rows.append(["Receitas", "Total de receitas", f"{s['income_total']:.2f}", f"{s['previous']['income']:.2f}", _pct(s["changes"]["income"])])
        rows += [["Despesas", safe_cell(r["name"]), f"{r['amount']:.2f}", f"{r['previous']:.2f}", _pct(r["change"])] for r in s["expenses"]]
        rows.append(["Despesas", "Total de despesas", f"{s['expense_total']:.2f}", f"{s['previous']['expense']:.2f}", _pct(s["changes"]["expense"])])
        rows.append(["Resultado", "Resultado líquido", f"{s['net']:.2f}", f"{s['previous']['net']:.2f}", _pct(s["changes"]["net"])])
        return ["secao", "linha", "valor", "periodo_anterior", "variacao_percentual"], rows
    if report == "categories":
        return (["categoria", "valor", "participacao_percentual", "periodo_anterior", "variacao_percentual"],
                [[safe_cell(r["name"]), f"{r['amount']:.2f}", r["share"], f"{r['previous']:.2f}", _pct(r["change"])] for r in data["categories"]])
    if report == "trend":
        return (["periodo", "receitas", "despesas", "resultado", "taxa_de_poupanca_percentual"],
                [[r["period"], f"{r['income']:.2f}", f"{r['expense']:.2f}", f"{r['net']:.2f}", _pct(r["savings_rate"])] for r in data["trend"]])
    if report == "net_worth":
        return ["mes", "saldo"], [[r["period"], f"{r['balance']:.2f}"] for r in data["net_worth"]]
    if report == "payees":
        return ["favorecido", "total", "transacoes"], [[safe_cell(r["name"]), f"{r['amount']:.2f}", r["count"]] for r in data["payees"]]
    if report == "tags":
        return (["tag", "receitas", "despesas", "transacoes"],
                [[safe_cell(r["name"]), f"{r['income']:.2f}", f"{r['expense']:.2f}", r["count"]] for r in data["tags"]])
    if report == "cash_flow":
        return (["conta", "saldo_inicial", "receitas", "despesas", "transferencias_recebidas", "transferencias_enviadas", "saldo_final"],
                [[safe_cell(r["name"]), f"{r['opening']:.2f}", f"{r['inflow']:.2f}", f"{r['outflow']:.2f}",
                  f"{r['transfers_in']:.2f}", f"{r['transfers_out']:.2f}", f"{r['closing']:.2f}"] for r in data["cash_flow"]])
    raise KeyError(report)

"""Plain-language insights: how the month is going, what changed, upcoming bills and subscriptions."""
import calendar
import datetime
from collections import defaultdict
from statistics import median

from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import budgeting, services
from .formatting import format_money
from .models import Goal, LedgerEntry, Person, RecurringTransaction, Transaction
from .services import ZERO, add_months, month_end, month_start, q2

UNUSUAL_PERCENT = 50        # a category this far above its 3-month average is flagged
BILL_WINDOW_DAYS = 14
DUE_SOON_DAYS = 3


def _spent_by_category(user, start, end):
    rows = (Transaction.objects.filter(user=user, kind="expense", date__gte=start, date__lte=end)
            .values("category_id", "category__name", "category__color").annotate(t=Sum("amount")))
    return {r["category_id"]: (r["category__name"], r["category__color"], q2(r["t"])) for r in rows}


def movers(user, today):
    """Categories whose spending so far this month changed most compared with the same days last month."""
    this_start = month_start(today)
    prev_start = add_months(this_start, -1)
    prev_end = prev_start.replace(day=min(today.day, calendar.monthrange(prev_start.year, prev_start.month)[1]))
    now = _spent_by_category(user, this_start, today)
    before = _spent_by_category(user, prev_start, prev_end)
    diffs = []
    for cid in set(now) | set(before):
        name, color, a = now.get(cid, (None, None, ZERO))
        pname, pcolor, b = before.get(cid, (None, None, ZERO))
        diffs.append({"name": name or pname, "color": color or pcolor, "now": a, "before": b, "change": a - b})
    up = sorted((d for d in diffs if d["change"] > 0), key=lambda d: -d["change"])[:3]
    down = sorted((d for d in diffs if d["change"] < 0), key=lambda d: d["change"])[:3]
    return {"up": up, "down": down}


def unusual(user, today):
    """Categories already far above their average of the previous three complete months."""
    this_start = month_start(today)
    first = add_months(this_start, -3)
    history = defaultdict(lambda: ZERO)
    names = {}
    rows = (Transaction.objects.filter(user=user, kind="expense", date__gte=first, date__lt=this_start)
            .values("category_id", "category__name").annotate(t=Sum("amount")))
    for r in rows:
        history[r["category_id"]] = q2(r["t"]) / 3
        names[r["category_id"]] = r["category__name"]
    now = _spent_by_category(user, this_start, today)
    out = []
    for cid, (name, color, spent) in now.items():
        avg = history.get(cid, ZERO)
        if avg > 0 and spent > avg * (100 + UNUSUAL_PERCENT) / 100:
            out.append({"name": name, "color": color, "spent": spent, "average": q2(avg),
                        "percent": round(float(spent / avg * 100))})
    return sorted(out, key=lambda d: -d["percent"])[:3]


def subscriptions(user, today):
    """Payees (or descriptions) that repeat about monthly with a similar amount."""
    first = add_months(month_start(today), -4)
    groups = defaultdict(list)
    for t in (Transaction.objects.filter(user=user, kind="expense", date__gte=first, date__lte=today)
              .select_related("category")):
        key = (t.payee or t.name).strip().lower()
        if key:
            groups[key].append(t)
    tracked = {r.name.strip().lower() for r in RecurringTransaction.objects.filter(user=user)}
    found = []
    for key, items in groups.items():
        months = defaultdict(list)
        for t in items:
            months[month_start(t.date)].append(t)
        if len(months) < 3 or any(len(v) > 2 for v in months.values()):
            continue
        amounts = [float(t.amount) for t in items]
        mid = median(amounts)
        if mid <= 0 or any(abs(a - mid) / mid > 0.15 for a in amounts):
            continue
        last = max(items, key=lambda t: t.date)
        found.append({
            "name": last.payee or last.name, "amount": last.amount, "months": len(months),
            "category": last.category.name, "last_date": last.date,
            "next_date": add_months(last.date, 1), "tracked": last.name.strip().lower() in tracked or key in tracked,
        })
    return sorted(found, key=lambda d: -d["amount"])[:6]


def bills(user, today):
    """Recurring expenses (and dated debts) coming due soon, soonest first."""
    horizon = today + datetime.timedelta(days=BILL_WINDOW_DAYS)
    out = []
    for r in (RecurringTransaction.objects.filter(user=user, active=True, kind="expense", next_due__lte=horizon)
              .select_related("category")):
        out.append({"type": "bill", "id": r.pk, "name": r.name, "amount": r.amount, "due": r.next_due,
                    "days": (r.next_due - today).days, "category": r.category.name})
    from .planning import upcoming_invoices   # planning imports budgeting, which this module also uses
    for inv in upcoming_invoices(user, today, horizon):
        out.append({"type": "invoice", "id": inv["account_id"], "name": f"Fatura {inv['account']}", "amount": inv["open"],
                    "due": inv["due"], "days": inv["days"], "category": "Cartão de crédito"})
    # Installments to receive or pay (debts module).
    for entry in LedgerEntry.objects.filter(person__user=user, planned=True, date__lte=horizon).select_related("person"):
        receive = entry.amount < 0
        out.append({"type": "debt", "id": entry.person_id,
                    "name": f"{entry.person.name} te paga" if receive else f"Pagar {entry.person.name}",
                    "amount": abs(entry.amount), "due": entry.date, "days": (entry.date - today).days,
                    "category": entry.description})
    for person in Person.objects.filter(user=user):
        net = person.entries.filter(planned=False).aggregate(t=Sum("amount"))["t"] or ZERO
        if net == 0:
            continue
        due = (person.entries.filter(due_date__isnull=False, kind__in=("lent", "borrowed"), due_date__lte=horizon)
               .order_by("due_date").first())
        if due:
            out.append({"type": "debt", "id": person.pk, "name": f"{person.name} ({'te deve' if net > 0 else 'você deve'})",
                        "amount": abs(net), "due": due.due_date, "days": (due.due_date - today).days, "category": ""})
    return sorted(out, key=lambda b: b["due"])


def _money(user, value):
    return format_money(value, services.get_profile(user))


def sentences(user, today=None):
    """The headline sentences for the dashboard."""
    today = today or timezone.localdate()
    this_start = month_start(today)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    left = days_in_month - today.day
    out = []
    income, expense = services.income_expense(user, this_start, today)

    # budget health
    data = budgeting.status(user, today, today)
    items = ([data["overall"]] if data["overall"] else []) + sorted(data["categories"], key=lambda i: -i["percent"])
    for item in items[:3]:
        name = item["category"]["name"] if item["category"] else ""
        target = f"o orçamento de {name}" if item["category"] else "o orçamento do mês"
        label = f"do orçamento de {name}" if item["category"] else "do orçamento do mês"
        if item["over"]:
            out.append({"level": "bad", "text": f"Você estourou {target} em {_money(user, -item['remaining'])}.", "url": "/budgets/"})
        elif item["percent"] >= 50:
            days = f"falta{'m' if left != 1 else ''} {left} dia{'s' if left != 1 else ''}"
            out.append({"level": "warn" if item["level"] == "warn" else "info",
                        "text": f"Você já gastou {item['percent']:.0f}% {label} e {days} para acabar o mês.", "url": "/budgets/"})

    # pace against last month
    prev_start = add_months(this_start, -1)
    prev_end = prev_start.replace(day=min(today.day, calendar.monthrange(prev_start.year, prev_start.month)[1]))
    _, prev_expense = services.income_expense(user, prev_start, prev_end)
    if expense and prev_expense:
        change = round(float((expense - prev_expense) / prev_expense * 100))
        word = "a menos" if change < 0 else "a mais"
        if change == 0:
            out.append({"level": "info", "text": "Você está gastando mais ou menos o mesmo que nesta altura do mês passado.", "url": "/analytics/"})
        else:
            out.append({"level": "good" if change < 0 else "warn",
                        "text": f"Você gastou {_money(user, expense)} até agora, {abs(change)}% {word} que nesta altura do mês passado.",
                        "url": "/analytics/"})

    from .models import RecurringMatch
    waiting = RecurringMatch.objects.filter(recurring__user=user, decision=RecurringMatch.PENDING).count()
    if waiting:
        out.append({"level": "warn", "url": "/analytics/#plano",
                    "text": f"{waiting} lançamento(s) recorrente(s) parecem já ter sido lançados à mão: confirme no planejamento."})

    # forecast: the month plan (what happened + what is still to come + estimated variable spending),
    # the same numbers as the Dashboard totals and Analytics, instead of a simple daily pace.
    from .planning import month_plan
    plan = month_plan(user, this_start, today)
    total = plan["totals"]["expense"] + plan["totals"]["estimate"]
    if total > 0:
        done = plan["totals"]["expense_done"]
        out.append({"level": "info", "url": "/analytics/#plano",
                    "text": f"Pelo planejamento, você vai gastar cerca de {_money(user, total)} este mês "
                            f"(já gastou {_money(user, done)})."})

    for u in unusual(user, today)[:2]:
        out.append({"level": "warn", "text": f"{u['name']} já está em {u['percent']}% do gasto mensal habitual "
                                             f"({_money(user, u['average'])} em média).", "url": "/analytics/"})
    upcoming = [b for b in bills(user, today) if b["days"] <= DUE_SOON_DAYS]
    for b in upcoming[:2]:
        when = "está vencida" if b["days"] < 0 else "vence hoje" if b["days"] == 0 else f"vence em {b['days']} dia{'s' if b['days'] != 1 else ''}"
        out.append({"level": "warn", "text": f"{b['name']} ({_money(user, b['amount'])}) {when}.",
                    "url": {"bill": "/recurring/", "invoice": "/analytics/#plano"}.get(b["type"], "/people/")})
    if income and expense and income > 0:
        rate = round(float((income - expense) / income * 100))
        if rate >= 20:
            out.append({"level": "good", "text": f"Você guardou {rate}% da renda deste mês até agora.", "url": "/reports/"})
    return out


def goal_progress(user, today=None):
    rows = []
    for g in Goal.objects.filter(user=user):
        info = budgeting.goal_summary(g, today)
        rows.append({"id": g.pk, "name": g.name, "percent": info["percent"], "saved": info["saved"], "target": g.target_amount,
                     "required_monthly": info["required_monthly"], "complete": info["complete"]})
    return sorted(rows, key=lambda r: (r["complete"], -r["percent"]))[:4]


def insights(user, today=None):
    today = today or timezone.localdate()
    return {
        "sentences": sentences(user, today), "movers": movers(user, today), "subscriptions": subscriptions(user, today),
        "bills": bills(user, today), "goals": goal_progress(user, today),
    }


def weekly_digest(user, today=None):
    today = today or timezone.localdate()
    end = today
    start = today - datetime.timedelta(days=6)
    prev_end = start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=6)
    income, expense = services.income_expense(user, start, end)
    prev_income, prev_expense = services.income_expense(user, prev_start, prev_end)
    cats = sorted(_spent_by_category(user, start, end).values(), key=lambda v: -v[2])[:5]
    biggest = (Transaction.objects.filter(user=user, kind="expense", date__gte=start, date__lte=end)
               .select_related("category").order_by("-amount").first())
    return {
        "start": start, "end": end, "prev_start": prev_start, "prev_end": prev_end,
        "income": income, "expense": expense, "net": income - expense,
        "prev_income": prev_income, "prev_expense": prev_expense,
        "expense_change": round(float((expense - prev_expense) / prev_expense * 100)) if prev_expense else None,
        "categories": [{"name": n, "color": c, "total": t} for n, c, t in cats],
        "biggest": biggest,
        "count": Transaction.objects.filter(user=user, date__gte=start, date__lte=end).count(),
        "bills": bills(user, today), "budget": budgeting.status(user, today, today)["totals"],
    }

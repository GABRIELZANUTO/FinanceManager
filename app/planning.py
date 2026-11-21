"""Month planning: what already happened and what is still to come in a month.

Three sources make up a month:
  * transactions already recorded (done, or scheduled for later, like card purchases dated at the due date);
  * recurring items not posted yet (salary, bills, card installments, one-off forecasts);
  * an estimate for variable spending: what is left in each budget, or the recent average where
    a category has no budget.

Credit cards stay as they are: their purchases count as expenses in their categories. The money
leaves the bank only when the invoice is paid, so invoices are listed apart (never added to the
expenses again) and only used to project how much will be left in the bank accounts.
"""
import calendar
import datetime
import re
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from . import budgeting, services
from .models import Account, CardInvoice, LedgerEntry, RecurringTransaction, Transaction, Transfer

ZERO = services.ZERO
AVERAGE_MONTHS = 3
MONTHS_BACK = 120
MONTHS_AHEAD = 12
MIN_ESTIMATE = Decimal("1.00")


# --- card due dates ------------------------------------------------------------

def _due_day(account):
    if account.due_day:
        return account.due_day
    if account.pk is None:
        return None
    last = (CardInvoice.objects.filter(account=account, due_date__isnull=False)
            .order_by("-due_date").values_list("due_date", flat=True).first())
    return last.day if last else None


# A card without its closing day closes this many days before the due day (the usual gap).
DEFAULT_CLOSING_GAP = 7


def default_closing_day(due_day):
    """Day of the month 7 days before the due day: due on the 5th closes on the 28th."""
    return (due_day - DEFAULT_CLOSING_GAP - 1) % 30 + 1


def closing_day(account):
    """The card's closing day, or the default 7 days before the due day (None if neither is known)."""
    if account.closing_day:
        return account.closing_day
    due = _due_day(account)
    return default_closing_day(due) if due else None


def due_in_month(account, month, day=None):
    day = day or _due_day(account)
    if not day:
        return None
    return month.replace(day=min(day, calendar.monthrange(month.year, month.month)[1]))


def next_due_date(account, today=None):
    """The next invoice due date of a card (today counts), or None if the due day is unknown."""
    today = today or timezone.localdate()
    day = _due_day(account)
    if not day:
        return None
    due = due_in_month(account, services.month_start(today), day)
    return due if due >= today else due_in_month(account, services.add_months(services.month_start(today), 1), day)


def invoice_due_for(account, purchase):
    """Due date of the invoice a purchase on this card goes into (None if the due day is unknown).

    The invoice is the one that closes on or after the purchase, and it is due on the first due day
    after that closing. A card without a closing day closes 7 days before the due day."""
    due_day = _due_day(account)
    if not due_day:
        return None
    close_day = closing_day(account)
    month = services.month_start(purchase)
    closing = due_in_month(account, month, close_day)
    if purchase > closing:
        closing = due_in_month(account, services.add_months(month, 1), close_day)
    due = due_in_month(account, services.month_start(closing), due_day)
    if due <= closing:
        due = due_in_month(account, services.add_months(services.month_start(closing), 1), due_day)
    return due


PURCHASE_NOTE = re.compile(r"\s*Data da compra: \d{2}/\d{2}/\d{4}\.?")


def card_booking(user, account, purchase):
    """(date to book, purchase date to keep) for something bought on ``purchase`` day.

    Only credit cards differ: with "no mês da fatura" (Settings) the entry is dated at the due date
    of the invoice the purchase goes into; the real day is kept apart. Other accounts: (purchase, None)."""
    if account is None or not account.is_credit:
        return purchase, None
    if services.get_profile(user).card_date_mode == "invoice":
        return invoice_due_for(account, purchase) or purchase, purchase
    return purchase, purchase


def with_purchase_note(note, date, purchase):
    """The note with "Data da compra: dd/mm/aaaa." when the entry is dated on another day."""
    note = PURCHASE_NOTE.sub("", note or "").strip()
    if purchase and date != purchase:
        note = f"{note} Data da compra: {purchase:%d/%m/%Y}.".strip()
    return note[:1000]


def apply_card_rule(user, tx, purchase):
    """Date a transaction bought on ``purchase`` day by the card rule (see card_booking)."""
    tx.date, tx.purchase_date = card_booking(user, tx.account, purchase)
    tx.note = with_purchase_note(tx.note, tx.date, tx.purchase_date)
    return tx


def _tx_invoice_due(card, tx):
    """The invoice (its due date) a card transaction belongs to, with a closing day set."""
    if tx.purchase_date:
        return invoice_due_for(card, tx.purchase_date)
    if tx.date == due_in_month(card, services.month_start(tx.date)):
        return tx.date          # already dated at an invoice due date (imports, older entries)
    return invoice_due_for(card, tx.date)


# --- recurring occurrences not posted yet ---------------------------------------

def occurrences(user, start, end, matched=None):
    """Unposted dates of the active recurring items between start and end (both included).

    Dates before today are items that should have been posted but were not (for example, a
    recurring expense blocked by a low balance); they are returned too and marked late.
    An occurrence already typed by hand (same account, similar name and amount, a few days
    apart) is left out, so it counts once; it is appended to ``matched`` as (item, due, tx).
    """
    items = list(RecurringTransaction.objects.filter(user=user, active=True, next_due__lte=end)
                 .select_related("category", "account"))
    if not items:
        return []
    taken = set(Transaction.all_objects.filter(recurring__in=items, recurring_date__gte=start, recurring_date__lte=end)
                .values_list("recurring_id", "recurring_date"))
    default = services.default_account(user)
    candidates = services.manual_candidates(user, max(start, min(i.next_due for i in items)), end)
    from .models import RecurringMatch
    rejected = defaultdict(set)
    for rec_id, slot, tx_id in RecurringMatch.objects.filter(recurring__in=items, decision=RecurringMatch.DIFFERENT) \
            .values_list("recurring_id", "slot", "transaction_id"):
        rejected[rec_id].add((slot, tx_id))
    # occurrences whose date passed while waiting for an answer: still shown, counted once
    waiting = list(RecurringMatch.objects.filter(recurring__in=items, decision=RecurringMatch.PENDING,
                                                 slot__gte=start, slot__lte=end).select_related("transaction"))
    used = set()
    out = []
    for item in items:
        first = max(start, item.next_due)
        if item.end_date and first > item.end_date:
            continue
        due = services.first_due_on_or_after(item, first)
        guard = 0
        while due <= end and (not item.end_date or due <= item.end_date) and guard < 400:
            guard += 1
            if (item.pk, due) not in taken:
                account_id = item.account_id or (default.pk if default else None)
                typed = services.find_manual_match(item, due, account_id, candidates, used, rejected[item.pk])
                if typed is None:
                    out.append((item, due))
                else:
                    used.add(typed.pk)
                    if matched is not None:
                        matched.append((item, due, typed))
            due = services.following_date(item, due)
    if matched is not None:
        by_id = {i.pk: i for i in items}
        for match in waiting:
            if match.recurring_id in by_id and match.transaction_id not in used:
                matched.append((by_id[match.recurring_id], match.slot, match.transaction))
    return out


def installments_left(account):
    """Installments still to come on a card (not posted yet), like the bank holds them on the limit.

    Only installment plans ("Geladeira 3/10"), which have an end; ordinary recurring items such as
    subscriptions do not hold the limit ahead of time. An occurrence already posted, or typed by
    hand and matched to the plan, is not counted again (it is in the card's balance already).
    Returns {"amount", "count", "plans": [{"id", "name", "left", "each", "amount", "last"}]}."""
    out = {"amount": ZERO, "count": 0, "plans": []}
    if account is None or not account.is_credit or account.pk is None:
        return out
    items = list(RecurringTransaction.objects.filter(account=account, active=True, installment_total__isnull=False))
    if not items:
        return out
    last = {}
    for item in items:
        final = item.end_date or services.add_months(
            item.start_date, max(0, item.installment_total - (item.installment_first or 1)), anchor_day=item.start_date.day)
        last[item.pk] = final
    start = min(i.next_due for i in items)
    end = max(last.values())
    plans = {}
    for item, due in occurrences(account.user, start, end):
        if item.pk not in last or due > last[item.pk]:
            continue
        sign = 1 if item.kind == "expense" else -1
        plan = plans.setdefault(item.pk, {"id": item.pk, "name": services.base_name(item.name), "left": 0,
                                          "each": item.amount, "amount": ZERO, "last": last[item.pk]})
        plan["left"] += 1
        plan["amount"] += sign * item.amount
    out["plans"] = sorted(plans.values(), key=lambda p: p["last"])
    out["amount"] = max(ZERO, sum((p["amount"] for p in plans.values()), ZERO))
    out["count"] = sum(p["left"] for p in plans.values())
    return out


# --- debts and shared bills ------------------------------------------------------

def _debt_account(entry, default_account):
    if entry.account_id:
        return entry.account_id
    # Planned installments without an account are expected on the default account.
    return default_account.pk if entry.planned and default_account else None


def debt_entries(user, start, end):
    """Debt entries dated in a period that move (or will move) money: real ones with an account
    and every planned installment."""
    return [e for e in LedgerEntry.objects.filter(person__user=user, date__gte=start, date__lte=end)
            .select_related("person", "account").order_by("date", "id")
            if e.planned or e.account_id]


def _debt_row(entry, today, default_account):
    account_id = _debt_account(entry, default_account)
    effect = entry.money_effect if account_id else ZERO
    if entry.planned:
        status = "late" if entry.date < today else "planned"
    else:
        status = "done"
    owed_to_me = entry.amount < 0 if entry.kind in ("paid_me", "paid_them") else entry.amount > 0
    return {"date": entry.date, "id": entry.pk, "person": entry.person.name, "person_id": entry.person_id,
            "kind": entry.kind, "label": entry.get_kind_display(), "description": entry.description,
            "amount": abs(entry.amount), "effect": effect, "status": status, "planned": entry.planned,
            "account": entry.account.name if entry.account_id else (default_account.name if account_id else ""),
            "receive": effect > 0, "owed_to_me": owed_to_me, "account_id": account_id,
            "credit": bool(account_id and (entry.account.is_credit if entry.account_id else default_account.is_credit))}


# --- the month ----------------------------------------------------------------

def _cat(category):
    return {"id": category.pk, "name": category.name, "color": category.color, "icon": category.icon}


def _tx_row(tx, today):
    return {"date": tx.date, "name": tx.name, "amount": tx.amount, "kind": tx.kind, "category": _cat(tx.category),
            "account": tx.account.name if tx.account_id else "", "credit": bool(tx.account_id and tx.account.is_credit),
            "status": "done" if tx.date <= today else "scheduled", "source": "transaction", "id": tx.pk}


def _plan_row(item, due, today, default_account, booked=None):
    account = item.account or default_account
    return {"date": booked or due, "name": services.occurrence_name(item, due), "amount": item.amount, "kind": item.kind, "category": _cat(item.category),
            "account": account.name if account else "", "credit": bool(account and account.is_credit),
            "status": "late" if due < today else "planned", "source": "once" if item.frequency == item.ONCE else "recurring",
            "id": item.pk, "account_id": account.pk if account else None}


def _invoices(user, cards, month, today, default_account):
    """One estimated invoice per card due in ``month``: charges since the previous due date."""
    out = []
    for card in cards:
        day = _due_day(card)
        if not day:
            continue
        due = due_in_month(card, month, day)
        previous = due_in_month(card, services.add_months(month, -1), day)
        start = previous + datetime.timedelta(days=1)
        if closing_day(card):
            # Each purchase goes to the invoice whose closing comes after it.
            belongs = lambda when: invoice_due_for(card, when) == due  # noqa: E731
            wide_start, wide_end = due - datetime.timedelta(days=75), due + datetime.timedelta(days=40)
            charges = ZERO
            for tx in Transaction.objects.filter(user=user, account=card, date__gte=wide_start, date__lte=wide_end):
                if _tx_invoice_due(card, tx) == due:
                    charges += tx.amount if tx.kind == "expense" else -tx.amount
        else:
            belongs = lambda when: start <= when <= due  # noqa: E731
            wide_start, wide_end = start, due
            rows = (Transaction.objects.filter(user=user, account=card, date__gte=start, date__lte=due)
                    .values("kind").annotate(t=Sum("amount")))
            sums = {r["kind"]: r["t"] or ZERO for r in rows}
            charges = sums.get("expense", ZERO) - sums.get("income", ZERO)
        for item, when in occurrences(user, wide_start, wide_end):
            if (item.account_id or (default_account.pk if default_account else None)) == card.pk and belongs(when):
                charges += item.amount if item.kind == "expense" else -item.amount
        for entry in debt_entries(user, wide_start, wide_end):
            if _debt_account(entry, default_account) == card.pk and belongs(entry.date):
                charges -= entry.money_effect   # something paid on the card for someone, say
        paid = (Transfer.objects.filter(user=user, to_account=card, date__gte=start, date__lte=due)
                .aggregate(t=Sum("amount"))["t"] or ZERO)
        charges = max(ZERO, services.q2(charges))
        open_amount = max(ZERO, charges - paid)
        if charges == 0 and paid == 0:
            continue
        status = "paid" if open_amount == 0 else ("open" if due >= today else "overdue")
        out.append({"account": card.name, "account_id": card.pk, "due": due, "amount": charges, "paid": services.q2(paid),
                    "open": services.q2(open_amount), "status": status, "days": (due - today).days})
    return out


def _estimates(user, month, today, rows):
    """Variable spending still expected this month, per category."""
    if services.month_end(month) < today:
        return []
    rollup = budgeting.category_rollup(user)
    expenses = [r for r in rows if r["kind"] == "expense"]
    out = []
    budgeted = set()
    for b in budgeting.status(user, month, today)["items"]:
        if not b["category"]:
            continue
        cid = b["category"]["id"]
        budgeted.add(cid)
        planned = sum((r["amount"] for r in expenses if r["status"] in ("planned", "late", "scheduled")
                       and cid in rollup.get(r["category"]["id"], [r["category"]["id"]])), ZERO)
        if month > services.month_start(today):
            spent = ZERO   # nothing has happened yet in a future month (scheduled card purchases are in "planned")
        else:
            spent = b["spent"]     # only what already happened; what is coming is in "planned" below
        left = services.q2(b["available"] - spent - planned)
        if left >= MIN_ESTIMATE:
            out.append({"category": b["category"], "amount": left, "basis": "budget"})

    # Categories without a budget: the average of the last full months, counting only one-off
    # spending (recurring items and installments are already in the plan on their own dates).
    this_month = services.month_start(today)
    first = services.add_months(this_month, -AVERAGE_MONTHS)
    history = (Transaction.objects.filter(user=user, kind="expense", recurring__isnull=True,
                                          date__gte=first, date__lt=this_month)
               .values("category_id").annotate(t=Sum("amount")))
    done = defaultdict(lambda: ZERO)
    if month == this_month:
        for r in expenses:
            if r["source"] == "transaction" and not r.get("from_plan"):
                done[r["category"]["id"]] += r["amount"]
    cats = {c.pk: c for c in user.categories.all()}
    for h in history:
        cid = h["category_id"]
        if cid not in cats or any(c in budgeted for c in rollup.get(cid, [cid])):
            continue
        average = services.q2(h["t"] / AVERAGE_MONTHS)
        left = services.q2(average - done[cid])
        if left >= MIN_ESTIMATE:
            out.append({"category": _cat(cats[cid]), "amount": left, "basis": "average"})
    out.sort(key=lambda e: -e["amount"])
    return out


def month_plan(user, month, today=None, light=False):
    """The plan of one month. ``light`` skips invoices, the bank projection and the balance
    (only the rows and totals are needed to chain the balance into later months)."""
    today = today or timezone.localdate()
    this_month = services.month_start(today)
    month = services.month_start(month)
    month = max(services.add_months(this_month, -MONTHS_BACK), min(month, services.add_months(this_month, MONTHS_AHEAD)))
    end = services.month_end(month)
    default_account = services.default_account(user)

    rows = []
    txs = (Transaction.objects.filter(user=user, date__gte=month, date__lte=end)
           .select_related("category", "account").order_by("date", "id"))
    for tx in txs:
        row = _tx_row(tx, today)
        row["from_plan"] = tx.recurring_id is not None
        rows.append(row)
    matched = []
    # Card items are counted in the month they will be booked in (the card rule), which can be the
    # next month: look a little earlier and keep what lands in this month.
    for item, due in occurrences(user, month - datetime.timedelta(days=45), end, matched):
        account = item.account or default_account
        booked = card_booking(user, account, due)[0] if account and account.is_credit else due
        if month <= booked <= end:
            rows.append(_plan_row(item, due, today, default_account, booked))
    rows.sort(key=lambda r: (r["date"], r["source"] != "transaction", r["kind"] != "income", r["name"].lower()))

    def total(kind, statuses):
        return services.q2(sum((r["amount"] for r in rows if r["kind"] == kind and r["status"] in statuses), ZERO))

    estimates = _estimates(user, month, today, rows)
    estimate_total = services.q2(sum((e["amount"] for e in estimates), ZERO))
    totals = {
        "income_done": total("income", ("done",)), "income_planned": total("income", ("planned", "late", "scheduled")),
        "expense_done": total("expense", ("done",)), "expense_planned": total("expense", ("planned", "late", "scheduled")),
        "estimate": estimate_total,
    }
    totals["income"] = totals["income_done"] + totals["income_planned"]
    totals["expense"] = totals["expense_done"] + totals["expense_planned"]
    totals["result"] = totals["income"] - totals["expense"] - estimate_total

    debt_rows = [_debt_row(e, today, default_account) for e in debt_entries(user, month, end)]
    totals["debts"] = services.q2(sum((r["effect"] for r in debt_rows), ZERO))
    if light:
        return {"month": month, "totals": totals}
    accounts = list(Account.objects.filter(user=user, archived=False))
    cards = [a for a in accounts if a.is_credit]
    invoices = _invoices(user, cards, month, today, default_account)

    bank = None
    if end >= today:
        bank = _bank_projection(user, accounts, cards, month, end, today, default_account)

    kind = "past" if end < today else ("future" if month > this_month else "current")
    balance = _balance(user, month, this_month, today, totals)
    counted_once = [{"recurring": item.name, "recurring_id": item.pk, "due": due, "amount": item.amount,
                     "transaction": tx.name, "transaction_id": tx.pk, "date": tx.date, "transaction_amount": tx.amount}
                    for item, due, tx in matched if month <= due <= end]
    return {"month": month, "end": end, "today": today, "kind": kind, "rows": rows, "totals": totals,
            "counted_once": counted_once, "balance": balance, "debts": debt_rows,
            "estimates": estimates, "invoices": invoices, "bank": bank,
            "previous": services.add_months(month, -1) if month > services.add_months(this_month, -MONTHS_BACK) else None,
            "next": services.add_months(month, 1) if month < services.add_months(this_month, MONTHS_AHEAD) else None}


def _balance(user, month, this_month, today, totals):
    """What the accounts add up to at the start of the month and, with the month's result, at its end.

    Every account counts, credit cards included (their balance is what is owed), because the
    result already takes card purchases as expenses. Money lent, borrowed or paid back in the
    month moves the accounts too (it is not part of the result). Later months start where the
    previous one is expected to end."""
    start = services.balance_for(user, before=min(month, this_month))
    walk = this_month
    while walk < month:
        walked = month_plan(user, walk, today, light=True)["totals"]
        start += walked["result"] + walked["debts"]
        walk = services.add_months(walk, 1)
    start = services.q2(start)
    return {"start": start, "debts": totals["debts"], "end": services.q2(start + totals["result"] + totals["debts"])}


def _bank_projection(user, accounts, cards, month, end, today, default_account):
    """Money in banks, cash and wallets now, and day by day until the end of the month.

    Counts unposted recurring items on those accounts and the open part of every card invoice due
    until then. Variable spending estimates are left out: nobody knows yet which account pays them.
    """
    balances = services.account_balances(user, until=today)
    money = [a for a in accounts if not a.is_credit]
    if not money:
        return None
    ids = {a.pk for a in money}
    now = services.q2(sum((balances.get(a.pk, ZERO) for a in money), ZERO))
    events = defaultdict(lambda: ZERO)
    for item, due in occurrences(user, services.add_months(services.month_start(today), -MONTHS_BACK), end):
        account_id = item.account_id or (default_account.pk if default_account else None)
        if account_id in ids:
            events[max(due, today)] += item.amount if item.kind == "income" else -item.amount
    # Entries already recorded with a later date on these accounts (a salary typed in advance).
    for tx in Transaction.objects.filter(user=user, account_id__in=ids, date__gt=today, date__lte=end).only("date", "kind", "amount"):
        events[tx.date] += tx.amount if tx.kind == "income" else -tx.amount
    # Transfers scheduled ahead (an invoice payment, say): money leaving or entering these accounts.
    for t in Transfer.objects.filter(user=user, date__gt=today, date__lte=end).only("date", "amount", "from_account_id", "to_account_id"):
        if t.from_account_id in ids and t.to_account_id not in ids:
            events[t.date] -= t.amount
        elif t.to_account_id in ids and t.from_account_id not in ids:
            events[t.date] += t.amount
    # Installments to receive or pay (debts module), late ones as if they happened today.
    for entry in LedgerEntry.objects.filter(person__user=user, planned=True, date__lte=end):
        if _debt_account(entry, default_account) in ids:
            events[max(entry.date, today)] += entry.money_effect
    card_month = services.month_start(today)
    while card_month <= month:
        for invoice in _invoices(user, cards, card_month, today, default_account):
            if invoice["due"] >= today and invoice["open"] > 0:
                events[invoice["due"]] -= invoice["open"]
        card_month = services.add_months(card_month, 1)
    balance = now
    lowest = None
    first_negative = None
    days = []
    for day in sorted(d for d in events if d <= end):
        balance += events[day]
        if day >= month:
            days.append({"date": day, "change": services.q2(events[day]), "balance": services.q2(balance)})
        if lowest is None or balance < lowest["balance"]:
            lowest = {"date": day, "balance": services.q2(balance)}
        if balance < 0 and first_negative is None:
            first_negative = day
    return {"now": now, "end": services.q2(balance), "days": days, "lowest": lowest, "first_negative": first_negative}


def upcoming_invoices(user, today, horizon):
    """Open card invoices due between today and ``horizon`` (for the dashboard bills list)."""
    cards = [a for a in Account.objects.filter(user=user, archived=False) if a.is_credit]
    if not cards:
        return []
    default_account = services.default_account(user)
    out = []
    month = services.month_start(today)
    while month <= horizon:
        for invoice in _invoices(user, cards, month, today, default_account):
            if today <= invoice["due"] <= horizon and invoice["open"] > 0:
                out.append(invoice)
        month = services.add_months(month, 1)
    return out

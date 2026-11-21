"""Debts and shared bills: what you and other people owe each other, and the money it moves.

Sign of ``LedgerEntry.amount``: positive means the person owes you more, negative means you owe
them more. When an entry has an account, the money went through it like a transfer (never
income nor expense): the account moves by ``-amount`` (lending takes money out, being paid
back puts it in).

Split bills keep your share as an expense in its category:
  * you paid the whole bill: your share is an expense on the account you paid with, and the
    rest leaves the account as money the other person owes you;
  * the other person paid: your share is an expense on the date of the bill and, at the same
    time, money the person lent you, so the account does not move until you pay them back.

Planned entries are installments still to come ("vai me pagar em 5x"): they appear in the month
plan and only count once confirmed.
"""
import uuid
from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from . import services
from .models import LedgerEntry, Transaction, validate_date

ZERO = services.ZERO
CENT = Decimal("0.01")
OWED_TO_ME = ("lent", "split_me", "paid_them")      # these make the person owe you more (positive)
SPLITS = ("split_me", "split_them")
PAYMENTS = ("paid_me", "paid_them")
MAX_INSTALLMENTS = 72
SHARED_TAG = "dividido"


def signed(kind, amount):
    return amount if kind in OWED_TO_ME else -amount


def net(person):
    """What the person owes you (negative: what you owe them). Planned installments do not count yet."""
    return person.entries.filter(planned=False).aggregate(t=Sum("amount"))["t"] or ZERO


def split_parts(total, count):
    """``total`` in ``count`` parts that add up exactly (the cents left go to the first parts)."""
    base = (total / count).quantize(CENT, rounding=ROUND_DOWN)
    parts = [base] * count
    for i in range(int((total - base * count) / CENT)):
        parts[i] += CENT
    return parts


def _check_money(user, entries):
    """Closed months and the overdraft policy, for entries that move an account."""
    moving = [e for e in entries if e.account_id and not e.planned]
    services.ensure_unlocked(user, *[e.date for e in moving])
    services.ensure_effects(user, [(e.account, e.money_effect) for e in moving])


def _share_expense(user, person_names, amount, date, account, category, description, existing=None):
    """Create or update the expense for your share of a split bill."""
    tx = existing or Transaction(user=user, kind="expense")
    tx.name = (description or f"Conta dividida com {person_names}")[:120]
    tx.amount, tx.date, tx.account, tx.category = amount, date, account, category
    if category and not category.accepts("expense"):
        raise ValidationError({"category": "Esta categoria não pode ser usada em despesas."})
    tx.full_clean()
    return services.save_transaction(tx, tags=[SHARED_TAG], action="updated" if existing else "created")


def _installments(person, kind, owed, count, first_due, account, group, description):
    """Planned entries that settle ``owed`` (signed, as the main entry) in ``count`` monthly parts."""
    back = "paid_me" if owed > 0 else "paid_them"
    parts = split_parts(abs(owed), count)
    return [LedgerEntry(person=person, kind=back, amount=signed(back, part),
                        description=(f"Parcela {i + 1}/{count}" + (f" · {description}" if description else ""))[:120],
                        date=services.add_months(first_due, i, anchor_day=first_due.day), account=account,
                        planned=True, group=group)
            for i, part in enumerate(parts)]


def create_entry(user, person, data):
    """Record what happened with a person. ``data`` comes from LedgerEntryForm.cleaned_data.

    Returns the entries created (the main one first, then any planned installments)."""
    kind, account = data["kind"], data.get("account")
    date = data.get("date") or timezone.localdate()
    description = data.get("description", "")
    count = data.get("installments")
    group = uuid.uuid4() if count else None
    with services.locked(user):
        entries = []
        if kind in PAYMENTS and count:
            # "Vai me pagar em 5x de R$ 190": only the installments, each of the given amount.
            first = data.get("first_due") or date
            for i in range(count):
                entries.append(LedgerEntry(
                    person=person, kind=kind, amount=signed(kind, data["amount"]), account=account, planned=True,
                    group=group, date=services.add_months(first, i, anchor_day=first.day),
                    description=(f"Parcela {i + 1}/{count}" + (f" · {description}" if description else ""))[:120]))
        else:
            main = LedgerEntry(person=person, kind=kind, amount=signed(kind, data["amount"]), description=description,
                               date=date, due_date=data.get("due_date"), account=account, group=group)
            if kind in SPLITS and account and data.get("my_share"):
                main.transaction = _share_expense(user, person.name, data["my_share"], date, account,
                                                  data.get("category"), description)
            entries.append(main)
            if count:
                entries += _installments(person, kind, main.amount, count, data.get("first_due") or date,
                                         data.get("installment_account") or account, group, description)
        _check_money(user, entries)
        for entry in entries:
            entry.full_clean(exclude=["person", "transaction"])
            entry.save()
    return entries


def update_entry(user, entry, data):
    """Change an entry. For split bills, total and share are recomputed and the expense follows."""
    with services.locked(user):
        old_effect = (entry.account, -entry.money_effect) if entry.account_id and not entry.planned else None
        old_date = entry.date
        kind = entry.kind
        entry.description = data.get("description", entry.description)
        entry.date = data.get("date") or entry.date
        entry.due_date = data.get("due_date")
        entry.account = data.get("account")
        if kind in SPLITS and (data.get("total") is not None or kind == "split_them"):
            entry.amount = signed(kind, data["amount"])
            mine = data.get("my_share") if kind == "split_me" else data["amount"]
            tx = entry.transaction
            if entry.account and mine:
                entry.transaction = _share_expense(user, entry.person.name, mine, entry.date, entry.account,
                                                   data.get("category") or (tx.category if tx else None),
                                                   entry.description, existing=tx)
            elif tx is not None:
                services.delete_transactions(user, Transaction.objects.filter(pk=tx.pk))
                entry.transaction = None
        elif data.get("amount") is not None:
            entry.amount = signed(kind, data["amount"])
        effects = [(entry.account, entry.money_effect)] if entry.account_id and not entry.planned else []
        if old_effect:
            effects.append(old_effect)
        services.ensure_unlocked(user, old_date, entry.date)
        services.ensure_effects(user, effects)
        entry.full_clean(exclude=["person", "transaction"])
        entry.save()
    return entry


def delete_entry(user, entry):
    """Remove an entry, the expense of your share and the installments still planned for it."""
    with services.locked(user):
        if entry.account_id and not entry.planned:
            services.ensure_unlocked(user, entry.date)
            services.ensure_funds(user, -entry.money_effect, entry.account)
        if entry.transaction_id:
            services.delete_transactions(user, Transaction.objects.filter(pk=entry.transaction_id))
        if entry.group and not entry.planned:
            LedgerEntry.objects.filter(person=entry.person, group=entry.group, planned=True).delete()
        entry.delete()


def confirm_entry(user, entry, account=None, date=None, amount=None):
    """A planned installment happened: it starts counting in balances and in what is owed."""
    if not entry.planned:
        raise ValidationError("Esta parcela já foi confirmada.")
    with services.locked(user):
        entry.planned = False
        entry.account = account if account is not None else entry.account
        entry.date = date or timezone.localdate()
        if amount:
            entry.amount = signed(entry.kind, amount)
        entry.full_clean(exclude=["person", "transaction"])
        validate_date(entry.date)   # what happened cannot be in the future
        _check_money(user, [entry])
        entry.save()
    return entry


def settle(user, person, account=None, date=None):
    """Settle everything with a person in one go: one payment for the whole balance.

    Installments still planned are dropped, because nothing is owed any more."""
    owed = net(person)
    if owed == 0:
        raise ValidationError("Vocês já estão quites.")
    kind = "paid_me" if owed > 0 else "paid_them"
    date = date or timezone.localdate()
    validate_date(date)
    entry = LedgerEntry(person=person, kind=kind, amount=-owed, account=account, date=date, description="Acerto de contas")
    with services.locked(user):
        _check_money(user, [entry])
        entry.full_clean(exclude=["person", "transaction"])
        entry.save()
        person.entries.filter(planned=True).delete()
    return entry


def split_among(user, people, total, mine, date, account=None, category=None, description=""):
    """You paid one bill shared with several people: each of them owes you an equal part of the rest."""
    if not people:
        raise ValidationError("Escolha pelo menos uma pessoa.")
    others = total - mine
    if others <= 0:
        raise ValidationError({"my_share": "A sua parte deve ser menor que o total."})
    group = uuid.uuid4()
    names = ", ".join(p.name for p in people)
    with services.locked(user):
        entries = [LedgerEntry(person=p, kind="split_me", amount=part, description=description, date=date,
                               account=account, group=group)
                   for p, part in zip(people, split_parts(others, len(people)))]
        _check_money(user, entries)
        if account and mine > 0:
            share = _share_expense(user, names, mine, date, account, category, description)
            entries[0].transaction = share      # deleting this entry removes your share too
        for entry in entries:
            entry.full_clean(exclude=["person", "transaction"])
            entry.save()
    return entries


def summary_text(person, profile):
    """A plain message to send the person (WhatsApp, e-mail): the balance and the entries."""
    from .formatting import format_money

    owed = net(person)
    money = format_money(abs(owed), profile)
    if owed > 0:
        head = f"Oi, {person.name}! Pelas minhas contas você me deve {money}."
    elif owed < 0:
        head = f"Oi, {person.name}! Pelas minhas contas eu te devo {money}."
    else:
        head = f"Oi, {person.name}! Pelas minhas contas estamos quites."
    lines = [head, ""]
    for e in person.entries.filter(planned=False).order_by("date", "id"):
        sign = "+" if e.amount > 0 else "-"
        text = f"{e.date:%d/%m/%Y} {e.get_kind_display()}"
        if e.description:
            text += f" ({e.description})"
        lines.append(f"{text}: {sign}{format_money(abs(e.amount), profile)}")
    planned = list(person.entries.filter(planned=True).order_by("date"))
    if planned:
        lines += ["", "Parcelas combinadas:"]
        lines += [f"{e.date:%d/%m/%Y}: {format_money(abs(e.amount), profile)}" for e in planned]
    return "\n".join(lines)


def due_in(user, start, end):
    """Planned installments between two dates, soonest first (for the month plan and the dashboard)."""
    return list(LedgerEntry.objects.filter(person__user=user, planned=True, date__gte=start, date__lte=end)
                .select_related("person", "account").order_by("date", "id"))


def next_planned(person):
    entry = person.entries.filter(planned=True).order_by("date").first()
    return entry.date if entry else None

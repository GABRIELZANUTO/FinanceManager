"""Business rules: balances, overdraft policy, defaults and recurring items."""
import calendar
import datetime
import re
import unicodedata
from contextlib import contextmanager
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .formatting import format_money, month_label
from .models import (
    Account, AuditEntry, Category, CategoryRule, LedgerEntry, MonthLock, Profile, RecurringTransaction, Tag, Transaction,
    Transfer,
)

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

DEFAULT_CATEGORIES = (
    # name, kind, colour, icon
    ("Salário", "income", "green", "banknote"),
    ("Outras receitas", "income", "olive", "coins"),
    ("Mercado", "expense", "teal", "shopping-cart"),
    ("Aluguel", "expense", "plum", "house"),
    ("Contas da casa", "expense", "blue", "zap"),
    ("Transporte", "expense", "amber", "bus"),
    ("Restaurantes", "expense", "red", "utensils"),
    ("Saúde", "expense", "teal", "heart-pulse"),
    ("Compras", "expense", "gray", "shopping-bag"),
    ("Outros", "both", "gray", "ellipsis"),
)


def q2(value):
    return Decimal(value).quantize(CENT)


def get_profile(user):
    profile = getattr(user, "_profile_cache", None)
    if profile is None:
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={
                "currency_code": settings.DEFAULT_CURRENCY_CODE,
                "currency_symbol": settings.DEFAULT_CURRENCY_SYMBOL,
                "number_format": settings.DEFAULT_NUMBER_FORMAT,
            },
        )
        user._profile_cache = profile
    return profile


def forget_profile(user):
    user.__dict__.pop("_profile_cache", None)


def create_default_categories(user):
    existing = {n.lower() for n in Category.objects.filter(user=user).values_list("name", flat=True)}
    Category.objects.bulk_create(
        Category(user=user, name=name, kind=kind, color=color, icon=icon)
        for name, kind, color, icon in DEFAULT_CATEGORIES
        if name.lower() not in existing
    )


def default_account(user):
    """Where entries go when no account is chosen: the one picked in Settings, else the first
    account of the list (banks come first, so it is never a card by accident). Created on first use."""
    chosen = get_profile(user).default_account
    if chosen is not None and chosen.user_id == user.pk and not chosen.archived:
        return chosen
    account = Account.objects.filter(user=user, archived=False).first()
    if account is None:
        account = Account.objects.create(user=user, name="Conta principal", type="bank")
    return account


def set_up_new_user(user):
    """Defaults every account needs: categories and a first account."""
    create_default_categories(user)
    default_account(user)


# --- month helpers -----------------------------------------------------------

def month_start(day):
    return day.replace(day=1)


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day, months, anchor_day=None):
    """Add months, clamping the day to the target month's length."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    wanted = anchor_day or day.day
    return datetime.date(year, month, min(wanted, calendar.monthrange(year, month)[1]))


def parse_month(value, default=None):
    """Parse 'YYYY-MM' to the first day of that month; fall back to ``default``."""
    default = default or month_start(timezone.localdate())
    try:
        year, month = str(value).split("-")
        parsed = datetime.date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return default
    return parsed if parsed.year >= 2000 else default


# --- balances ----------------------------------------------------------------

def _net(qs):
    totals = qs.order_by().aggregate(
        income=Sum("amount", filter=Q(kind="income")),
        expense=Sum("amount", filter=Q(kind="expense")),
    )
    return q2(totals["income"] or ZERO), q2(totals["expense"] or ZERO)


def income_expense(user, start=None, end=None):
    qs = Transaction.objects.filter(user=user)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    return _net(qs)


def income_expense_for(queryset):
    """Income and expense totals of an arbitrary transaction queryset."""
    return _net(queryset)


def balance_for(user, before=None):
    """Sum of account opening balances plus income minus expense (optionally before a date).

    Transfers move money between accounts and cancel out, so they do not appear here.
    """
    opening = Account.objects.filter(user=user).aggregate(total=Sum("opening_balance"))["total"] or ZERO
    last = before - datetime.timedelta(days=1) if before else None
    income, expense = income_expense(user, end=last)
    # Money lent, borrowed or paid back through an account (debts module) moves the balance too.
    debts = ledger_money(user, until=last).aggregate(t=Sum("amount"))["t"] or ZERO
    return q2(opening + income - expense - debts)


def ledger_money(user, until=None):
    """Debt entries that really moved money through an account (not planned ones)."""
    qs = LedgerEntry.objects.filter(person__user=user, account__isnull=False, planned=False)
    return qs.filter(date__lte=until) if until else qs


def account_balances(user, until=None):
    """{account_id: Decimal} for all of the user's accounts, in a handful of queries.

    With ``until``, only movements up to that day count (card purchases dated later are left out).
    """
    balances = {a.pk: q2(a.opening_balance) for a in Account.objects.filter(user=user)}
    txs = Transaction.objects.filter(user=user, account__isnull=False)
    transfers = Transfer.objects.filter(user=user)
    if until:
        txs = txs.filter(date__lte=until)
        transfers = transfers.filter(date__lte=until)
    rows = txs.values("account_id").annotate(
        income=Sum("amount", filter=Q(kind="income")), expense=Sum("amount", filter=Q(kind="expense")))
    for r in rows:
        balances[r["account_id"]] += q2(r["income"] or ZERO) - q2(r["expense"] or ZERO)
    for r in transfers.values("from_account_id").annotate(t=Sum("amount")):
        balances[r["from_account_id"]] -= q2(r["t"])
    for r in transfers.values("to_account_id").annotate(t=Sum("amount")):
        balances[r["to_account_id"]] += q2(r["t"])
    for r in ledger_money(user, until).values("account_id").annotate(t=Sum("amount")):
        balances[r["account_id"]] -= q2(r["t"])   # money_effect is minus the amount
    return balances


def account_balance(account):
    return account_balances(account.user).get(account.pk, ZERO)


def allows_negative(user):
    preference = get_profile(user).allow_negative_balance
    return settings.ALLOW_NEGATIVE_BALANCE if preference is None else preference


class InsufficientFunds(ValidationError):
    pass


@contextmanager
def locked(user):
    """Serialise balance-affecting writes for one user."""
    with transaction.atomic():
        Profile.objects.select_for_update().filter(user=user).first()
        forget_profile(user)
        yield


def ensure_funds(user, delta, account=None):
    """Raise InsufficientFunds if applying ``delta`` would overdraw a disallowed account.

    Credit accounts may go negative. Without an account the overall balance is checked.
    Money dated later (a salary typed in advance) does not pay for anything yet, while expenses
    already scheduled do count: the lower of "today" and "with everything" is used.
    """
    if delta >= 0 or allows_negative(user):
        return
    tomorrow = timezone.localdate() + datetime.timedelta(days=1)
    if account is not None:
        if account.is_credit:
            return
        current = min(account_balances(user, until=tomorrow - datetime.timedelta(days=1)).get(account.pk, ZERO),
                      account_balance(account))
        label = f"o saldo de {account.name}"
    else:
        current = min(balance_for(user, before=tomorrow), balance_for(user))
        label = "o seu saldo"
    if current + delta < 0:
        raise InsufficientFunds(
            f"Isso deixaria {label} abaixo de zero "
            f"(hoje: {format_money(current, get_profile(user))}). "
            "Permita saldo negativo em Configurações para continuar."
        )


def ensure_effects(user, effects):
    """``effects`` is a list of (account_or_None, delta); sums per account before checking."""
    grouped = {}
    for account, delta in effects:
        key = account.pk if account is not None else None
        entry = grouped.setdefault(key, [account, ZERO])
        entry[1] += delta
    for account, delta in grouped.values():
        ensure_funds(user, delta, account)


def is_overdrawn(user):
    return balance_for(user) < 0


# --- rules, tags ------------------------------------------------------------

def suggest_category(user, text, kind=None):
    """First active rule (by priority) whose pattern occurs in ``text``."""
    haystack = (text or "").lower()
    if not haystack:
        return None
    for rule in CategoryRule.objects.filter(user=user, active=True).select_related("category"):
        if rule.pattern.lower() in haystack and (kind is None or rule.category.accepts(kind)):
            return rule.category
    return None


def set_tags(tx, names):
    """Replace a transaction's tags with the named ones (created per user, case-insensitive)."""
    tags = []
    for name in names:
        name = " ".join(str(name).split())[:30]
        if not name:
            continue
        tag = Tag.objects.filter(user=tx.user, name__iexact=name).first() or Tag.objects.create(user=tx.user, name=name)
        if tag not in tags:
            tags.append(tag)
    tx.tags.set(tags[:10])


# --- closed months and the change log ----------------------------------------

class MonthClosed(ValidationError):
    pass


def locked_months(user):
    return set(MonthLock.objects.filter(user=user).values_list("month", flat=True))


def ensure_unlocked(user, *days):
    """Refuse changes to transactions dated in a closed month."""
    closed = locked_months(user)
    if not closed:
        return
    for day in days:
        if day and month_start(day) in closed:
            raise MonthClosed(
                f"O mês de {month_label(day)} está fechado, então as transações dele não podem ser alteradas. "
                "Reabra o mês em Configurações primeiro."
            )


def snapshot(tx):
    return {
        "name": tx.name, "amount": f"{tx.amount:.2f}", "date": tx.date.isoformat(), "kind": tx.kind,
        "category": tx.category.name if tx.category_id else "", "account": tx.account.name if tx.account_id else "",
        "payee": tx.payee, "note": tx.note, "tags": sorted(t.name for t in tx.tags.all()) if tx.pk else [],
    }


def record(user, tx_id, action, summary="", before=None, after=None):
    AuditEntry.objects.create(user=user, transaction_id=tx_id, action=action, summary=summary[:300], before=before, after=after)


FIELD_LABELS = {
    "name": "descrição", "amount": "valor", "date": "data", "kind": "tipo", "category": "categoria", "account": "conta",
    "payee": "favorecido", "note": "observação", "tags": "tags",
}


def changed_fields(before, after):
    return [key for key in after if before.get(key) != after.get(key)]


# --- transaction writes ------------------------------------------------------

def save_transaction(instance, tags=None, action=None):
    """Check the overdraft policy and closed months, save, and log the change, atomically."""
    user = instance.user
    with locked(user):
        if instance.account_id is None:
            instance.account = default_account(user)
        effects = [(instance.account, instance.effect)]
        days = [instance.date]
        before = None
        if instance.pk:
            old = Transaction.objects.select_related("account", "category").prefetch_related("tags").filter(pk=instance.pk).first()
            if old:
                effects.append((old.account, -old.effect))
                days.append(old.date)
                before = snapshot(old)
        ensure_unlocked(user, *days)
        ensure_effects(user, effects)
        instance.save()
        if tags is not None:
            set_tags(instance, tags)
        instance = Transaction.objects.select_related("account", "category").prefetch_related("tags").get(pk=instance.pk)
        after = snapshot(instance)
        if before is None:
            record(user, instance.pk, action or "created", f"Adicionada: {instance.name} ({instance.amount:.2f})", None, after)
        else:
            fields = changed_fields(before, after)
            if fields:
                record(user, instance.pk, "updated", "Alterado: " + ", ".join(FIELD_LABELS.get(f, f) for f in fields), before, after)
    return instance


def delete_transactions(user, queryset):
    """Soft delete (kept in the trash for 30 days); returns the ids that were deleted."""
    with locked(user):
        rows = list(queryset.filter(user=user).select_related("account", "category"))
        ensure_unlocked(user, *[t.date for t in rows])
        ensure_effects(user, [(t.account, -t.effect) for t in rows])
        ids = [t.pk for t in rows]
        Transaction.objects.filter(pk__in=ids).update(deleted_at=timezone.now())
        for t in rows:
            record(user, t.pk, "deleted", f"Excluída: {t.name} ({t.amount:.2f})", snapshot(t), None)
    return ids


def restore_transactions(user, ids):
    with locked(user):
        qs = Transaction.all_objects.filter(user=user, pk__in=ids, deleted_at__isnull=False)
        rows = list(qs.select_related("account", "category"))
        ensure_unlocked(user, *[t.date for t in rows])
        ensure_effects(user, [(t.account, t.effect) for t in rows])
        qs.update(deleted_at=None)
        for t in rows:
            record(user, t.pk, "restored", f"Restaurada: {t.name}", None, snapshot(t))
    return [t.pk for t in rows]


def change_category(user, ids, category):
    with locked(user):
        rows = list(Transaction.objects.filter(user=user, pk__in=ids).select_related("category", "account"))
        ensure_unlocked(user, *[t.date for t in rows])
        for t in rows:
            if t.category_id != category.pk:
                record(user, t.pk, "updated", "Alterado: categoria", {"category": t.category.name}, {"category": category.name})
        Transaction.objects.filter(pk__in=[t.pk for t in rows]).update(category=category, updated_at=timezone.now())


def change_dates(user, ids, date=None, shift_months=None):
    """Move transactions to one ``date``, or by ``shift_months`` keeping each day of the month.

    Returns the number of transactions moved. Closed months (old or new date) block the change.
    """
    from .models import validate_transaction_date

    with locked(user):
        rows = list(Transaction.objects.filter(user=user, pk__in=ids))
        moves = []
        for t in rows:
            new = date if date is not None else add_months(t.date, shift_months)
            try:
                validate_transaction_date(new)
            except ValidationError as exc:
                raise ValidationError(f"{t.name}: {exc.messages[0]}") from None
            if new != t.date:
                moves.append((t, new))
        ensure_unlocked(user, *[t.date for t, _ in moves], *[new for _, new in moves])
        now = timezone.now()
        for t, new in moves:
            record(user, t.pk, "updated", "Alterado: data", {"date": t.date.isoformat()}, {"date": new.isoformat()})
            Transaction.objects.filter(pk=t.pk).update(date=new, updated_at=now)
        return len(moves)


def set_reconciled(user, ids, value):
    Transaction.objects.filter(user=user, pk__in=ids).update(reconciled=value)


def create_split(user, base, parts, tags=None):
    """One purchase over several categories: one transaction per part, linked by ``split_group``.

    ``base`` is an unsaved Transaction with the shared fields; ``parts`` is [(category, amount)].
    """
    import uuid

    with locked(user):
        group = uuid.uuid4()
        if base.account_id is None:
            base.account = default_account(user)
        total = sum((amount for _, amount in parts), ZERO)
        ensure_unlocked(user, base.date)
        ensure_effects(user, [(base.account, total if base.kind == "income" else -total)])
        made = []
        for category, amount in parts:
            tx = Transaction(user=user, name=base.name, amount=amount, date=base.date, kind=base.kind, category=category,
                             account=base.account, payee=base.payee, note=base.note, split_group=group)
            tx.save()
            if tags:
                set_tags(tx, tags)
            made.append(tx)
            record(user, tx.pk, "created", f"Adicionada: {tx.name} ({tx.amount:.2f}), parte de uma divisão", None,
                   snapshot(Transaction.objects.select_related("category", "account").get(pk=tx.pk)))
    return made


def create_transfer(transfer):
    with locked(transfer.user):
        ensure_unlocked(transfer.user, transfer.date)
        ensure_funds(transfer.user, -transfer.amount, transfer.from_account)
        transfer.save()
    return transfer


def delete_transfer(transfer):
    with locked(transfer.user):
        ensure_unlocked(transfer.user, transfer.date)
        ensure_funds(transfer.user, -transfer.amount, transfer.to_account)
        transfer.delete()


TRASH_DAYS = 30


def purge_deleted(older_than=datetime.timedelta(days=TRASH_DAYS)):
    cutoff = timezone.now() - older_than
    count, _ = Transaction.all_objects.filter(deleted_at__lt=cutoff).delete()
    return count


# --- recurring ---------------------------------------------------------------

def following_date(item, current):
    if item.frequency == RecurringTransaction.ONCE:
        return current + datetime.timedelta(days=1)   # past its end date, so the item finishes
    if item.frequency == RecurringTransaction.WEEKLY:
        return current + datetime.timedelta(weeks=1)
    return add_months(current, 1, anchor_day=item.start_date.day)


def first_due_on_or_after(item, day):
    if item.frequency == RecurringTransaction.ONCE:
        return max(item.start_date, day)
    due = item.start_date
    # jump close to the target to keep this cheap for old schedules
    if item.frequency == RecurringTransaction.WEEKLY:
        if due < day:
            due += datetime.timedelta(weeks=(day - due).days // 7)
    else:
        months = (day.year - due.year) * 12 + day.month - due.month - 1
        if months > 0:
            due = add_months(due, months, anchor_day=item.start_date.day)
    while due < day:
        due = following_date(item, due)
    return due


MAX_CATCH_UP = 400

# --- installments and entries typed by hand -----------------------------------

_PLAN_SUFFIX = re.compile(r"\s*\((?:parcelado em \d{1,3}x|\d{1,3}/\d{1,3})\)\s*$", re.IGNORECASE)
MATCH_DAYS = 10


def base_name(name):
    """A name without "(parcelado em 10x)" or "(3/10)"."""
    return _PLAN_SUFFIX.sub("", name or "").strip()


def installment_number(item, due):
    months = (due.year - item.start_date.year) * 12 + due.month - item.start_date.month
    return (item.installment_first or 1) + months


def plan_name(base, total):
    return f"{base[:100]} (parcelado em {total}x)"


def occurrence_name(item, due):
    """What one occurrence is called: installments read "Geladeira (3/10)", like on the card invoice."""
    if item.installment_total:
        return f"{base_name(item.name)[:110]} ({installment_number(item, due)}/{item.installment_total})"
    return item.name


def _words(text):
    text = unicodedata.normalize("NFKD", base_name(text)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def similar_names(a, b):
    a, b = _words(a), _words(b)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def amounts_close(a, b):
    return abs(a - b) <= max(Decimal("1.00"), max(a, b) * Decimal("0.05"))


def manual_candidates(user, start, end):
    """Transactions not linked to any recurring item, around a period (they may be one typed by hand)."""
    margin = datetime.timedelta(days=MATCH_DAYS)
    return list(Transaction.objects.filter(user=user, recurring__isnull=True, date__gte=start - margin,
                                           date__lte=end + margin).only("id", "name", "payee", "amount", "kind", "date",
                                                                        "account_id"))


def rejected_matches(item):
    """{(slot, transaction id)} the person said are different things (see RecurringMatch)."""
    from .models import RecurringMatch
    return set(RecurringMatch.objects.filter(recurring=item, decision=RecurringMatch.DIFFERENT)
               .values_list("slot", "transaction_id"))


def find_manual_match(item, due, account_id, candidates, used=(), rejected=()):
    """The transaction typed by hand that looks like this occurrence: same account and kind, a
    similar name, about the same amount, within a few days. None if there is no such transaction."""
    best = None
    for tx in candidates:
        if tx.pk in used or tx.kind != item.kind or tx.account_id != account_id or (due, tx.pk) in rejected:
            continue
        if abs((tx.date - due).days) > MATCH_DAYS or not amounts_close(tx.amount, item.amount):
            continue
        if not (similar_names(tx.name, item.name) or (item.payee and similar_names(tx.payee or tx.name, item.payee))):
            continue
        score = (abs(tx.amount - item.amount), abs((tx.date - due).days))
        if best is None or score < best[0]:
            best = (score, tx)
    return best[1] if best else None


# Installments keep the category of what was bought; this tag tells them apart.
INSTALLMENT_TAG = "parcelado"


def post_occurrence(item, due, account=None):
    """Create the transaction of one occurrence (checks closed months and the overdraft policy)."""
    account = account or item.account or default_account(item.user)
    tx = Transaction(
        user=item.user, name=occurrence_name(item, due), amount=item.amount, kind=item.kind,
        category=item.category, note=item.note, date=due, payee=item.payee,
        account=account, recurring=item, recurring_date=due,
    )
    if account.is_credit:        # a card: dated by the card rule (month of the invoice or of the purchase)
        from .planning import apply_card_rule
        apply_card_rule(item.user, tx, due)
    with transaction.atomic():
        ensure_unlocked(item.user, due)
        ensure_funds(item.user, tx.effect, tx.account)
        tx.save()
        if item.installment_total:
            set_tags(tx, [INSTALLMENT_TAG])
        record(item.user, tx.pk, "created", f"Adicionada: {tx.name} (recorrência)", None, snapshot(tx))
    return tx


def decide_match(item, slot, tx, decision):
    """The person's answer to "is this the same thing?" (see RecurringMatch)."""
    from .models import RecurringMatch
    with locked(item.user):
        match, _ = RecurringMatch.objects.get_or_create(recurring=item, slot=slot, transaction=tx)
        match.decision = decision
        match.save(update_fields=["decision"])
        taken = Transaction.all_objects.filter(recurring=item, recurring_date=slot).exists()
        if decision == RecurringMatch.SAME and not taken and tx.recurring_id is None:
            Transaction.objects.filter(pk=tx.pk).update(recurring=item, recurring_date=slot)
            record(item.user, tx.pk, "updated", f"Vinculada à recorrência “{item.name}” (confirmado)", None, None)
        elif decision == RecurringMatch.DIFFERENT and not taken and slot < item.next_due:
            post_occurrence(item, slot)   # its date already passed while waiting for the answer
    return match


def apply_recurring_item(item, today=None):
    """Create every due occurrence of one item. Idempotent. Returns the count."""
    today = today or timezone.localdate()
    created = 0
    if not item.active:
        return created
    error = ""
    guard = 0
    while item.next_due <= today and guard < MAX_CATCH_UP:
        guard += 1
        due = item.next_due
        if item.end_date and due > item.end_date:
            break
        account = item.account or default_account(item.user)
        typed = None
        if not Transaction.all_objects.filter(recurring=item, recurring_date=due).exists():
            typed = find_manual_match(item, due, account.pk, manual_candidates(item.user, due, due),
                                      rejected=rejected_matches(item))
        if typed is not None:
            # Looks already typed by hand: ask instead of linking or posting it twice (see RecurringMatch).
            from .models import RecurringMatch
            RecurringMatch.objects.get_or_create(recurring=item, slot=due, transaction=typed)
        elif not Transaction.all_objects.filter(recurring=item, recurring_date=due).exists():
            try:
                post_occurrence(item, due, account)
                created += 1
            except ValidationError as exc:
                error = "; ".join(exc.messages)[:200]
                break
            except IntegrityError:  # a concurrent run created it first
                pass
        item.next_due = following_date(item, due)
    if item.end_date and item.next_due > item.end_date:
        item.active = False
    item.last_error = error
    item.save(update_fields=["next_due", "active", "last_error"])
    return created


def apply_due(user=None, today=None):
    today = today or timezone.localdate()
    items = RecurringTransaction.objects.filter(active=True, next_due__lte=today).select_related("user", "category")
    if user is not None:
        items = items.filter(user=user)
    total = 0
    for item in items:
        with locked(item.user):
            total += apply_recurring_item(item, today)
    return total

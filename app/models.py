import datetime
import uuid
from decimal import Decimal

import django
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Case, Q, Value, When
from django.db.models.functions import Lower
from django.utils import timezone

from .icons import ACCOUNT_ICONS, ICON_CHOICES
from .sync.fields import SyncModel, TrackedManager

MAX_AMOUNT = Decimal("999999999.99")
MIN_AMOUNT = Decimal("0.01")
MIN_DATE = datetime.date(2000, 1, 1)

KIND_CHOICES = (("expense", "Despesa"), ("income", "Receita"))
# "despesas" / "receitas", used inside sentences such as "cannot be used for ..."
KIND_PLURAL = {"expense": "despesas", "income": "receitas"}
CATEGORY_KIND_CHOICES = (("both", "Receita e despesa"), ("expense", "Só despesa"), ("income", "Só receita"))

# A small calm palette; the CSS defines one class per key.
COLOR_CHOICES = (
    ("gray", "Cinza"),
    ("blue", "Azul"),
    ("teal", "Verde-azulado"),
    ("green", "Verde"),
    ("olive", "Oliva"),
    ("amber", "Âmbar"),
    ("red", "Vermelho"),
    ("plum", "Ameixa"),
)

NUMBER_FORMATS = (
    ("intl", "1,234,567.89"),
    ("indian", "12,34,567.89"),
    ("eu", "1.234.567,89"),
    ("space", "1 234 567,89"),
)


def check_constraint(condition, name):
    """CheckConstraint spelled for both Django 4.2 (check=) and 5.1+ (condition=)."""
    if django.VERSION >= (5, 1):
        return models.CheckConstraint(condition=condition, name=name)
    return models.CheckConstraint(check=condition, name=name)


def validate_date(value):
    latest = timezone.localdate() + datetime.timedelta(days=1)
    if value < MIN_DATE:
        raise ValidationError(f"A data não pode ser anterior a {MIN_DATE:%d/%m/%Y}.")
    if value > latest:
        raise ValidationError("A data não pode estar no futuro.")


# Credit card purchases may be booked on the invoice due date, which is usually ahead.
CARD_FUTURE_DAYS = 45
# Any transaction may be scheduled ahead (a salary next month, say). Entries dated after today
# show in the month plan and wait until their day to count in balances and in the dashboard.
FUTURE_DAYS = 366


def validate_transaction_date(value):
    latest = timezone.localdate() + datetime.timedelta(days=FUTURE_DAYS)
    if value < MIN_DATE:
        raise ValidationError(f"A data não pode ser anterior a {MIN_DATE:%d/%m/%Y}.")
    if value > latest:
        raise ValidationError("A data não pode passar de um ano no futuro.")


amount_validators = [MinValueValidator(MIN_AMOUNT), MaxValueValidator(MAX_AMOUNT)]


CARD_DATE_MODES = (("invoice", "No mês da fatura (data de vencimento)"), ("purchase", "No mês da compra"))


PLANS = (("free", "Gratuito"), ("sync", "Com sincronização"))


class Profile(SyncModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    currency_code = models.CharField(max_length=3, default="NPR")
    currency_symbol = models.CharField(max_length=5, default="Rs")
    number_format = models.CharField(max_length=10, choices=NUMBER_FORMATS, default="intl")
    # None means "use the ALLOW_NEGATIVE_BALANCE setting".
    allow_negative_balance = models.BooleanField(null=True, blank=True, default=None)
    onboarding_dismissed = models.BooleanField(default=False)
    setup_done = models.BooleanField(default=False)
    # Optional second currency shown next to the main one, with a manually entered rate:
    # 1 unit of the main currency = secondary_rate units of the secondary currency.
    secondary_currency_code = models.CharField(max_length=3, blank=True, default="")
    secondary_rate = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    # Where entries go when no account is chosen (empty: the first bank in the list).
    default_account = models.ForeignKey("Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Credit card purchases count in the month of the invoice (its due date) or of the purchase.
    # Every way in follows it: invoice import, quick entry, the transaction form, recurring items.
    card_date_mode = models.CharField(max_length=10, choices=CARD_DATE_MODES, default="invoice")
    # Last change number handed out for this user's data (see SyncChange).
    sync_seq = models.BigIntegerField(default=0, editable=False)
    # "sync" unlocks the phone/site synchronisation (set by hand for now; billing is a later decision).
    plan = models.CharField(max_length=10, choices=PLANS, default="free", editable=False)
    # Goes up when a backup is restored on the server: phones then download everything again
    # (restored versions may be lower than theirs and would be skipped as echoes).
    sync_epoch = models.PositiveIntegerField(default=1, editable=False)

    def __str__(self):
        return f"Profile of {self.user}"


class Category(SyncModel):
    name = models.CharField(max_length=50)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="categories")
    kind = models.CharField(max_length=7, choices=CATEGORY_KIND_CHOICES, default="both")
    color = models.CharField(max_length=10, choices=COLOR_CHOICES, default="gray")
    icon = models.CharField(max_length=30, choices=ICON_CHOICES, default="tag")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    class Meta:
        ordering = [Lower("name")]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="uniq_category_name_per_user"),
        ]

    def __str__(self):
        return self.name

    def accepts(self, kind):
        return self.kind in ("both", kind)

    @property
    def full_name(self):
        return f"{self.parent.name} / {self.name}" if self.parent_id else self.name


ACCOUNT_TYPES = (("bank", "Banco"), ("cash", "Dinheiro"), ("wallet", "Carteira digital"), ("credit", "Cartão de crédito"))


class Account(SyncModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="accounts")
    name = models.CharField(max_length=60)
    type = models.CharField(max_length=10, choices=ACCOUNT_TYPES, default="bank")
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    archived = models.BooleanField(default=False)
    # Credit cards only (both optional): the total limit and the day of the month the invoice is due.
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                       validators=[MinValueValidator(MIN_AMOUNT), MaxValueValidator(MAX_AMOUNT)])
    due_day = models.PositiveSmallIntegerField(null=True, blank=True,
                                               validators=[MinValueValidator(1), MaxValueValidator(31)])
    # Day the invoice closes: purchases after it go to the next invoice.
    closing_day = models.PositiveSmallIntegerField(null=True, blank=True,
                                                   validators=[MinValueValidator(1), MaxValueValidator(31)])

    class Meta:
        # Where the money really is comes first: banks, digital wallets, cash, then credit cards.
        ordering = ("archived", Case(When(type="bank", then=Value(0)), When(type="wallet", then=Value(1)),
                                     When(type="cash", then=Value(2)), default=Value(3)), Lower("name"))
        constraints = [models.UniqueConstraint(Lower("name"), "user", name="uniq_account_name_per_user")]

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return ACCOUNT_ICONS.get(self.type, "wallet")

    @property
    def is_credit(self):
        return self.type == "credit"


class Tag(SyncModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=30)

    class Meta:
        ordering = [Lower("name")]
        constraints = [models.UniqueConstraint(Lower("name"), "user", name="uniq_tag_name_per_user")]

    def __str__(self):
        return self.name


class ActiveTransactionManager(TrackedManager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Transaction(SyncModel):
    name = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=amount_validators)
    date = models.DateField(default=timezone.localdate, validators=[validate_transaction_date])
    kind = models.CharField(max_length=7, default="expense", choices=KIND_CHOICES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions")
    category = models.ForeignKey(Category, on_delete=models.RESTRICT, related_name="transactions")
    note = models.TextField(blank=True, default="", max_length=1000)
    payee = models.CharField(max_length=80, blank=True, default="")
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.RESTRICT, related_name="transactions")
    tags = models.ManyToManyField(Tag, blank=True, related_name="transactions")
    recurring = models.ForeignKey(
        "RecurringTransaction", null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions"
    )
    recurring_date = models.DateField(null=True, blank=True)
    # Credit cards counted by invoice month: ``date`` is the invoice due date, this is the day the
    # purchase was really made (also written in the note).
    purchase_date = models.DateField(null=True, blank=True)
    split_group = models.UUIDField(null=True, blank=True, db_index=True)
    reconciled = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ActiveTransactionManager()
    all_objects = TrackedManager()

    class Meta:
        ordering = ("-date", "-id")
        indexes = [models.Index(fields=["user", "date"], name="tx_user_date")]
        constraints = [
            check_constraint(Q(amount__gt=0), "tx_amount_positive"),
            models.UniqueConstraint(
                fields=["recurring", "recurring_date"],
                condition=Q(recurring__isnull=False),
                name="uniq_recurring_occurrence",
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # A recurring occurrence gets the same uid on the site and on the phone, so posting it on both
        # sides (each runs apply_due) gives one record when they synchronise.
        from .sync.capture import applying_remote
        if self._state.adding and self.recurring_id and self.recurring_date and not applying_remote.get():
            self.uid = occurrence_uid(self.recurring.uid, self.recurring_date)
        super().save(*args, **kwargs)

    @property
    def effect(self):
        """Signed effect on the balance."""
        return self.amount if self.kind == "income" else -self.amount

    def clean(self):
        super().clean()
        if self.category_id and self.user_id:
            if self.category.user_id != self.user_id:
                raise ValidationError({"category": "Escolha uma das suas categorias."})
            if not self.category.accepts(self.kind):
                raise ValidationError({"category": f"Esta categoria não pode ser usada em {KIND_PLURAL[self.kind]}."})
        if self.account_id and self.user_id and self.account.user_id != self.user_id:
            raise ValidationError({"account": "Escolha uma das suas contas."})


OCCURRENCE_NAMESPACE = uuid.UUID("6f1c2a7e-3b5d-4c8e-9a10-5e2f7d4b8c31")


def occurrence_uid(recurring_uid, day):
    return uuid.uuid5(OCCURRENCE_NAMESPACE, f"{recurring_uid}:{day.isoformat()}")


class Budget(SyncModel):
    """A spending limit for one month. ``category`` empty means the overall monthly budget."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="budgets")
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.CASCADE, related_name="budgets")
    month = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=amount_validators)
    rollover = models.BooleanField(default=False)
    warn_percent = models.PositiveSmallIntegerField(default=80, validators=[MinValueValidator(1), MaxValueValidator(99)])

    class Meta:
        ordering = ("month", "id")
        constraints = [
            models.UniqueConstraint(fields=["user", "category", "month"], name="uniq_budget_per_category_month"),
            models.UniqueConstraint(fields=["user", "month"], condition=Q(category__isnull=True), name="uniq_overall_budget_month"),
            check_constraint(Q(amount__gt=0), "budget_amount_positive"),
        ]

    def __str__(self):
        return f"{self.category or 'Overall'} budget {self.month:%Y-%m}"


class CategoryRule(SyncModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rules")
    pattern = models.CharField(max_length=60)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="rules")
    priority = models.PositiveSmallIntegerField(default=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("priority", "id")

    def __str__(self):
        return f"{self.pattern} -> {self.category}"


class Transfer(SyncModel):
    """Money moved between two of the user's accounts. Not income and not an expense."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transfers")
    from_account = models.ForeignKey(Account, on_delete=models.RESTRICT, related_name="transfers_out")
    to_account = models.ForeignKey(Account, on_delete=models.RESTRICT, related_name="transfers_in")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=amount_validators)
    date = models.DateField(default=timezone.localdate, validators=[validate_transaction_date])  # may be scheduled
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ("-date", "-id")
        constraints = [
            check_constraint(Q(amount__gt=0), "transfer_amount_positive"),
            check_constraint(~Q(from_account=models.F("to_account")), "transfer_distinct_accounts"),
        ]

    def __str__(self):
        return f"{self.from_account} -> {self.to_account}"


class Goal(SyncModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="goals")
    name = models.CharField(max_length=80)
    target_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=amount_validators)
    target_date = models.DateField(null=True, blank=True)
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="goals")
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="goals")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("created_at", "id")

    def __str__(self):
        return self.name


class Contribution(SyncModel):
    SYNC_OWNER = "goal__user"
    goal = models.ForeignKey(Goal, on_delete=models.CASCADE, related_name="contributions")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.localdate, validators=[validate_date])
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ("-date", "-id")


class SavedView(SyncModel):
    """A named set of transaction filters (a query string)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=60)
    query = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = [Lower("name")]
        constraints = [models.UniqueConstraint(Lower("name"), "user", name="uniq_view_name_per_user")]


class RecurringTransaction(SyncModel):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONCE = "once"   # a one-off planned income or expense: posted once, on its date
    FREQUENCY_CHOICES = ((MONTHLY, "Mensal"), (WEEKLY, "Semanal"), (ONCE, "Uma vez"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring")
    name = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=amount_validators)
    kind = models.CharField(max_length=7, default="expense", choices=KIND_CHOICES)
    category = models.ForeignKey(Category, on_delete=models.RESTRICT, related_name="recurring")
    note = models.TextField(blank=True, default="", max_length=1000)
    payee = models.CharField(max_length=80, blank=True, default="")
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.RESTRICT, related_name="recurring")
    frequency = models.CharField(max_length=7, choices=FREQUENCY_CHOICES, default=MONTHLY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    next_due = models.DateField()
    active = models.BooleanField(default=True)
    last_error = models.CharField(max_length=200, blank=True, default="")
    # Credit card installments ("parcela 3/10"): the installment number generated on
    # start_date and the total number of installments. Empty for ordinary items.
    installment_first = models.PositiveSmallIntegerField(null=True, blank=True)
    installment_total = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("name", "id")

    def __str__(self):
        return self.name


class RecurringMatch(SyncModel):
    """A recurring occurrence that looks already typed by hand (similar name, amount and date).

    Nothing is linked on its own: the person says whether it is the same thing. Until then it is
    counted once ("pending"); "same" links the typed entry to the occurrence; "different" posts
    the occurrence as well and the pair is never suggested again."""

    SYNC_OWNER = "recurring__user"
    PENDING, SAME, DIFFERENT = "pending", "same", "different"
    DECISIONS = ((PENDING, "A confirmar"), (SAME, "É o mesmo"), (DIFFERENT, "São diferentes"))

    recurring = models.ForeignKey("RecurringTransaction", on_delete=models.CASCADE, related_name="matches")
    slot = models.DateField()
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="recurring_matches")
    decision = models.CharField(max_length=10, choices=DECISIONS, default=PENDING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["recurring", "slot", "transaction"], name="uniq_recurring_match")]


class CardInvoice(SyncModel):
    """One imported credit card statement (fatura). Kept as history; the money lives in transactions."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="card_invoices")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="invoices")
    issuer = models.CharField(max_length=60, blank=True, default="")
    reference_date = models.DateField()
    closing_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=20, default="")
    file_name = models.CharField(max_length=120, blank=True, default="")
    lines_imported = models.PositiveIntegerField(default=0)
    amount_imported = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    plans_created = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("-reference_date", "-id")

    def __str__(self):
        return f"{self.account} {self.reference_date:%m/%Y}"


class AuditEntry(models.Model):
    """One change to a transaction. Keeps working after the transaction itself is purged."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_entries")
    transaction_id = models.BigIntegerField(db_index=True)
    action = models.CharField(max_length=12)  # created, updated, deleted, restored, recategorised, imported
    summary = models.CharField(max_length=300, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")


class MonthLock(SyncModel):
    """A closed month: transactions dated in it cannot be added, changed or removed until unlocked."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="month_locks")
    month = models.DateField()

    class Meta:
        ordering = ("-month",)
        constraints = [models.UniqueConstraint(fields=["user", "month"], name="uniq_month_lock")]


class Person(SyncModel):
    """Someone you lend to, borrow from, or share bills with. No account needed."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="people")
    name = models.CharField(max_length=60)

    class Meta:
        ordering = [Lower("name")]
        verbose_name_plural = "people"
        constraints = [models.UniqueConstraint(Lower("name"), "user", name="uniq_person_name_per_user")]

    def __str__(self):
        return self.name


LEDGER_KINDS = (
    ("lent", "Emprestei dinheiro"),
    ("borrowed", "Peguei dinheiro emprestado"),
    ("split_me", "Paguei uma conta dividida"),
    ("split_them", "A pessoa pagou uma conta dividida"),
    ("paid_me", "A pessoa me pagou de volta"),
    ("paid_them", "Paguei de volta"),
)


class LedgerEntry(SyncModel):
    """A signed amount: positive means the person owes you more, negative means you owe them more.

    With an ``account``, the money really moved through it, like a transfer: lending or paying
    back takes ``amount`` out of the account, borrowing or being paid back puts it in. It is
    never income nor an expense. Without an account (older entries, or "do not touch my
    balances") the entry only changes what you and the person owe each other.
    A ``planned`` entry is an installment still to come: it counts in the month plan, not yet in
    balances nor in what is owed, until it is confirmed.
    """

    SYNC_OWNER = "person__user"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=10, choices=LEDGER_KINDS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=120, blank=True, default="")
    date = models.DateField(default=timezone.localdate, validators=[validate_transaction_date])
    due_date = models.DateField(null=True, blank=True)
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.RESTRICT, related_name="ledger_entries")
    planned = models.BooleanField(default=False)
    # Your share of a split bill, booked as an expense in its category.
    transaction = models.OneToOneField("Transaction", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="ledger_entry")
    # Entries made together: one bill split among several people, or a series of installments.
    group = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("-date", "-id")

    @property
    def money_effect(self):
        """How much this entry moves its account: lending takes money out, being paid back puts it in."""
        return -self.amount


class SyncChange(models.Model):
    """The latest change of one object, numbered per user. Devices read "everything after N".

    ``deleted`` marks an object that no longer exists (a tombstone)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    model = models.CharField(max_length=30)
    uid = models.UUIDField()
    seq = models.BigIntegerField()
    deleted = models.BooleanField(default=False)
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "model", "uid"], name="uniq_sync_change")]
        indexes = [models.Index(fields=["user", "seq"], name="sync_change_user_seq")]


API_SCOPES = (("read", "Ler"), ("write", "Alterar"), ("sync", "Sincronizar"))


class ApiToken(models.Model):
    """Access key for /api/v1/ (the phone and other programs). Only a hash of the secret is kept.

    The full key is shown once: fp_<prefix>_<secret>. ``prefix`` finds the row, the secret is
    compared by hash."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=60)
    prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64)
    scopes = models.CharField(max_length=40, default="read")
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"{self.name} ({self.prefix})"

    @property
    def scope_set(self):
        return set(self.scopes.split())

    @property
    def active(self):
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > timezone.now())


# --- synchronisation (Alteração 2) ----------------------------------------------------------
# On the server: SyncReceipt. On a phone with an account: SyncOutbox, SyncState, SyncConflict.
# The tables exist on both sides (same migrations) and stay empty where they are not used.

class SyncReceipt(models.Model):
    """The answer already given to one pushed operation: a resend after a dropped connection gets
    the same answer instead of applying the change twice."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    op_id = models.UUIDField()
    result = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "op_id"], name="uniq_sync_receipt")]


OUTBOX_STATUS = (("pending", "Pendente"), ("sent", "Enviada"), ("conflict", "Conflito"), ("rejected", "Recusada"),
                 ("blocked", "Esperando outra"))


class SyncOutbox(models.Model):
    """A local change still to be sent. One row per object; the data is read when it is sent.

    ``base_version`` and ``base`` are the object as the server last knew it (before the first
    change not sent yet), empty for objects created here: the server merges field by field."""

    op_id = models.UUIDField(default=uuid.uuid4, unique=True)
    model = models.CharField(max_length=30)
    uid = models.UUIDField(db_index=True)
    action = models.CharField(max_length=6, default="upsert")    # upsert or delete
    base_version = models.PositiveIntegerField(null=True, blank=True)
    base = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=OUTBOX_STATUS, default="pending", db_index=True)
    blocked_by = models.UUIDField(null=True, blank=True)
    # Changed again while an earlier row for the same object was on its way: waits for its answer.
    follows = models.UUIDField(null=True, blank=True)
    # "Usar a deste celular": overwrite on the server even if it changed or was deleted there.
    force = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("id",)


SYNC_MODES = (("", "Ainda não escolhido"), ("local", "Só neste celular"), ("account", "Com conta online"))


class SyncState(models.Model):
    """Synchronisation state of this phone (one row)."""

    mode = models.CharField(max_length=10, choices=SYNC_MODES, blank=True, default="")
    server_user_id = models.BigIntegerField(null=True, blank=True)
    epoch = models.PositiveIntegerField(default=0)
    cursor = models.BigIntegerField(default=0)
    snapshot_done = models.BooleanField(default=False)
    snapshot_progress = models.JSONField(default=dict, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    backoff_until = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)

    @classmethod
    def get(cls):
        state, _ = cls.objects.get_or_create(pk=1)
        return state


CONFLICT_RESOLUTIONS = (("mine", "Ficou a deste celular"), ("server", "Ficou a do servidor"), ("manual", "Lançada à mão"))


class SyncConflict(models.Model):
    """A change the server did not take as it was: shown in Sincronização, Conflitos e erros."""

    op_id = models.UUIDField(db_index=True)
    model = models.CharField(max_length=30)
    uid = models.UUIDField()
    reason = models.CharField(max_length=30)
    message = models.CharField(max_length=300, blank=True, default="")
    local_data = models.JSONField(null=True, blank=True)
    server_data = models.JSONField(null=True, blank=True)
    server_version = models.PositiveIntegerField(null=True, blank=True)
    server_deleted = models.BooleanField(default=False)
    server_updated_by = models.CharField(max_length=60, blank=True, default="")
    server_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=10, choices=CONFLICT_RESOLUTIONS, blank=True, default="")

    class Meta:
        ordering = ("-created_at", "-id")

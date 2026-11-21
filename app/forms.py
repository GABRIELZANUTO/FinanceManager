import datetime
import unicodedata
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm
from django.db import transaction
from django.utils import timezone

from . import services
from .models import (
    MAX_AMOUNT,
    MIN_AMOUNT,
    MIN_DATE,
    NUMBER_FORMATS,
    Account,
    Budget,
    Category,
    CategoryRule,
    Contribution,
    Goal,
    KIND_CHOICES,
    KIND_PLURAL,
    CARD_DATE_MODES,
    LEDGER_KINDS,
    LedgerEntry,
    Person,
    RecurringTransaction,
    SavedView,
    Transaction,
    Transfer,
    validate_date,
    validate_transaction_date,
)

User = get_user_model()

CATEGORY_ERROR = {"invalid_choice": "Escolha uma das suas categorias."}
ACCOUNT_ERROR = {"invalid_choice": "Escolha uma das suas contas."}
MAX_TAGS = 10


def password_min_length():
    """The configured minimum password length (exposed to the browser so both sides agree)."""
    for validator in settings.AUTH_PASSWORD_VALIDATORS:
        if validator["NAME"].endswith("MinimumLengthValidator"):
            return validator.get("OPTIONS", {}).get("min_length", 8)
    return 8


def password_widget_attrs(field, **extra):
    """Mark a password field for the browser-side checks and drop the static rules list."""
    field.help_text = ""
    field.widget.attrs.update({"data-min-length": str(password_min_length()), **extra})


def split_name(full_name):
    parts = (full_name or "").split(None, 1)
    first = parts[0][:150] if parts else ""
    last = parts[1].strip()[:150] if len(parts) > 1 else ""
    return first, last


class TagsField(forms.Field):
    """Accepts a list (JSON) or a comma separated string; returns a list of clean names."""

    def to_python(self, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple)):
            raise forms.ValidationError("As tags devem ser uma lista de nomes.")
        names = []
        for raw in value:
            name = " ".join(str(raw).split())
            if not name:
                continue
            if len(name) > 30:
                raise forms.ValidationError("Uma tag pode ter no máximo 30 caracteres.")
            if name.lower() not in [n.lower() for n in names]:
                names.append(name)
        if len(names) > MAX_TAGS:
            raise forms.ValidationError(f"Use no máximo {MAX_TAGS} tags.")
        return names


class TransactionForm(forms.ModelForm):
    tags = TagsField(required=False)

    class Meta:
        model = Transaction
        fields = ["name", "amount", "date", "kind", "category", "account", "payee", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }
        error_messages = {"category": CATEGORY_ERROR, "account": ACCOUNT_ERROR}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self.fields["category"].required = False
        accounts = Account.objects.filter(user=user, archived=False)
        if self.instance.account_id:
            accounts = Account.objects.filter(user=user, pk__in=[a.pk for a in accounts] + [self.instance.account_id])
        self.fields["account"].queryset = accounts
        self.fields["account"].required = False

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        category = cleaned.get("category")
        if not category and kind and "category" not in self.errors:
            text = f"{cleaned.get('name', '')} {cleaned.get('payee', '')}"
            category = services.suggest_category(self.user, text, kind)
            if category is None:
                self.add_error("category", "Escolha uma categoria.")
            else:
                cleaned["category"] = category
                self.instance.category = category
        if category and kind and not category.accepts(kind):
            self.add_error("category", f"Esta categoria não pode ser usada em {KIND_PLURAL[kind]}.")
        return cleaned

    @property
    def tag_names(self):
        return self.cleaned_data.get("tags", [])


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "kind", "color", "icon", "parent"]
        error_messages = {"parent": {"invalid_choice": "Escolha uma das suas categorias."}}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        parents = Category.objects.filter(user=user, parent__isnull=True)
        if self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = parents
        self.fields["parent"].required = False

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Category.objects.filter(user=self.user, name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Você já tem uma categoria chamada “{name}”.")
        return name

    def clean_kind(self):
        kind = self.cleaned_data["kind"]
        if self.instance.pk and kind != "both":
            other = "income" if kind == "expense" else "expense"
            if Transaction.all_objects.filter(category=self.instance, kind=other).exists():
                raise forms.ValidationError(f"Esta categoria já tem {KIND_PLURAL[other]} lançadas.")
        return kind

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and self.instance.pk and Category.objects.filter(parent=self.instance).exists():
            raise forms.ValidationError("Uma categoria que tem subcategorias não pode virar subcategoria.")
        return parent


def _signed_amount(**kwargs):
    return forms.DecimalField(max_digits=14, decimal_places=2, min_value=-MAX_AMOUNT, max_value=MAX_AMOUNT, **kwargs)


class AccountForm(forms.ModelForm):
    opening_balance = _signed_amount(required=False, initial=0)

    class Meta:
        model = Account
        fields = ["name", "type", "opening_balance", "archived", "credit_limit", "due_day", "closing_day"]
        error_messages = {
            "credit_limit": {"min_value": "O limite deve ser maior que zero.", "invalid": "Informe um valor válido."},
            "due_day": {"min_value": "Use um dia entre 1 e 31.", "max_value": "Use um dia entre 1 e 31.",
                        "invalid": "Use um dia entre 1 e 31."},
            "closing_day": {"min_value": "Use um dia entre 1 e 31.", "max_value": "Use um dia entre 1 e 31.",
                            "invalid": "Use um dia entre 1 e 31."},
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("type") != "credit":   # limit and due day only mean something on a card
            cleaned["credit_limit"] = None
            cleaned["due_day"] = None
            cleaned["closing_day"] = None
        elif cleaned.get("closing_day") and not cleaned.get("due_day"):
            self.add_error("due_day", "Informe também o dia do vencimento.")
        elif cleaned.get("closing_day") and cleaned.get("closing_day") == cleaned.get("due_day"):
            self.add_error("closing_day", "O fechamento acontece alguns dias antes do vencimento.")
        return cleaned

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Account.objects.filter(user=self.user, name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Você já tem uma conta chamada “{name}”.")
        return name

    def clean_opening_balance(self):
        value = self.cleaned_data.get("opening_balance")
        return services.ZERO if value is None else value


class TransferForm(forms.ModelForm):
    class Meta:
        model = Transfer
        fields = ["from_account", "to_account", "amount", "date", "note"]
        error_messages = {"from_account": ACCOUNT_ERROR, "to_account": ACCOUNT_ERROR}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        accounts = Account.objects.filter(user=user, archived=False)
        self.fields["from_account"].queryset = accounts
        self.fields["to_account"].queryset = accounts

    def clean(self):
        cleaned = super().clean()
        a, b = cleaned.get("from_account"), cleaned.get("to_account")
        if a and b and a.pk == b.pk:
            self.add_error("to_account", "Escolha duas contas diferentes.")
        return cleaned


class SplitForm(forms.Form):
    """One purchase over several categories."""

    name = forms.CharField(max_length=120)
    date = forms.DateField(validators=[validate_transaction_date])
    kind = forms.ChoiceField(choices=KIND_CHOICES)
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, error_messages=ACCOUNT_ERROR)
    payee = forms.CharField(max_length=80, required=False)
    note = forms.CharField(max_length=1000, required=False)
    tags = TagsField(required=False)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, required=False)
    splits = forms.Field()

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["account"].queryset = Account.objects.filter(user=user, archived=False)
        self._parts = []

    def clean_splits(self):
        value = self.cleaned_data["splits"]
        if not isinstance(value, list) or not 2 <= len(value) <= 12:
            raise forms.ValidationError("Divida a compra em 2 a 12 partes.")
        return value

    def clean(self):
        cleaned = super().clean()
        raw = cleaned.get("splits")
        kind = cleaned.get("kind")
        if not raw or not kind:
            return cleaned
        from decimal import Decimal, InvalidOperation

        parts, total = [], Decimal("0.00")
        cats = {c.pk: c for c in Category.objects.filter(user=self.user)}
        for i, row in enumerate(raw, start=1):
            try:
                category = cats[int(row.get("category"))]
                amount = Decimal(str(row.get("amount")))
                if not amount.is_finite() or amount < MIN_AMOUNT or amount > MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
                    raise InvalidOperation
            except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
                self.add_error("splits", f"Parte {i}: escolha uma das suas categorias e um valor positivo com no máximo 2 casas decimais.")
                return cleaned
            if not category.accepts(kind):
                self.add_error("splits", f"Parte {i}: {category.name} não pode ser usada em {KIND_PLURAL[kind]}.")
                return cleaned
            parts.append((category, amount))
            total += amount
        if cleaned.get("amount") is not None and cleaned["amount"] != total:
            self.add_error("amount", f"As partes somam {total}, e não {cleaned['amount']}.")
        self._parts = parts
        return cleaned

    def parts(self):
        return self._parts

    def base_transaction(self):
        d = self.cleaned_data
        return Transaction(user=self.user, name=d["name"].strip(), date=d["date"], kind=d["kind"], account=d.get("account"),
                           payee=d.get("payee", ""), note=d.get("note", ""), amount=sum((a for _, a in self._parts), services.ZERO))


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["name"]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Person.objects.filter(user=self.user, name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Você já acompanha alguém chamado \u201c{name}\u201d.")
        return name


class LedgerEntryForm(forms.Form):
    """One thing that happened with a person (debts module). See app/debts.py for the money rules."""

    kind = forms.ChoiceField(choices=LEDGER_KINDS)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT, max_value=MAX_AMOUNT, required=False)
    total = forms.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT, max_value=MAX_AMOUNT, required=False)
    my_share = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, max_value=MAX_AMOUNT, required=False)
    description = forms.CharField(max_length=120, required=False)
    date = forms.DateField(validators=[validate_date], required=False)
    due_date = forms.DateField(required=False)
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, error_messages=ACCOUNT_ERROR)
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, error_messages=CATEGORY_ERROR)
    installments = forms.IntegerField(min_value=2, max_value=72, required=False,
                                      error_messages={"min_value": "Use de 2 a 72 parcelas.", "max_value": "Use de 2 a 72 parcelas."})
    first_due = forms.DateField(validators=[validate_transaction_date], required=False)
    installment_account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, error_messages=ACCOUNT_ERROR)

    def __init__(self, *args, user, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        accounts = Account.objects.filter(user=user, archived=False)
        self.fields["account"].queryset = accounts
        self.fields["installment_account"].queryset = accounts
        self.fields["category"].queryset = Category.objects.filter(user=user)
        if kind:   # editing: the kind of an entry does not change
            self.fields["kind"].required = False
            self.fixed_kind = kind
        else:
            self.fixed_kind = None

    def clean(self):
        cleaned = super().clean()
        kind = self.fixed_kind or cleaned.get("kind")
        cleaned["kind"] = kind
        if kind in ("split_me", "split_them") and cleaned.get("total") is not None:
            total = cleaned["total"]
            mine = cleaned.get("my_share")
            if mine is None:
                mine = (total / 2).quantize(Decimal("0.01"))
            if mine >= total:
                self.add_error("my_share", "Sua parte deve ser menor que o total.")
                return cleaned
            cleaned["my_share"] = mine
            cleaned["amount"] = (total - mine) if kind == "split_me" else mine
            if cleaned["amount"] <= 0:
                self.add_error("my_share", "Sua parte deve ser maior que zero quando a outra pessoa pagou a conta.")
            if cleaned.get("account") and mine > 0 and not cleaned.get("category"):
                self.add_error("category", "Escolha a categoria da sua parte (ela entra nas suas despesas).")
        if kind == "split_them" and cleaned.get("total") is None and cleaned.get("my_share"):
            # Editing a bill the other person paid: only your share is kept (it is what you owe).
            cleaned["amount"] = cleaned["my_share"]
            if cleaned.get("account") and not cleaned.get("category"):
                self.add_error("category", "Escolha a categoria da sua parte (ela entra nas suas despesas).")
        if cleaned.get("amount") is None and not self.errors:
            self.add_error("amount", "Informe um valor.")
        due = cleaned.get("due_date")
        if due and due < MIN_DATE:
            self.add_error("due_date", "Data antiga demais.")
        if cleaned.get("installments") and not cleaned.get("first_due"):
            self.add_error("first_due", "Informe a data da primeira parcela.")
        return cleaned


class SplitAmongForm(forms.Form):
    """You paid one bill shared with several people."""

    people = forms.JSONField()
    total = forms.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT, max_value=MAX_AMOUNT)
    my_share = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, max_value=MAX_AMOUNT, required=False)
    date = forms.DateField(validators=[validate_date], required=False)
    description = forms.CharField(max_length=120, required=False)
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, error_messages=ACCOUNT_ERROR)
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, error_messages=CATEGORY_ERROR)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["account"].queryset = Account.objects.filter(user=user, archived=False)
        self.fields["category"].queryset = Category.objects.filter(user=user)

    def clean_people(self):
        ids = self.cleaned_data["people"]
        if not isinstance(ids, list) or not ids:
            raise forms.ValidationError("Escolha pelo menos uma pessoa.")
        try:
            ids = list(dict.fromkeys(int(i) for i in ids))
        except (TypeError, ValueError):
            raise forms.ValidationError("Escolha pessoas da sua lista.") from None
        people = list(Person.objects.filter(user=self.user, pk__in=ids))
        if len(people) != len(ids):
            raise forms.ValidationError("Escolha pessoas da sua lista.")
        return people

    def clean(self):
        cleaned = super().clean()
        total, people = cleaned.get("total"), cleaned.get("people")
        if total is None or not people:
            return cleaned
        mine = cleaned.get("my_share")
        if mine is None:   # equal parts, you included
            mine = (total / (len(people) + 1)).quantize(Decimal("0.01"))
        if mine >= total:
            self.add_error("my_share", "A sua parte deve ser menor que o total.")
        cleaned["my_share"] = mine
        if cleaned.get("account") and mine > 0 and not cleaned.get("category"):
            self.add_error("category", "Escolha a categoria da sua parte (ela entra nas suas despesas).")
        return cleaned


class RuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["pattern", "category", "priority", "active"]
        error_messages = {"category": CATEGORY_ERROR}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self.fields["priority"].required = False
        self.fields["active"].required = False

    def clean_pattern(self):
        pattern = " ".join(self.cleaned_data["pattern"].split())
        if len(pattern) < 2:
            raise forms.ValidationError("Use pelo menos 2 caracteres.")
        return pattern

    def clean_priority(self):
        value = self.cleaned_data.get("priority")
        return 100 if value is None else value


class BudgetForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, error_messages=CATEGORY_ERROR)
    month = forms.CharField()
    amount = forms.DecimalField(min_value=MIN_AMOUNT, max_value=MAX_AMOUNT, max_digits=12, decimal_places=2)
    rollover = forms.BooleanField(required=False)
    warn_percent = forms.IntegerField(min_value=1, max_value=99, required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user, kind__in=("both", "expense"))

    def clean_month(self):
        value = self.cleaned_data["month"]
        month = services.parse_month(value, default=None) if len(str(value)) == 7 else None
        if month is None or str(value) != month.strftime("%Y-%m"):
            raise forms.ValidationError("Use um mês no formato 2025-03.")
        return month

    def save(self):
        data = self.cleaned_data
        defaults = {"amount": data["amount"], "rollover": data["rollover"]}
        if data.get("warn_percent"):
            defaults["warn_percent"] = data["warn_percent"]
        budget, _ = Budget.objects.update_or_create(
            user=self.user, category=data.get("category"), month=data["month"], defaults=defaults)
        return budget


class GoalForm(forms.ModelForm):
    class Meta:
        model = Goal
        fields = ["name", "target_amount", "target_date", "account", "category"]
        error_messages = {"category": CATEGORY_ERROR, "account": ACCOUNT_ERROR}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        self.fields["account"].queryset = Account.objects.filter(user=user)
        self.fields["category"].queryset = Category.objects.filter(user=user)

    def clean_target_date(self):
        value = self.cleaned_data.get("target_date")
        if value and value < MIN_DATE:
            raise forms.ValidationError("Data antiga demais.")
        if value and value > timezone.localdate() + datetime.timedelta(days=365 * 50):
            raise forms.ValidationError("Data distante demais no futuro.")
        return value


class ContributionForm(forms.ModelForm):
    amount = _signed_amount()

    class Meta:
        model = Contribution
        fields = ["amount", "date", "note"]

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value == 0:
            raise forms.ValidationError("Informe um valor diferente de zero.")
        return value


class SavedViewForm(forms.ModelForm):
    class Meta:
        model = SavedView
        fields = ["name", "query"]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        self.fields["query"].required = False

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if SavedView.objects.filter(user=self.user, name__iexact=name).exists():
            raise forms.ValidationError("Você já tem um filtro salvo com esse nome.")
        return name

    def clean_query(self):
        return (self.cleaned_data.get("query") or "").lstrip("?")


class RecurringForm(forms.ModelForm):
    class Meta:
        model = RecurringTransaction
        fields = ["name", "amount", "kind", "category", "account", "payee", "note", "frequency", "start_date", "end_date",
                  "installment_total", "installment_first"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}
        error_messages = {"category": CATEGORY_ERROR, "account": ACCOUNT_ERROR}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self.fields["account"].queryset = Account.objects.filter(user=user, archived=False)
        self.fields["account"].required = False
        self._schedule_before = (self.instance.start_date, self.instance.frequency) if self.instance.pk else None

    def clean_start_date(self):
        value = self.cleaned_data["start_date"]
        if value < MIN_DATE:
            raise forms.ValidationError(f"A data não pode ser anterior a {MIN_DATE:%d/%m/%Y}.")
        if value > timezone.localdate() + datetime.timedelta(days=366):
            raise forms.ValidationError("A data inicial está distante demais no futuro.")
        return value

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        total, first = cleaned.get("installment_total"), cleaned.get("installment_first")
        if total:
            # A purchase in installments, like the ones read from card invoices: one a month,
            # named "X (parcelado em Nx)", ending on the last installment.
            first = first or 1
            if not 2 <= total <= 72:
                self.add_error("installment_total", "Use de 2 a 72 parcelas.")
            elif not 1 <= first <= total:
                self.add_error("installment_first", f"Use um número de 1 a {total}.")
            elif start:
                cleaned["installment_first"] = first
                cleaned["frequency"] = RecurringTransaction.MONTHLY
                cleaned["end_date"] = services.add_months(start, total - first, anchor_day=start.day)
                if cleaned.get("name"):
                    cleaned["name"] = services.plan_name(services.base_name(cleaned["name"]), total)
            if cleaned.get("kind") == "income":
                self.add_error("kind", "Compra parcelada é uma despesa.")
        else:
            cleaned["installment_first"] = None
        if cleaned.get("frequency") == RecurringTransaction.ONCE and start:
            cleaned["end_date"] = start   # a one-off forecast happens on a single day
        elif start and end and end < start and not total:
            self.add_error("end_date", "A data final deve ser igual ou posterior à data inicial.")
        category, kind = cleaned.get("category"), cleaned.get("kind")
        if category and kind and not category.accepts(kind):
            self.add_error("category", f"Esta categoria não pode ser usada em {KIND_PLURAL[kind]}.")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if self._schedule_before is None:
            item.next_due = item.start_date
        elif self._schedule_before != (item.start_date, item.frequency):
            last = (
                Transaction.all_objects.filter(recurring=item).order_by("-recurring_date")
                .values_list("recurring_date", flat=True).first()
            )
            floor = item.start_date if last is None else max(item.start_date, last + datetime.timedelta(days=1))
            item.next_due = services.first_due_on_or_after(item, floor)
        if item.end_date and item.next_due > item.end_date:
            item.active = False
        if commit:
            item.save()
        return item


class RegisterForm(UserCreationForm):
    full_name = forms.CharField(max_length=100, label="Nome completo")
    email = forms.EmailField(max_length=254, label="E-mail")

    class Meta:
        model = User
        fields = ("full_name", "username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        password_widget_attrs(self.fields["password1"], **{
            "data-strength": "1", "data-similar-to": "id_full_name id_username id_email"})
        password_widget_attrs(self.fields["password2"], **{"data-match": "id_password1"})
        self.fields["username"].help_text = ""
        self.fields["username"].label = "Nome de usuário"
        self.fields["password1"].label = "Senha"
        self.fields["password2"].label = "Confirme a senha"
        self.fields["username"].widget.attrs["maxlength"] = "150"

    def clean_username(self):
        username = unicodedata.normalize("NFKC", self.cleaned_data["username"]).strip().lower()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Esse nome de usuário já está em uso.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Já existe uma conta com este e-mail.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name, user.last_name = split_name(self.cleaned_data["full_name"])
        if commit:
            with transaction.atomic():
                user.save()
                services.set_up_new_user(user)
        return user


NEGATIVE_CHOICES = (("", "Usar o padrão do sistema"), ("yes", "Permitir"), ("no", "Bloquear com erro"))


class SettingsForm(forms.Form):
    display_name = forms.CharField(max_length=100, label="Nome de exibição")
    currency_code = forms.CharField(min_length=3, max_length=3, label="Código da moeda")
    currency_symbol = forms.CharField(max_length=5, label="Símbolo da moeda")
    number_format = forms.ChoiceField(choices=NUMBER_FORMATS, label="Formato numérico")
    allow_negative_balance = forms.ChoiceField(choices=NEGATIVE_CHOICES, required=False, label="Saldo negativo")
    secondary_currency_code = forms.CharField(max_length=3, required=False, label="Mostrar também em")
    secondary_rate = forms.DecimalField(max_digits=16, decimal_places=6, min_value=Decimal("0.000001"), required=False,
                                        label="Cotação")
    default_account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, label="Conta padrão",
                                             empty_label="A primeira da lista", error_messages=ACCOUNT_ERROR)
    card_date_mode = forms.ChoiceField(choices=CARD_DATE_MODES, required=False, label="Compras no cartão de crédito contam")

    def __init__(self, *args, user, **kwargs):
        self.user = user
        profile = services.get_profile(user)
        pref = {None: "", True: "yes", False: "no"}[profile.allow_negative_balance]
        kwargs.setdefault("initial", {
            "display_name": f"{user.first_name} {user.last_name}".strip(),
            "currency_code": profile.currency_code,
            "currency_symbol": profile.currency_symbol,
            "number_format": profile.number_format,
            "allow_negative_balance": pref,
            "secondary_currency_code": profile.secondary_currency_code,
            "secondary_rate": profile.secondary_rate,
            "default_account": profile.default_account_id,
            "card_date_mode": profile.card_date_mode,
        })
        super().__init__(*args, **kwargs)
        self.fields["default_account"].queryset = Account.objects.filter(user=user, archived=False)

    def clean_secondary_currency_code(self):
        code = self.cleaned_data.get("secondary_currency_code", "").strip().upper()
        if code and not (code.isascii() and code.isalpha() and len(code) == 3):
            raise forms.ValidationError("Use um código de três letras, como USD.")
        return code

    def clean(self):
        cleaned = super().clean()
        code, rate = cleaned.get("secondary_currency_code"), cleaned.get("secondary_rate")
        if code and not rate:
            self.add_error("secondary_rate", "Informe quantas unidades dessa moeda vale uma unidade da sua moeda principal.")
        if rate and not code:
            self.add_error("secondary_currency_code", "Informe o código da moeda desta cotação.")
        return cleaned

    def clean_currency_code(self):
        code = self.cleaned_data["currency_code"].strip().upper()
        if not (code.isascii() and code.isalpha()):
            raise forms.ValidationError("Use um código de três letras, como BRL ou USD.")
        return code

    def clean_currency_symbol(self):
        symbol = self.cleaned_data["currency_symbol"].strip()
        if not symbol or any(unicodedata.category(c).startswith("C") or c.isspace() for c in symbol):
            raise forms.ValidationError("Informe de 1 a 5 caracteres visíveis.")
        return symbol

    def save(self):
        data = self.cleaned_data
        profile = services.get_profile(self.user)
        profile.currency_code = data["currency_code"]
        profile.currency_symbol = data["currency_symbol"]
        profile.number_format = data["number_format"]
        profile.allow_negative_balance = {"": None, "yes": True, "no": False}[data["allow_negative_balance"] or ""]
        profile.secondary_currency_code = data.get("secondary_currency_code", "")
        profile.secondary_rate = data.get("secondary_rate") if profile.secondary_currency_code else None
        profile.default_account = data.get("default_account")
        profile.card_date_mode = data.get("card_date_mode") or profile.card_date_mode
        profile.save()
        self.user.first_name, self.user.last_name = split_name(data["display_name"])
        self.user.save(update_fields=["first_name", "last_name"])
        services.forget_profile(self.user)


class ChangePasswordForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = kwargs.get("user") or (args[0] if args else None)
        words = " ".join(filter(None, [getattr(user, "username", ""), getattr(user, "email", ""),
                                       getattr(user, "first_name", ""), getattr(user, "last_name", "")]))
        self.fields["old_password"].label = "Senha atual"
        self.fields["new_password1"].label = "Nova senha"
        self.fields["new_password2"].label = "Confirme a nova senha"
        password_widget_attrs(self.fields["new_password1"], **{"data-strength": "1", "data-similar-words": words})
        password_widget_attrs(self.fields["new_password2"], **{"data-match": "id_new_password1"})


class ImportForm(forms.Form):
    file = forms.FileField(label="Arquivo CSV")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > settings.FILE_UPLOAD_MAX_MEMORY_SIZE // 2:
            raise forms.ValidationError("O arquivo é grande demais (limite de 512 KB).")
        if not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Envie um arquivo .csv.")
        return upload


class CardInvoiceForm(forms.Form):
    """Upload of a credit card statement (PDF or CSV)."""

    DATE_MODES = (
        ("due", "Na data de vencimento da fatura (o mês em que você paga)"),
        ("purchase", "Na data de cada compra"),
        ("invoice", "Na data de fechamento da fatura"),
    )

    file = forms.FileField(label="Fatura (PDF ou CSV)", required=False)
    pasted = forms.CharField(
        label="Ou cole o CSV que a IA devolveu", required=False, max_length=300_000,
        widget=forms.Textarea(attrs={"rows": 5, "spellcheck": "false", "placeholder": "# emissor: ...\ndata;descricao;valor;..."}),
    )
    account = forms.ModelChoiceField(
        label="Cartão", queryset=Account.objects.none(), required=False,
        empty_label="Descobrir pelo banco da fatura (cria o cartão se precisar)",
    )
    new_account_name = forms.CharField(label="Ou crie um cartão novo com o nome", max_length=60, required=False)
    # Not shown any more: the rule in Settings ("Compras no cartão de crédito contam") decides.
    date_mode = forms.ChoiceField(label="Lançar as despesas", choices=DATE_MODES, initial="due",
                                  widget=forms.RadioSelect, required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(user=user, type="credit", archived=False)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("file") and not (cleaned.get("pasted") or "").strip() and not self.errors:
            self.add_error("file", "Escolha o arquivo da fatura ou cole o CSV no campo abaixo.")
        return cleaned

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload:
            return upload
        name = upload.name.lower()
        if not name.endswith((".pdf", ".csv", ".txt")):
            raise forms.ValidationError("Envie a fatura em PDF ou CSV.")
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError("O arquivo é grande demais (limite de 10 MB).")
        return upload

    def clean_new_account_name(self):
        return " ".join(self.cleaned_data["new_account_name"].split())


class SetupForm(forms.Form):
    """First-run setup: currency, accounts, starter categories, optional budget and sample data."""

    currency_code = forms.CharField(min_length=3, max_length=3, label="Código da moeda")
    currency_symbol = forms.CharField(max_length=5, label="Símbolo da moeda")
    number_format = forms.ChoiceField(choices=NUMBER_FORMATS, label="Formato numérico")
    main_name = forms.CharField(max_length=60, label="Nome da conta principal")
    main_opening = _signed_amount(required=False, label="Saldo atual")
    cash = forms.BooleanField(required=False, label="Também tenho dinheiro em espécie")
    cash_opening = _signed_amount(required=False, label="Dinheiro em mãos")
    wallet = forms.BooleanField(required=False, label="Uso carteira digital (Mercado Pago, PicPay...)")
    wallet_opening = _signed_amount(required=False, label="Saldo da carteira digital")
    categories = forms.MultipleChoiceField(
        choices=[(name, name) for name, _, _, _ in services.DEFAULT_CATEGORIES], required=False, label="Categorias iniciais")
    monthly_budget = forms.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT, max_value=MAX_AMOUNT,
                                        required=False, label="Orçamento mensal de gastos")
    sample_data = forms.BooleanField(required=False, label="Preencher o app com dados de exemplo para eu explorar")

    def __init__(self, *args, user, **kwargs):
        self.user = user
        profile = services.get_profile(user)
        kwargs.setdefault("initial", {
            "currency_code": profile.currency_code, "currency_symbol": profile.currency_symbol,
            "number_format": profile.number_format, "main_name": "Conta principal", "main_opening": 0,
            "categories": [name for name, _, _, _ in services.DEFAULT_CATEGORIES],
        })
        super().__init__(*args, **kwargs)

    clean_currency_code = SettingsForm.clean_currency_code
    clean_currency_symbol = SettingsForm.clean_currency_symbol

    def clean_main_name(self):
        return " ".join(self.cleaned_data["main_name"].split())

    def apply(self):
        d = self.cleaned_data
        profile = services.get_profile(self.user)
        profile.currency_code, profile.currency_symbol, profile.number_format = d["currency_code"], d["currency_symbol"], d["number_format"]
        profile.setup_done = True
        profile.save()
        with transaction.atomic():
            if d["sample_data"]:
                from . import backup, demo
                backup.erase(self.user)
                demo.seed_user(self.user)
            else:
                main = services.default_account(self.user)
                main.name, main.opening_balance = d["main_name"], d.get("main_opening") or services.ZERO
                main.save()
                if d["cash"]:
                    Account.objects.get_or_create(user=self.user, name__iexact="Dinheiro",
                                                  defaults={"name": "Dinheiro", "type": "cash", "opening_balance": d.get("cash_opening") or 0})
                if d["wallet"]:
                    Account.objects.get_or_create(user=self.user, name__iexact="Carteira digital",
                                                  defaults={"name": "Carteira digital", "type": "wallet", "opening_balance": d.get("wallet_opening") or 0})
                keep = set(d["categories"])
                Category.objects.filter(user=self.user, name__in=[n for n, *_ in services.DEFAULT_CATEGORIES if n not in keep],
                                        transactions__isnull=True).delete()
                if not Category.objects.filter(user=self.user).exists():
                    services.create_default_categories(self.user)
                if d.get("monthly_budget"):
                    today = timezone.localdate()
                    Budget.objects.update_or_create(user=self.user, category=None, month=today.replace(day=1),
                                                    defaults={"amount": d["monthly_budget"]})
        services.forget_profile(self.user)

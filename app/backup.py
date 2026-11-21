"""Full-data JSON backup: export, strict validation, and restore (replaces the user's data)."""
import datetime
import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import services
from .icons import CATEGORY_ICONS
from .models import (
    ACCOUNT_TYPES, CATEGORY_KIND_CHOICES, COLOR_CHOICES, LEDGER_KINDS, MAX_AMOUNT, MIN_DATE, NUMBER_FORMATS, Account, AuditEntry,
    Budget, Category, CategoryRule, Contribution, Goal, LedgerEntry, MonthLock, Person, Profile, RecurringTransaction, SavedView,
    Tag, Transaction, Transfer,
)

FORMAT = "finance-manager-backup"
VERSION = 1
MAX_BYTES = 8 * 1024 * 1024


class BackupError(Exception):
    def __init__(self, problems):
        self.problems = problems if isinstance(problems, list) else [problems]
        super().__init__("; ".join(self.problems[:5]))


def _d(value):
    return None if value is None else f"{value:.2f}"


def _small_int(value):
    """Installment numbers in a backup: a small positive integer, or None (older backups lack them)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 999 else None


def _limit(value):
    """Card limit in a backup: a positive amount, or None (older backups lack it)."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if Decimal("0.01") <= amount <= Decimal("999999999.99") else None


def _day(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 31 else None


def _uuid(value):
    """A group id in a backup, or None (older backups lack it)."""
    import uuid
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _ident(obj):
    """Global identity of a record, kept by a restore so phones see the same object again."""
    return {"uid": str(obj.uid), "version": obj.version}


def _read_ident(row):
    if not isinstance(row, dict):
        return {}
    uid = _uuid(row.get("uid"))
    version = row.get("version")
    out = {"uid": uid} if uid else {}
    if uid and isinstance(version, int) and not isinstance(version, bool) and 1 <= version <= 2_000_000_000:
        out["version"] = version
    return out


def _attach_idents(data, out):
    """Copy uid and version from the file onto the validated rows (same order, objects only)."""
    for key in ("accounts", "categories", "rules", "transactions", "transfers", "budgets", "goals", "recurring", "views", "people"):
        source = data.get(key)
        if not isinstance(source, list):
            continue
        for row, raw in zip(out.get(key, []), [r for r in source if isinstance(r, dict)]):
            row["ident"] = _read_ident(raw)
            nested = {"goals": "contributions", "people": "entries"}.get(key)
            if nested:
                inner = raw.get(nested) if isinstance(raw.get(nested), list) else []
                for child, raw_child in zip(row[nested], [x for x in inner if isinstance(x, dict)]):
                    child["ident"] = _read_ident(raw_child)
    tag_ids = data.get("tag_ids")
    out["tag_ids"] = {str(k).lower(): _read_ident(v) for k, v in tag_ids.items()} if isinstance(tag_ids, dict) else {}


def export_data(user):
    profile = services.get_profile(user)
    cats = list(Category.objects.filter(user=user).select_related("parent"))
    tags = list(Tag.objects.filter(user=user))
    txs = (Transaction.objects.filter(user=user).select_related("category", "account").prefetch_related("tags")
           .order_by("date", "id"))
    return {
        "format": FORMAT, "version": VERSION, "exported_at": timezone.now().isoformat(),
        "profile": {
            "currency_code": profile.currency_code, "currency_symbol": profile.currency_symbol,
            "number_format": profile.number_format, "allow_negative_balance": profile.allow_negative_balance,
            "secondary_currency_code": profile.secondary_currency_code,
            "secondary_rate": str(profile.secondary_rate) if profile.secondary_rate is not None else None,
            "default_account": profile.default_account.name if profile.default_account_id else None,
            "card_date_mode": profile.card_date_mode,
        },
        "accounts": [{**_ident(a), "name": a.name, "type": a.type, "opening_balance": _d(a.opening_balance), "archived": a.archived,
                      "credit_limit": _d(a.credit_limit) if a.credit_limit is not None else None, "due_day": a.due_day,
                      "closing_day": a.closing_day}
                     for a in Account.objects.filter(user=user)],
        "categories": [{**_ident(c), "name": c.name, "kind": c.kind, "color": c.color, "icon": c.icon,
                        "parent": c.parent.name if c.parent_id else None} for c in cats],
        "tags": [t.name for t in tags],
        "tag_ids": {t.name: _ident(t) for t in tags},
        "rules": [{**_ident(r), "pattern": r.pattern, "category": r.category.name, "priority": r.priority, "active": r.active}
                  for r in CategoryRule.objects.filter(user=user).select_related("category")],
        "transactions": [{
            **_ident(t), "name": t.name, "amount": _d(t.amount), "date": t.date.isoformat(), "kind": t.kind, "category": t.category.name,
            "account": t.account.name if t.account_id else None, "payee": t.payee, "note": t.note,
            "tags": [x.name for x in t.tags.all()], "split_group": str(t.split_group) if t.split_group else None,
            "reconciled": t.reconciled} for t in txs],
        "transfers": [{**_ident(t), "from": t.from_account.name, "to": t.to_account.name, "amount": _d(t.amount), "date": t.date.isoformat(),
                       "note": t.note} for t in Transfer.objects.filter(user=user).select_related("from_account", "to_account")],
        "budgets": [{**_ident(b), "category": b.category.name if b.category_id else None, "month": b.month.strftime("%Y-%m"),
                     "amount": _d(b.amount), "rollover": b.rollover, "warn_percent": b.warn_percent}
                    for b in Budget.objects.filter(user=user).select_related("category")],
        "goals": [{**_ident(g), "name": g.name, "target_amount": _d(g.target_amount),
                   "target_date": g.target_date.isoformat() if g.target_date else None,
                   "account": g.account.name if g.account_id else None, "category": g.category.name if g.category_id else None,
                   "contributions": [{**_ident(c), "amount": _d(c.amount), "date": c.date.isoformat(), "note": c.note}
                                     for c in g.contributions.all()]}
                  for g in Goal.objects.filter(user=user).select_related("account", "category").prefetch_related("contributions")],
        "recurring": [{**_ident(r), "name": r.name, "amount": _d(r.amount), "kind": r.kind, "category": r.category.name, "payee": r.payee,
                       "account": r.account.name if r.account_id else None, "note": r.note, "frequency": r.frequency,
                       "start_date": r.start_date.isoformat(), "end_date": r.end_date.isoformat() if r.end_date else None,
                       "next_due": r.next_due.isoformat(), "active": r.active,
                       "installment_first": r.installment_first, "installment_total": r.installment_total}
                      for r in RecurringTransaction.objects.filter(user=user).select_related("category", "account")],
        "views": [{**_ident(v), "name": v.name, "query": v.query} for v in SavedView.objects.filter(user=user)],
        "locked_months": [m.month.strftime("%Y-%m") for m in MonthLock.objects.filter(user=user)],
        "people": [{**_ident(p), "name": p.name, "entries": [{**_ident(e), "kind": e.kind, "amount": _d(e.amount), "description": e.description,
                                                "date": e.date.isoformat(), "due_date": e.due_date.isoformat() if e.due_date else None,
                                                "account": e.account.name if e.account_id else None, "planned": e.planned,
                                                "group": str(e.group) if e.group else None}
                                               for e in p.entries.select_related("account")]}
                   for p in Person.objects.filter(user=user)],
    }


def dumps(user):
    return json.dumps(export_data(user), indent=2, ensure_ascii=False)


# --- validation ---------------------------------------------------------------

class _Checker:
    def __init__(self):
        self.problems = []

    def bad(self, where, text):
        if len(self.problems) < 25:
            self.problems.append(f"{where}: {text}")

    def text(self, row, key, where, maximum, required=True, default=""):
        value = row.get(key, default)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
            self.bad(where, f"{key} deve ser um texto de 1 a {maximum} caracteres" if required else f"{key} é longo demais")
            return default
        return value

    def money(self, row, key, where, positive=True, signed=False):
        try:
            value = Decimal(str(row.get(key)))
            if not value.is_finite() or abs(value) > MAX_AMOUNT * 10 or value != value.quantize(Decimal("0.01")):
                raise InvalidOperation
            if positive and value <= 0:
                raise InvalidOperation
            if not signed and not positive and value < 0:
                raise InvalidOperation
            return value
        except (InvalidOperation, ValueError, TypeError):
            self.bad(where, f"{key} não é um valor válido")
            return Decimal("1.00")

    def date(self, row, key, where, required=True):
        value = row.get(key)
        if value in (None, "") and not required:
            return None
        try:
            parsed = datetime.date.fromisoformat(value)
            if parsed < MIN_DATE or parsed > timezone.localdate() + datetime.timedelta(days=3660):
                raise ValueError
            return parsed
        except (ValueError, TypeError):
            self.bad(where, f"{key} deve ser uma data como 2025-03-31")
            return MIN_DATE

    def choice(self, row, key, where, choices):
        value = row.get(key)
        if value not in {c[0] for c in choices}:
            self.bad(where, f"{key} deve ser um destes: {', '.join(c[0] for c in choices)}")
        return value

    def month(self, value, where):
        try:
            parsed = datetime.date.fromisoformat(f"{value}-01")
            if parsed < MIN_DATE:
                raise ValueError
            return parsed
        except (ValueError, TypeError):
            self.bad(where, "o mês deve estar no formato 2025-03")
            return MIN_DATE


def parse(raw):
    """Decode and validate a backup. Returns a dict ready for :func:`restore`; raises BackupError."""
    if len(raw) > MAX_BYTES:
        raise BackupError("O arquivo é grande demais (limite de 8 MB).")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("Este não é um arquivo JSON válido.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("Este arquivo não é um backup do Finanças Pessoais.")
    if data.get("version") != VERSION:
        raise BackupError(f"Versão de backup não suportada: {data.get('version')!r}. Este app lê a versão {VERSION}.")
    c = _Checker()

    def rows(key):
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > 200000:
            c.bad(key, "deve ser uma lista")
            return []
        out = []
        for i, row in enumerate(value):
            if isinstance(row, dict):
                out.append((f"{key}[{i}]", row))
            else:
                c.bad(f"{key}[{i}]", "deve ser um objeto")
        return out

    out = {"profile": data.get("profile") if isinstance(data.get("profile"), dict) else {}}
    p = out["profile"]
    if p:
        c.text(p, "currency_code", "profile", 3)
        c.text(p, "currency_symbol", "profile", 5)
        if p.get("number_format") not in {n[0] for n in NUMBER_FORMATS}:
            c.bad("profile", "number_format não reconhecido")

    accounts = []
    for where, r in rows("accounts"):
        accounts.append({"name": c.text(r, "name", where, 60), "type": c.choice(r, "type", where, ACCOUNT_TYPES),
                         "opening_balance": c.money(r, "opening_balance", where, positive=False, signed=True),
                         "archived": bool(r.get("archived", False)),
                         "credit_limit": _limit(r.get("credit_limit")), "due_day": _day(r.get("due_day")),
                         "closing_day": _day(r.get("closing_day"))})
    names = [a["name"].lower() for a in accounts]
    if len(set(names)) != len(names):
        c.bad("accounts", "os nomes das contas devem ser únicos")
    out["accounts"] = accounts
    acct_names = set(names)

    categories = []
    for where, r in rows("categories"):
        icon = r.get("icon", "tag")
        categories.append({"name": c.text(r, "name", where, 50), "kind": c.choice(r, "kind", where, CATEGORY_KIND_CHOICES),
                           "color": c.choice(r, "color", where, COLOR_CHOICES), "icon": icon if icon in CATEGORY_ICONS else "tag",
                           "parent": r.get("parent")})
    cat_names = [x["name"].lower() for x in categories]
    if len(set(cat_names)) != len(cat_names):
        c.bad("categories", "os nomes das categorias devem ser únicos")
    for x in categories:
        if x["parent"] is not None and (not isinstance(x["parent"], str) or x["parent"].lower() not in cat_names):
            c.bad("categories", f"a categoria pai de {x['name']!r} não existe")
    out["categories"] = categories
    cat_set = set(cat_names)

    def cat_ref(r, key, where, required=True):
        value = r.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or value.lower() not in cat_set:
            c.bad(where, f"categoria desconhecida {value!r}")
        return value

    def acct_ref(r, key, where):
        value = r.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or value.lower() not in acct_names:
            c.bad(where, f"conta desconhecida {value!r}")
        return value

    tags = data.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() or len(t) > 30 for t in tags):
        c.bad("tags", "deve ser uma lista de nomes de até 30 caracteres")
        tags = []
    out["tags"] = tags

    out["rules"] = [{"pattern": c.text(r, "pattern", w, 60), "category": cat_ref(r, "category", w),
                     "priority": int(r.get("priority", 100)) if isinstance(r.get("priority", 100), int) else 100,
                     "active": bool(r.get("active", True))} for w, r in rows("rules")]

    txs = []
    for where, r in rows("transactions"):
        t_tags = r.get("tags", [])
        if not isinstance(t_tags, list) or any(not isinstance(x, str) or len(x) > 30 for x in t_tags):
            c.bad(where, "tags deve ser uma lista de nomes")
            t_tags = []
        txs.append({"name": c.text(r, "name", where, 120), "amount": c.money(r, "amount", where),
                    "date": c.date(r, "date", where), "kind": c.choice(r, "kind", where, (("expense",), ("income",))),
                    "category": cat_ref(r, "category", where), "account": acct_ref(r, "account", where),
                    "payee": c.text(r, "payee", where, 80, required=False), "note": c.text(r, "note", where, 1000, required=False),
                    "tags": t_tags, "split_group": r.get("split_group"), "reconciled": bool(r.get("reconciled", False))})
    out["transactions"] = txs

    out["transfers"] = [{"from": acct_ref(r, "from", w), "to": acct_ref(r, "to", w), "amount": c.money(r, "amount", w),
                         "date": c.date(r, "date", w), "note": c.text(r, "note", w, 200, required=False)}
                        for w, r in rows("transfers")]
    out["budgets"] = [{"category": cat_ref(r, "category", w, required=False), "month": c.month(r.get("month"), w),
                       "amount": c.money(r, "amount", w), "rollover": bool(r.get("rollover", False)),
                       "warn_percent": min(99, max(1, int(r.get("warn_percent", 80)))) if isinstance(r.get("warn_percent", 80), int) else 80}
                      for w, r in rows("budgets")]
    goals = []
    for w, r in rows("goals"):
        contribs = r.get("contributions", [])
        if not isinstance(contribs, list):
            c.bad(w, "contributions deve ser uma lista")
            contribs = []
        goals.append({"name": c.text(r, "name", w, 80), "target_amount": c.money(r, "target_amount", w),
                      "target_date": c.date(r, "target_date", w, required=False),
                      "account": acct_ref(r, "account", w), "category": cat_ref(r, "category", w, required=False),
                      "contributions": [{"amount": c.money(x, "amount", w, positive=False, signed=True),
                                         "date": c.date(x, "date", w), "note": c.text(x, "note", w, 200, required=False)}
                                        for x in contribs if isinstance(x, dict)]})
    out["goals"] = goals
    out["recurring"] = [{"name": c.text(r, "name", w, 120), "amount": c.money(r, "amount", w),
                         "kind": c.choice(r, "kind", w, (("expense",), ("income",))), "category": cat_ref(r, "category", w),
                         "payee": c.text(r, "payee", w, 80, required=False), "account": acct_ref(r, "account", w),
                         "note": c.text(r, "note", w, 1000, required=False),
                         "frequency": c.choice(r, "frequency", w, (("weekly",), ("monthly",), ("once",))),
                         "start_date": c.date(r, "start_date", w), "end_date": c.date(r, "end_date", w, required=False),
                         "next_due": c.date(r, "next_due", w), "active": bool(r.get("active", True)),
                         "installment_first": _small_int(r.get("installment_first")),
                         "installment_total": _small_int(r.get("installment_total"))}
                        for w, r in rows("recurring")]
    out["views"] = [{"name": c.text(r, "name", w, 60), "query": c.text(r, "query", w, 500, required=False)} for w, r in rows("views")]
    locks = data.get("locked_months", [])
    out["locked_months"] = [c.month(m, "locked_months") for m in locks] if isinstance(locks, list) else []
    people = []
    for w, r in rows("people"):
        entries = r.get("entries", [])
        entries = entries if isinstance(entries, list) else []
        people.append({"name": c.text(r, "name", w, 60), "entries": [{
            "kind": c.choice(x, "kind", w, LEDGER_KINDS), "amount": c.money(x, "amount", w, positive=False, signed=True),
            "description": c.text(x, "description", w, 120, required=False), "date": c.date(x, "date", w),
            "due_date": c.date(x, "due_date", w, required=False), "account": acct_ref(x, "account", w),
            "planned": bool(x.get("planned", False)), "group": _uuid(x.get("group"))}
            for x in entries if isinstance(x, dict)]})
    out["people"] = people
    if c.problems:
        raise BackupError(c.problems)
    _attach_idents(data, out)
    return out


def summary(parsed):
    return {key: len(parsed[key]) for key in ("accounts", "categories", "transactions", "transfers", "budgets", "goals",
                                              "recurring", "people", "tags", "rules", "views")}


def erase(user):
    """Remove every record the user owns (the account itself stays)."""
    for model in (Transfer, Budget, CategoryRule, RecurringTransaction, SavedView, MonthLock, Person):
        model.objects.filter(user=user).delete()
    Goal.objects.filter(user=user).delete()
    Transaction.all_objects.filter(user=user).delete()
    AuditEntry.objects.filter(user=user).delete()
    Category.objects.filter(user=user).update(parent=None)
    Category.objects.filter(user=user).delete()
    Tag.objects.filter(user=user).delete()
    Account.objects.filter(user=user).delete()


@transaction.atomic
def restore(user, parsed):
    """Replace all of the user's data with the validated backup."""
    used = set()

    def keep(model, item):
        """uid and version from the file, unless that uid is already taken (another account)."""
        ident = (item or {}).get("ident") or {}
        uid = ident.get("uid")
        if not uid or (model, uid) in used or model._base_manager.filter(uid=uid).exists():
            return {}
        used.add((model, uid))
        return ident

    with services.locked(user):
        erase(user)
        p = parsed["profile"]
        profile = services.get_profile(user)
        if p:
            profile.currency_code = p.get("currency_code", profile.currency_code).upper()[:3]
            profile.currency_symbol = p.get("currency_symbol", profile.currency_symbol)
            profile.number_format = p.get("number_format", profile.number_format)
            profile.allow_negative_balance = p.get("allow_negative_balance") if isinstance(p.get("allow_negative_balance"), bool) else None
            profile.secondary_currency_code = (p.get("secondary_currency_code") or "")[:3].upper()
            try:
                rate = Decimal(str(p.get("secondary_rate"))) if p.get("secondary_rate") else None
            except InvalidOperation:
                rate = None
            profile.secondary_rate = rate if rate and rate > 0 else None
            profile.card_date_mode = p.get("card_date_mode") if p.get("card_date_mode") in ("invoice", "purchase") else "invoice"
        profile.setup_done = True
        profile.save()
        accounts = {a["name"].lower(): Account.objects.create(user=user, name=a["name"], type=a["type"],
                                                              opening_balance=a["opening_balance"], archived=a["archived"],
                                                              credit_limit=a["credit_limit"], due_day=a["due_day"],
                                                              closing_day=a["closing_day"], **keep(Account, a))
                    for a in parsed["accounts"]}
        wanted = (p or {}).get("default_account")
        if isinstance(wanted, str) and wanted.lower() in accounts:
            profile.default_account = accounts[wanted.lower()]
            profile.save(update_fields=["default_account"])
        cats = {c["name"].lower(): Category.objects.create(user=user, name=c["name"], kind=c["kind"], color=c["color"], icon=c["icon"],
                                                         **keep(Category, c))
                for c in parsed["categories"]}
        for c in parsed["categories"]:
            if c["parent"]:
                parent = cats[c["parent"].lower()]
                child = cats[c["name"].lower()]
                if parent.pk != child.pk and not parent.parent_id:
                    # Keeps the version from the file (the parent is set in a second step).
                    Category.objects.filter(pk=child.pk).update(parent=parent, version=child.version)
        tag_ids = parsed.get("tag_ids") or {}
        tags = {t.lower(): Tag.objects.create(user=user, name=t, **keep(Tag, {"ident": tag_ids.get(t.lower())}))
                for t in dict.fromkeys(parsed["tags"])}
        for r in parsed["rules"]:
            CategoryRule.objects.create(user=user, pattern=r["pattern"], category=cats[r["category"].lower()],
                                        priority=r["priority"], active=r["active"], **keep(CategoryRule, r))
        import uuid
        groups = {}
        for t in parsed["transactions"]:
            group = None
            if t["split_group"]:
                group = groups.setdefault(t["split_group"], uuid.uuid4())
            tx = Transaction.objects.create(
                user=user, name=t["name"], amount=t["amount"], date=t["date"], kind=t["kind"], category=cats[t["category"].lower()],
                account=accounts.get(t["account"].lower()) if t["account"] else None, payee=t["payee"], note=t["note"],
                split_group=group, reconciled=t["reconciled"], **keep(Transaction, t))
            if t["tags"]:
                found = []
                for name in t["tags"]:
                    tag = tags.get(name.lower()) or tags.setdefault(name.lower(), Tag.objects.create(user=user, name=name))
                    found.append(tag)
                tx.tags.set(found)
        for t in parsed["transfers"]:
            Transfer.objects.create(user=user, from_account=accounts[t["from"].lower()], to_account=accounts[t["to"].lower()],
                                    amount=t["amount"], date=t["date"], note=t["note"], **keep(Transfer, t))
        for b in parsed["budgets"]:
            Budget.objects.create(user=user, category=cats[b["category"].lower()] if b["category"] else None, month=b["month"],
                                  amount=b["amount"], rollover=b["rollover"], warn_percent=b["warn_percent"], **keep(Budget, b))
        for g in parsed["goals"]:
            goal = Goal.objects.create(user=user, name=g["name"], target_amount=g["target_amount"], target_date=g["target_date"],
                                       account=accounts.get(g["account"].lower()) if g["account"] else None,
                                       category=cats.get(g["category"].lower()) if g["category"] else None, **keep(Goal, g))
            for x in g["contributions"]:
                Contribution.objects.create(goal=goal, amount=x["amount"], date=x["date"], note=x["note"], **keep(Contribution, x))
        for r in parsed["recurring"]:
            RecurringTransaction.objects.create(
                user=user, name=r["name"], amount=r["amount"], kind=r["kind"], category=cats[r["category"].lower()], payee=r["payee"],
                account=accounts.get(r["account"].lower()) if r["account"] else None, note=r["note"], frequency=r["frequency"],
                start_date=r["start_date"], end_date=r["end_date"], next_due=r["next_due"], active=r["active"],
                installment_first=r["installment_first"], installment_total=r["installment_total"],
                **keep(RecurringTransaction, r))
        for v in parsed["views"]:
            SavedView.objects.get_or_create(user=user, name=v["name"], defaults={"query": v["query"], **keep(SavedView, v)})
        for m in parsed["locked_months"]:
            MonthLock.objects.get_or_create(user=user, month=m)
        for person in parsed["people"]:
            obj = Person.objects.create(user=user, name=person["name"], **keep(Person, person))
            for e in person["entries"]:
                LedgerEntry.objects.create(person=obj, kind=e["kind"], amount=e["amount"], description=e["description"],
                                           date=e["date"], due_date=e["due_date"], planned=e["planned"], group=e["group"],
                                           account=accounts.get(e["account"].lower()) if e["account"] else None,
                                           **keep(LedgerEntry, e))
        if not Account.objects.filter(user=user).exists():
            services.default_account(user)
        # Phones download everything again: restored versions may be lower than theirs.
        from django.db.models import F
        Profile.objects.filter(user=user).update(sync_epoch=F("sync_epoch") + 1)
    services.forget_profile(user)

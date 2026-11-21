"""What is synchronised and how: the models in dependency order, their fields, how each value
travels (FKs as the target's uid, tags as a sorted list of uids) and the natural keys used to
join objects created on both sides ("Mercado" created offline on the phone and on the site).

An object travels as an *item*: {"uid", "version", "updated_at", "updated_by", "data"}.
Unknown fields are ignored on both sides, so a phone with an older app keeps working when the
server gains a field (new fields always have a default).
"""
import datetime
import uuid
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import models
from django.db.models.functions import Lower

# Kept out of the data: identity and bookkeeping travel beside it, the rest is local to each side.
SKIP = {"id", "user", "uid", "version", "updated_at", "updated_by"}
SKIP_BY_MODEL = {"profile": {"sync_seq", "plan", "sync_epoch"}}

# Fields that only make sense together count as one in the 3-way merge.
GROUPS = {
    "transaction": [("date", "purchase_date", "account"), ("recurring", "recurring_date"), ("amount", "kind")],
}

# Natural keys: an object created on both sides with the same key is the same object (remap).
# "name" keys are case-insensitive; models that are more than a name report a conflict when the
# values differ (a budget of 500 on one side and 600 on the other).
NATURAL = {
    "category": ("name",), "account": ("name",), "tag": ("name",), "person": ("name",), "savedview": ("name",),
    "budget": ("category", "month"), "monthlock": ("month",), "recurringmatch": ("recurring", "slot", "transaction"),
    "profile": (),
}
NAME_ONLY = {"category", "account", "tag", "person", "savedview"}

# Through the parent (no user column).
OWNER = {"contribution": "goal", "ledgerentry": "person", "recurringmatch": "recurring"}


class MissingRef(Exception):
    def __init__(self, model, uid):
        super().__init__(f"{model} {uid}")
        self.model, self.uid = model, uid


class Spec:
    def __init__(self, model):
        self.model = model
        self.name = model._meta.model_name
        skip = SKIP | SKIP_BY_MODEL.get(self.name, set())
        self.fields = []      # plain values
        self.fks = {}         # field name -> target model name
        self.m2m = {}
        for field in model._meta.get_fields():
            if field.auto_created and not field.concrete:
                continue
            if field.name in skip:
                continue
            if field.many_to_many:
                self.m2m[field.name] = field.related_model._meta.model_name
            elif field.is_relation and field.concrete:
                self.fks[field.name] = field.related_model._meta.model_name
            elif field.concrete:
                self.fields.append(field.name)
        self.groups = GROUPS.get(self.name, [])
        self.natural = NATURAL.get(self.name)
        self.owner_parent = OWNER.get(self.name)

    @property
    def manager(self):
        return getattr(self.model, "all_objects", None) or self.model._default_manager

    def owned(self, user):
        if self.owner_parent:
            return self.manager.filter(**{f"{self.owner_parent}__user": user})
        if self.name == "profile":
            return self.manager.filter(user=user)
        return self.manager.filter(user=user)

    def all_keys(self):
        return self.fields + list(self.fks) + list(self.m2m)

    def group_of(self, key):
        for group in self.groups:
            if key in group:
                return group
        return (key,)


def _order(specs):
    """Topological order by FK (targets first); self references (Category.parent) ignored."""
    by_name = {s.name: s for s in specs}
    done, out = set(), []

    def visit(name, path=()):
        if name in done:
            return
        if name in path:
            raise RuntimeError(f"Ciclo de FKs na sincronização: {' -> '.join(path + (name,))}")
        for target in list(by_name[name].fks.values()) + list(by_name[name].m2m.values()):
            if target in by_name and target != name:
                visit(target, path + (name,))
        done.add(name)
        out.append(by_name[name])

    for spec in sorted(specs, key=lambda s: s.name):
        visit(spec.name)
    return out


_SPECS = None


def specs():
    global _SPECS
    if _SPECS is None:
        from .fields import SyncModel
        models_ = [m for m in apps.get_app_config("app").get_models() if issubclass(m, SyncModel)]
        _SPECS = _order([Spec(m) for m in models_])
    return _SPECS


def by_name():
    return {s.name: s for s in specs()}


def get(name):
    spec = by_name().get(name)
    if spec is None:
        raise KeyError(name)
    return spec


def rank(name):
    order = [s.name for s in specs()]
    return order.index(name) if name in order else len(order)


# --- values ----------------------------------------------------------------------------------

def encode(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def decode(field, value):
    if value is None:
        return None
    try:
        if isinstance(field, models.DecimalField):
            return Decimal(str(value))
        return field.to_python(value)
    except (InvalidOperation, ValueError, TypeError):
        from django.core.exceptions import ValidationError
        raise ValidationError({field.name: [f"Valor inválido: {value!r}"]}) from None


class UidMap:
    """pk <-> uid of the related rows of one user, loaded once per model (no query per row)."""

    def __init__(self, user):
        self.user = user
        self._to_uid = {}
        self._to_pk = {}
        self.remapped = {}    # (model name, uid sent) -> uid on this side (remaps of this request)

    def _load(self, name):
        if name not in self._to_uid:
            spec = get(name)
            rows = list(spec.owned(self.user).values_list("pk", "uid"))
            self._to_uid[name] = {pk: str(uid) for pk, uid in rows}
            self._to_pk[name] = {str(uid): pk for pk, uid in rows}

    def uid(self, name, pk):
        if pk is None:
            return None
        self._load(name)
        if pk not in self._to_uid[name]:
            found = get(name).manager.filter(pk=pk).values_list("uid", flat=True).first()
            if found is None:
                return None
            self._to_uid[name][pk] = str(found)
        return self._to_uid[name][pk]

    def pk(self, name, uid):
        if uid in (None, ""):
            return None
        uid = self.remapped.get((name, str(uid)), str(uid))
        self._load(name)
        pk = self._to_pk[name].get(uid)
        if pk is None:
            row = get(name).owned(self.user).filter(uid=uid).values_list("pk", flat=True).first()
            if row is None:
                raise MissingRef(name, uid)
            self._to_pk[name][uid] = pk = row
        return pk

    def learn(self, name, obj):
        self._load(name)
        self._to_uid[name][obj.pk] = str(obj.uid)
        self._to_pk[name][str(obj.uid)] = obj.pk

    def forget(self, name, uid):
        if name in self._to_pk:
            pk = self._to_pk[name].pop(str(uid), None)
            self._to_uid[name].pop(pk, None)


def serialize(obj, uids):
    spec = get(obj._meta.model_name)
    data = {}
    for name in spec.fields:
        data[name] = encode(getattr(obj, name))
    for name, target in spec.fks.items():
        data[name] = uids.uid(target, getattr(obj, f"{name}_id"))
    for name, target in spec.m2m.items():
        data[name] = sorted(str(u) for u in getattr(obj, name).values_list("uid", flat=True))
    return data


def item(obj, uids):
    return {"uid": str(obj.uid), "version": obj.version, "updated_at": encode(obj.updated_at),
            "updated_by": obj.updated_by, "data": serialize(obj, uids)}


def deserialize(spec, data, uids):
    """(plain field values with FKs as pks, {m2m name: [pks]}). Unknown keys are ignored."""
    values, m2m = {}, {}
    for name in spec.fields:
        if name in data:
            values[name] = decode(spec.model._meta.get_field(name), data[name])
    for name, target in spec.fks.items():
        if name in data:
            values[f"{name}_id"] = uids.pk(target, data[name])
    for name, target in spec.m2m.items():
        if name in data and isinstance(data[name], list):
            m2m[name] = [uids.pk(target, u) for u in data[name]]
    return values, m2m


def natural_match(spec, values, user):
    """The object of this user with the same natural key, or None."""
    if spec.natural is None:
        return None
    qs = spec.owned(user)
    if spec.name == "profile":
        return qs.first()
    lookup = {}
    for key in spec.natural:
        if key == "name":
            if "name" not in values:
                return None
            qs = qs.annotate(_lname=Lower("name"))
            lookup["_lname"] = str(values["name"]).lower()
        elif key in spec.fks:
            lookup[f"{key}_id"] = values.get(f"{key}_id")
        else:
            if key not in values:
                return None
            lookup[key] = values[key]
    return qs.filter(**lookup).first()

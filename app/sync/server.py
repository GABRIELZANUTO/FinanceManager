"""Server side of the synchronisation: push (apply a phone's changes), pull (what changed since a
cursor), snapshot (everything, in pages) and object (one item).

Push writes row by row: each derived object (ledger entries, recurring links, split parts) comes
as an operation of its own, so the business services that create side effects are NOT called
here (that would duplicate them). Validation: full_clean() plus the month lock.
"""
import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils import timezone

from app.models import MonthLock, Profile, SyncChange, SyncReceipt
from app.services import month_start

from . import spec as specs
from .spec import MissingRef, UidMap

MAX_OPS = 200
RECEIPT_DAYS = 30


def _result(op, status, **extra):
    return {"op_id": str(op.get("op_id")), "status": status, **extra}


def _locked_months(user):
    return set(MonthLock.objects.filter(user=user).values_list("month", flat=True))


def _month_closed(spec, values, obj, locks):
    if spec.name != "transaction" or not locks:
        return False
    days = [values.get("date")] + ([obj.date] if obj is not None else [])
    return any(day and month_start(day) in locks for day in days)


def _save(spec, obj, values, m2m, user):
    for key, value in values.items():
        setattr(obj, key, value)
    if not spec.owner_parent and hasattr(obj, "user_id"):
        obj.user = user
    exclude = [f.name for f in spec.model._meta.fields if f.name in ("user",)]
    obj.full_clean(exclude=exclude, validate_unique=False)
    obj.save()
    for name, pks in m2m.items():
        getattr(obj, name).set(pks)
    return spec.manager.get(pk=obj.pk)


def _merge(spec, base, server_now, mine):
    """3-way merge by field (or group). Returns (merged data, conflicting fields)."""
    merged, conflicts = dict(server_now), []
    keys = [k for k in spec.all_keys() if k in mine]
    seen = set()
    for key in keys:
        group = spec.group_of(key)
        if group in seen:
            continue
        seen.add(group)
        group = [k for k in group if k in mine]
        if spec.name == "transaction" and group == ["tags"]:
            continue
        local_changed = any(_norm(mine.get(k)) != _norm(base.get(k)) for k in group)
        server_changed = any(_norm(server_now.get(k)) != _norm(base.get(k)) for k in group)
        if local_changed and server_changed and any(_norm(mine.get(k)) != _norm(server_now.get(k)) for k in group):
            conflicts.extend(group)
        elif local_changed:
            for k in group:
                merged[k] = mine.get(k)
    if "tags" in mine and "tags" in spec.m2m:
        # Sets: apply on the server's set what the phone added and removed (never a conflict).
        base_tags, mine_tags = set(base.get("tags") or []), set(mine.get("tags") or [])
        merged["tags"] = sorted((set(server_now.get("tags") or []) | (mine_tags - base_tags)) - (base_tags - mine_tags))
    return merged, conflicts


def _norm(value):
    if isinstance(value, list):
        return tuple(sorted(str(v) for v in value))
    return None if value in ("", None) else str(value)


def apply_op(user, op, uids, locks):
    name = op.get("model")
    try:
        spec = specs.get(name)
    except KeyError:
        return _result(op, "rejected", code="invalid", message=f"Tipo desconhecido: {name!r}.")
    uid = str(op.get("uid") or "")
    action = op.get("action", "upsert")
    force = op.get("force") is True
    base_version = op.get("base_version")
    obj = spec.owned(user).filter(uid=uid).first()

    if action == "delete":
        if obj is None:
            return _result(op, "applied", item={"uid": uid})
        if obj.version != base_version and not force:
            return _result(op, "conflict", reason="changed_on_server", fields=[], item=specs.item(obj, uids))
        if _month_closed(spec, {}, obj, locks):
            return _result(op, "rejected", code="month_locked", message="O mês desta transação está fechado.")
        try:
            obj.delete()
        except (ProtectedError, RestrictedError):
            return _result(op, "rejected", code="in_use", message="Este registro ainda é usado por outros e não foi apagado.")
        uids.forget(name, uid)
        return _result(op, "applied", item={"uid": uid})

    data = op.get("data") if isinstance(op.get("data"), dict) else {}
    try:
        values, m2m = specs.deserialize(spec, data, uids)
    except MissingRef as missing:
        nullable = [k for k, target in spec.fks.items() if target == missing.model and data.get(k) == missing.uid
                    and spec.model._meta.get_field(k).null]
        if not nullable:
            return _result(op, "rejected", code="missing_ref", message="Depende de um registro que não existe no servidor.",
                           ref={"model": missing.model, "uid": missing.uid})
        data = {**data, **{k: None for k in nullable}}
        return apply_op(user, {**op, "data": data, "_warning": "Um vínculo apagado no servidor ficou vazio."}, uids, locks)
    except ValidationError as exc:
        return _result(op, "rejected", code="invalid", message=" ".join(exc.messages)[:300])
    warning = op.get("_warning")

    if _month_closed(spec, values, obj, locks):
        return _result(op, "rejected", code="month_locked", message="O mês desta transação está fechado.")

    try:
        if obj is None:
            if spec.manager.filter(uid=uid).exists():
                return _result(op, "rejected", code="uid_taken", message="Identificador já usado.")
            deleted = SyncChange.objects.filter(user=user, model=name, uid=uid, deleted=True).exists()
            if (deleted or base_version is not None) and not force:
                return _result(op, "conflict", reason="deleted_on_server", fields=[], item={"uid": uid})
            same = specs.natural_match(spec, values, user) if not force else None
            if same is not None:
                uids.remapped[(name, uid)] = str(same.uid)
                extra = {}
                if name not in specs.NAME_ONLY and name != "profile":
                    differs = [k for k in specs.serialize(same, uids) if k in data and _norm(data[k]) != _norm(specs.serialize(same, uids)[k])]
                    if differs:
                        extra["conflict"] = {"reason": "duplicate", "fields": differs}
                return _result(op, "remap", item=specs.item(same, uids), **extra)
            obj = spec.model(uid=uid)
            saved = _save(spec, obj, values, m2m, user)
            uids.learn(name, saved)
            return _result(op, "applied", item=specs.item(saved, uids), **({"warning": warning} if warning else {}))

        if obj.version == base_version or force:
            saved = _save(spec, obj, values, m2m, user)
            return _result(op, "applied", item=specs.item(saved, uids), **({"warning": warning} if warning else {}))
        if base_version is None:
            # The same recurring occurrence posted on both sides (same uid).
            if obj.version == 1:
                saved = _save(spec, obj, values, m2m, user)
                return _result(op, "applied", item=specs.item(saved, uids))
            return _result(op, "conflict", reason="same_field", fields=list(data), item=specs.item(obj, uids))
        server_now = specs.serialize(obj, uids)
        merged, conflicts = _merge(spec, op.get("base") or {}, server_now, data)
        if conflicts:
            return _result(op, "conflict", reason="same_field", fields=conflicts, item=specs.item(obj, uids))
        values, m2m = specs.deserialize(spec, merged, uids)
        try:
            with transaction.atomic():
                saved = _save(spec, obj, values, m2m, user)
        except ValidationError:
            obj = spec.manager.get(pk=obj.pk)
            return _result(op, "conflict", reason="same_field", fields=list(data), item=specs.item(obj, uids))
        return _result(op, "merged", item=specs.item(saved, uids), **({"warning": warning} if warning else {}))
    except ValidationError as exc:
        return _result(op, "rejected", code="invalid", message=" ".join(exc.messages)[:300])
    except MissingRef as missing:
        return _result(op, "rejected", code="missing_ref", message="Depende de um registro que não existe no servidor.",
                       ref={"model": missing.model, "uid": missing.uid})
    except IntegrityError:
        return _result(op, "rejected", code="invalid", message="O registro não respeita uma regra de dados (duplicado ou inválido).")


def push(user, ops):
    """Apply each operation in its own savepoint, with its receipt in the same savepoint."""
    uids = UidMap(user)
    locks = _locked_months(user)
    results = []
    for op in ops:
        if not isinstance(op, dict) or not op.get("op_id"):
            results.append({"op_id": None, "status": "rejected", "code": "invalid", "message": "Operação sem op_id."})
            continue
        done = SyncReceipt.objects.filter(user=user, op_id=op["op_id"]).first()
        if done is not None:
            results.append(done.result)
            continue
        try:
            with transaction.atomic():
                result = apply_op(user, op, uids, locks)
                SyncReceipt.objects.create(user=user, op_id=op["op_id"], result=result)
        except (ValidationError, IntegrityError) as exc:
            result = _result(op, "rejected", code="invalid", message=str(exc)[:300])
        if result.get("status") == "remap":
            uids.remapped[(op.get("model"), str(op.get("uid")))] = result["item"]["uid"]
        results.append(result)
    SyncReceipt.objects.filter(user=user, created_at__lt=timezone.now() - datetime.timedelta(days=RECEIPT_DAYS)).delete()
    return results


def pull(user, cursor, limit):
    rows = list(SyncChange.objects.filter(user=user, seq__gt=cursor).order_by("seq")[:limit])
    uids = UidMap(user)
    wanted = {}
    for row in rows:
        if not row.deleted:
            wanted.setdefault(row.model, set()).add(row.uid)
    objects = {}
    for name, uid_set in wanted.items():
        try:
            spec = specs.get(name)
        except KeyError:
            continue
        for obj in spec.owned(user).filter(uid__in=uid_set):
            objects[(name, str(obj.uid))] = obj
    changes = []
    for row in rows:
        key = (row.model, str(row.uid))
        if row.deleted:
            changes.append({"model": row.model, "seq": row.seq, "deleted": True, "item": {"uid": str(row.uid)}})
        elif key in objects:
            changes.append({"model": row.model, "seq": row.seq, "deleted": False, "item": specs.item(objects[key], uids)})
        # gone between the two reads: its tombstone comes with a later number
    return {"changes": changes, "next_cursor": rows[-1].seq if rows else cursor, "has_more": len(rows) == limit}


def snapshot_start(user):
    profile = Profile.objects.get(user=user)
    return {"epoch": profile.sync_epoch, "cursor": profile.sync_seq, "models": [s.name for s in specs.specs()],
            "counts": {s.name: s.owned(user).count() for s in specs.specs()}}


def snapshot_page(user, name, after, limit):
    spec = specs.get(name)
    uids = UidMap(user)
    qs = spec.owned(user).order_by("pk")
    if "parent" in spec.fks:
        # Few rows; parents first so a child never arrives before its parent.
        rows = sorted(qs, key=lambda o: (o.parent_id is not None, o.pk))
        return {"items": [specs.item(o, uids) for o in rows], "next_after": None, "has_more": False}
    rows = list(qs.filter(pk__gt=after)[:limit])
    return {"items": [specs.item(o, uids) for o in rows], "next_after": rows[-1].pk if rows else after,
            "has_more": len(rows) == limit}


def one(user, name, uid):
    spec = specs.get(name)
    obj = spec.owned(user).filter(uid=uid).first()
    return specs.item(obj, UidMap(user)) if obj is not None else None

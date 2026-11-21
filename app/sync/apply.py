"""Writing on the phone what came from the server. Always inside remote_writes(): no new version,
no outbox row; version, updated_at and updated_by are written exactly as the server sent them."""
import datetime

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from app.models import SyncConflict, SyncOutbox

from . import spec as specs
from .capture import remote_writes
from .spec import UidMap


def _when(value):
    parsed = parse_datetime(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, datetime.datetime) else None


def _local_object(spec, uid, user):
    if spec.name == "profile":
        return spec.owned(user).first()
    return spec.manager.filter(uid=uid).first()


def apply_remote(user, name, item, deleted=False, uids=None):
    """Upsert (or delete) one item. Raises spec.MissingRef if it points to something not here yet."""
    spec = specs.get(name)
    uids = uids or UidMap(user)
    with remote_writes():
        obj = _local_object(spec, item["uid"], user)
        if deleted:
            if obj is not None and spec.name != "profile":
                obj.delete()
                uids.forget(name, item["uid"])
            return None
        values, m2m = specs.deserialize(spec, item.get("data") or {}, uids)
        meta = {"uid": item["uid"], "version": item.get("version") or 1, "updated_by": (item.get("updated_by") or "")[:60]}
        when = _when(item.get("updated_at"))
        if when:
            meta["updated_at"] = when
        if obj is None and spec.name in specs.NAME_ONLY:
            # The same name already here (made on this phone, e.g. the default account created again
            # by a screen): it is the same thing; it takes the server's identity and values.
            same = specs.natural_match(spec, values, user)
            if same is not None:
                SyncOutbox.objects.filter(model=name, uid=same.uid).delete()
                spec.manager.filter(pk=same.pk).update(uid=item["uid"])
                uids.forget(name, same.uid)
                obj = spec.manager.get(pk=same.pk)
        if obj is None:
            obj = spec.model(**{k: v for k, v in values.items()})
            if not spec.owner_parent and any(f.name == "user" for f in spec.model._meta.fields):
                obj.user = user
            obj.uid = item["uid"]
            obj.save()
        else:
            # update() skips save(): no auto_now, no version bump (remote_writes), no validation
            # (the server already validated).
            spec.manager.filter(pk=obj.pk).update(**values)
        spec.manager.filter(pk=obj.pk).update(**meta)
        for field, pks in m2m.items():
            getattr(spec.manager.get(pk=obj.pk), field).set(pks)
        obj = spec.manager.get(pk=obj.pk)
        uids.learn(name, obj)
        return obj


def remap_local(user, name, local_uid, item):
    """The server already had this object (same name, or the user's profile): use its uid here."""
    spec = specs.get(name)
    server_uid = item["uid"]
    with remote_writes():
        mine = spec.manager.filter(uid=local_uid).first()
        theirs = spec.manager.filter(uid=server_uid).first()
        if mine is not None and theirs is None:
            spec.manager.filter(pk=mine.pk).update(uid=server_uid)
        elif mine is not None and theirs is not None and mine.pk != theirs.pk:
            _repoint(name, mine, theirs)
            mine.delete()
        SyncOutbox.objects.filter(model=name, uid=local_uid).update(uid=server_uid)
        SyncConflict.objects.filter(model=name, uid=local_uid).update(uid=server_uid)
    return apply_remote(user, name, item)


def _repoint(name, old, new):
    """Every local reference to ``old`` now points to ``new``; rows that would duplicate go."""
    for spec in specs.specs():
        for field, target in spec.fks.items():
            if target != name:
                continue
            for row in spec.manager.filter(**{f"{field}_id": old.pk}):
                try:
                    with transaction.atomic():
                        spec.manager.filter(pk=row.pk).update(**{f"{field}_id": new.pk})
                except IntegrityError:
                    row.delete()
        for field, target in spec.m2m.items():
            if target != name:
                continue
            through = getattr(spec.model, field).through
            column = f"{target}_id"
            for link in through.objects.filter(**{column: old.pk}):
                if through.objects.filter(**{column: new.pk, f"{spec.name}_id": getattr(link, f"{spec.name}_id")}).exists():
                    link.delete()
                else:
                    through.objects.filter(pk=link.pk).update(**{column: new.pk})


def wipe_local(user):
    """Remove the user's data on this phone (the profile row stays), without queueing anything."""
    from app import backup
    with remote_writes():
        backup.erase(user)
        from app.models import CardInvoice, MonthLock
        CardInvoice.objects.filter(user=user).delete()
        MonthLock.objects.filter(user=user).delete()
        SyncOutbox.objects.all().delete()
        SyncConflict.objects.all().delete()

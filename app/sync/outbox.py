"""The phone's outbox: what changed here and is still to be sent.

One row per object, coalesced: the data is read from the database when it is sent, so editing
again only keeps the row. ``base`` is the object as the server last knew it, captured before the
first local change (see capture.before_change), so the server can merge field by field.
"""
from django.db import transaction

from app.models import SyncOutbox

from . import spec as specs
from .spec import UidMap

WAITING = ("pending", "sent", "conflict", "rejected", "blocked")


def _schedule():
    from . import client
    transaction.on_commit(lambda: client.request_sync("local", delay=10))


def ensure_base(model, pk):
    name = model._meta.model_name
    obj = model._base_manager.filter(pk=pk).first()
    if obj is None or SyncOutbox.objects.filter(model=name, uid=obj.uid).exists():
        return
    from .capture import owner_of
    SyncOutbox.objects.create(model=name, uid=obj.uid, action="upsert", base_version=obj.version,
                              base=specs.serialize(obj, UidMap(owner_of(obj))))


def enqueue(name, uid, deleted):
    rows = list(SyncOutbox.objects.filter(model=name, uid=uid).order_by("id"))
    last = rows[-1] if rows else None
    action = "delete" if deleted else "upsert"
    if last is None:
        SyncOutbox.objects.create(model=name, uid=uid, action=action)
    elif last.status == "sent":
        # On its way without an answer: this change waits for it (and gets its base from the answer).
        SyncOutbox.objects.create(model=name, uid=uid, action=action, follows=last.op_id)
    elif deleted:
        if last.base_version is None and last.follows is None and last.status == "pending" and len(rows) == 1 \
                and not last.force:
            last.delete()        # created here and never sent: the server never needs to know
        elif last.action != "delete":
            last.action = "delete"
            last.save(update_fields=["action"])
    elif last.action == "delete":
        last.action = "upsert"   # recreated with the same uid (an undo)
        last.save(update_fields=["action"])
    _schedule()


def enqueue_all(user):
    """Everything on this phone becomes a creation (sending local data to a new account)."""
    for spec in specs.specs():
        for uid in spec.owned(user).values_list("uid", flat=True):
            if not SyncOutbox.objects.filter(model=spec.name, uid=uid).exists():
                SyncOutbox.objects.create(model=spec.name, uid=uid, action="upsert")


def counts():
    return {
        "pending": SyncOutbox.objects.filter(status__in=("pending", "sent", "blocked")).count(),
        "conflicts": SyncOutbox.objects.filter(status__in=("conflict", "rejected")).count(),
    }

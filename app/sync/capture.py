"""Change capture: every write to a synchronised model leaves one row in SyncChange.

SyncChange keeps the latest change of each object (one row per user, model and uid) with a
sequence number per user. A device reads "everything after seq N" to catch up. The number is
taken while the user's Profile row is locked, inside the same transaction as the write, so a
later commit can never get a number below one already handed out.
"""
import contextlib
from contextvars import ContextVar

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

# Who is writing: shown as "Alterado por ..." (browser and system, or the phone's name).
actor = ContextVar("sync_actor", default="Sistema")
# True while data that came from the server is being written on a phone: no new version, no
# change log, no outbox (the change already exists on the other side).
applying_remote = ContextVar("sync_applying_remote", default=False)
# Users being deleted: their cascade must not write new rows pointing at them.
_deleting_users = ContextVar("sync_deleting_users", default=frozenset())


@contextlib.contextmanager
def acting_as(name):
    token = actor.set((name or "Sistema")[:60])
    try:
        yield
    finally:
        actor.reset(token)


@contextlib.contextmanager
def remote_writes():
    token = applying_remote.set(True)
    try:
        yield
    finally:
        applying_remote.reset(token)


def label(model):
    return model._meta.model_name


def owner_path(model):
    """Lookup from the model to its user id (direct, or through the parent)."""
    return getattr(model, "SYNC_OWNER", "user_id")


def owner_of(instance):
    cached = getattr(instance, "_sync_owner", None)
    if cached is not None:
        return cached
    path = owner_path(type(instance))
    if path == "user_id":
        return instance.user_id
    parent_field, _ = path.split("__", 1)
    parent_id = getattr(instance, parent_field + "_id", None)
    if parent_id is None:
        return None
    parent_model = type(instance)._meta.get_field(parent_field).related_model
    return parent_model._base_manager.filter(pk=parent_id).values_list("user_id", flat=True).first()


def record_change(user_id, model, uid, deleted=False):
    """Give the object the next sequence number of its owner. Silent when nothing applies."""
    if user_id is None or uid is None or applying_remote.get() or user_id in _deleting_users.get():
        return
    if client_mode():
        # On the phone the change goes to the outbox (only with an online account).
        if connected():
            from . import outbox
            outbox.enqueue(label(model), uid, deleted)
        return
    from app.models import Profile, SyncChange

    # savepoint=False: inside the caller's transaction this costs nothing; outside, it opens one.
    with transaction.atomic(savepoint=False):
        # The UPDATE locks the row until commit (Postgres), and SQLite runs one writer at a time.
        # RETURNING (SQLite 3.35+, Postgres) gives the new number without a second query.
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {Profile._meta.db_table} SET sync_seq = sync_seq + 1 WHERE user_id = %s RETURNING sync_seq",
                           [user_id])
            row = cursor.fetchone()
        if row is None:
            return
        seq = row[0]
        SyncChange.objects.bulk_create(
            [SyncChange(user_id=user_id, model=label(model), uid=uid, seq=seq, deleted=deleted, changed_at=timezone.now())],
            update_conflicts=True, unique_fields=["user", "model", "uid"], update_fields=["seq", "deleted", "changed_at"],
        )


# --- phone (client) side ----------------------------------------------------------------

_mode = {"value": None}


def client_mode():
    from django.conf import settings
    return settings.SYNC_ROLE == "client"


def connected():
    """Is this phone linked to an online account? Cached; set_connected() updates it."""
    if _mode["value"] is None:
        from app.models import SyncState
        try:
            _mode["value"] = SyncState.objects.filter(pk=1, mode="account").exists()
        except Exception:
            return False
    return _mode["value"]


def set_connected(value):
    _mode["value"] = bool(value) if value is not None else None


def before_change(model, pks):
    """Keep the server's version of these rows before the first local change (for the merge)."""
    if not client_mode() or applying_remote.get() or not connected():
        return
    from . import outbox
    for pk in pks:
        outbox.ensure_base(model, pk)


# --- receivers -------------------------------------------------------------------

def _saved(sender, instance, raw=False, **kwargs):
    if not raw:
        record_change(owner_of(instance), sender, instance.uid)


def _before_delete(sender, instance, **kwargs):
    # The parent may be gone by post_delete (cascade order): find the owner now.
    instance._sync_owner = owner_of(instance)
    before_change(sender, [instance.pk])


def _deleted(sender, instance, **kwargs):
    record_change(owner_of(instance), sender, instance.uid, deleted=True)


def _tags_changed(sender, instance, action, reverse, pk_set, **kwargs):
    from app.models import Transaction

    if applying_remote.get():
        return
    if action in ("pre_add", "pre_remove") and client_mode():
        before_change(Transaction, [instance.pk] if not reverse else list(pk_set or ()))
        return
    if action not in ("post_add", "post_remove", "pre_clear"):
        return
    if not reverse:
        ids = [instance.pk]
    elif action == "pre_clear":
        ids = list(Transaction.all_objects.filter(tags=instance).values_list("pk", flat=True))
    else:
        ids = list(pk_set or ())
    if not ids:
        return
    if action == "pre_clear":
        before_change(Transaction, ids)
    rows = Transaction._base_manager.filter(pk__in=ids)
    rows.update(version=F("version") + 1, updated_at=timezone.now(), updated_by=actor.get())
    for user_id, uid in rows.values_list("user_id", "uid"):
        record_change(user_id, Transaction, uid)


def _user_deleting(sender, instance, **kwargs):
    _deleting_users.set(_deleting_users.get() | {instance.pk})


def _user_deleted(sender, instance, **kwargs):
    _deleting_users.set(_deleting_users.get() - {instance.pk})


def connect():
    from django.conf import settings
    from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete

    from app.models import Transaction

    for model in sync_models():
        uid = f"sync-{label(model)}"
        post_save.connect(_saved, sender=model, dispatch_uid=uid + "-save")
        pre_delete.connect(_before_delete, sender=model, dispatch_uid=uid + "-predelete")
        post_delete.connect(_deleted, sender=model, dispatch_uid=uid + "-delete")
    m2m_changed.connect(_tags_changed, sender=Transaction.tags.through, dispatch_uid="sync-tags")
    pre_delete.connect(_user_deleting, sender=settings.AUTH_USER_MODEL, dispatch_uid="sync-user-deleting")
    post_delete.connect(_user_deleted, sender=settings.AUTH_USER_MODEL, dispatch_uid="sync-user-deleted")


def sync_models():
    from django.apps import apps

    from .fields import SyncModel

    return [m for m in apps.get_app_config("app").get_models() if issubclass(m, SyncModel)]

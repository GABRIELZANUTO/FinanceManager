"""SyncModel: global identity, version and last writer for every synchronised model."""
import uuid

from django.db import models
from django.db.models import F
from django.utils import timezone

from .capture import actor, applying_remote, before_change, owner_of, owner_path, record_change


class TrackedQuerySet(models.QuerySet):
    """QuerySet whose update() and bulk_create() also bump the version and log the change.

    Signals do not fire for these two calls, so without this the change log would miss trash,
    restore, recategorise, date changes, reconcile, recurring links and category merges.
    """

    def update(self, **kwargs):
        if applying_remote.get():
            return super().update(**kwargs)
        rows = list(self.values_list(owner_path(self.model), "uid", "pk"))
        before_change(self.model, [pk for _, _, pk in rows])
        kwargs.setdefault("version", F("version") + 1)
        kwargs.setdefault("updated_at", timezone.now())
        kwargs.setdefault("updated_by", actor.get())
        count = super().update(**kwargs)
        for user_id, uid, _ in rows:
            record_change(user_id, self.model, uid)
        return count

    update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        objs = super().bulk_create(objs, *args, **kwargs)
        for obj in objs:
            record_change(owner_of(obj), self.model, obj.uid)
        return objs


TrackedManager = models.Manager.from_queryset(TrackedQuerySet)


class SyncModel(models.Model):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=60, blank=True, default="", editable=False)

    objects = TrackedManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not applying_remote.get():
            if self.pk is not None and not kwargs.get("force_insert"):
                current = type(self)._base_manager.filter(pk=self.pk).values_list("version", flat=True).first()
                if current is not None:
                    before_change(type(self), [self.pk])
                    self.version = current + 1
            self.updated_by = actor.get()[:60]
            fields = kwargs.get("update_fields")
            if fields is not None:
                # A pre_save signal cannot change update_fields, so it is done here.
                kwargs["update_fields"] = set(fields) | {"version", "updated_at", "updated_by"}
        super().save(*args, **kwargs)

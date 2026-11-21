"""Etapa 0 of the sync plan: uid, version, updated_at and updated_by on every synchronised model,
plus the change log (SyncChange) and the per-user change counter (Profile.sync_seq).

The columns are added with plain ALTER TABLE ADD COLUMN and a unique index, filled row by row
(a callable default on AddField is evaluated once and would repeat the same uid). Through the
normal AddField/AlterField path SQLite rebuilds the table for every not-null column: 68 table
copies, slow on a phone with years of data.
"""
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

MODELS = (
    "account", "budget", "cardinvoice", "category", "categoryrule", "contribution", "goal", "ledgerentry", "monthlock",
    "person", "profile", "recurringmatch", "recurringtransaction", "savedview", "tag", "transaction", "transfer",
)
# Transaction already had updated_at.
NEW_UPDATED_AT = [m for m in MODELS if m != "transaction"]

TYPES = {
    "sqlite": {"uid": "char(32) NULL", "version": "integer unsigned NOT NULL DEFAULT 1",
               "updated_by": "varchar(60) NOT NULL DEFAULT ''", "updated_at": "datetime NULL"},
    "postgresql": {"uid": "uuid NULL", "version": "integer NOT NULL DEFAULT 1",
                   "updated_by": "varchar(60) NOT NULL DEFAULT ''", "updated_at": "timestamp with time zone NULL"},
}


def add_columns(apps, schema_editor):
    conn = schema_editor.connection
    types = TYPES.get(conn.vendor)
    if types is None:
        raise RuntimeError(f"Banco {conn.vendor} não suportado nesta migração.")
    quote = schema_editor.quote_name
    now = conn.ops.adapt_datetimefield_value(django.utils.timezone.now())
    with conn.cursor() as cursor:
        for name in MODELS:
            table = f"app_{name}"
            columns = ["uid", "version", "updated_by"] + (["updated_at"] if name in NEW_UPDATED_AT else [])
            for column in columns:
                cursor.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(column)} {types[column]}")
            if name in NEW_UPDATED_AT:
                cursor.execute(f"UPDATE {quote(table)} SET {quote('updated_at')} = %s", [now])
            cursor.execute(f"SELECT id FROM {quote(table)}")
            ids = [row[0] for row in cursor.fetchall()]
            cursor.executemany(f"UPDATE {quote(table)} SET {quote('uid')} = %s WHERE id = %s",
                               [(uuid.uuid4().hex if conn.vendor == "sqlite" else str(uuid.uuid4()), pk) for pk in ids])
            cursor.execute(f"CREATE UNIQUE INDEX {quote(table + '_uid_uniq')} ON {quote(table)} ({quote('uid')})")
        cursor.execute(f"ALTER TABLE {quote('app_profile')} ADD COLUMN {quote('sync_seq')} bigint NOT NULL DEFAULT 0")


def drop_columns(apps, schema_editor, state_apps):  # state_apps: the models before this migration
    """Plain DROP COLUMN; a table rebuilt by a later migration has uid UNIQUE in its definition,
    which SQLite cannot drop: that table goes through Django's normal (rebuilding) path."""
    quote = schema_editor.quote_name
    for name in MODELS:
        table = f"app_{name}"
        columns = ["uid", "version", "updated_by"] + (["updated_at"] if name in NEW_UPDATED_AT else [])
        if name == "profile":
            columns.append("sync_seq")
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS {quote(table + '_uid_uniq')}")
        try:
            with schema_editor.connection.cursor() as cursor:
                for column in columns:
                    cursor.execute(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}")
        except Exception:
            if schema_editor.connection.vendor != "sqlite":
                raise
            # One rebuild from the model as it was before this migration: its columns are copied,
            # the new ones are left behind.
            schema_editor._remake_table(state_apps.get_model("app", name))


def new_fields(name):
    fields = [
        ("uid", models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
        ("version", models.PositiveIntegerField(default=1, editable=False)),
        ("updated_by", models.CharField(max_length=60, blank=True, default="", editable=False)),
    ]
    if name in NEW_UPDATED_AT:
        fields.append(("updated_at", models.DateTimeField(auto_now=True)))
    if name == "profile":
        fields.append(("sync_seq", models.BigIntegerField(default=0, editable=False)))
    return fields


class AddSyncFields(migrations.operations.base.Operation):
    """All the new columns in one operation: 72 AddField operations would re-render the related
    models once each (about two seconds per migrate, even on an empty database)."""

    reversible = True

    def state_forwards(self, app_label, state):
        for name in MODELS:
            fields = state.models[app_label, name].fields
            for field_name, field in new_fields(name):
                fields[field_name] = field
        state.reload_models([(app_label, name) for name in MODELS], delay=True)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        add_columns(None, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        drop_columns(None, schema_editor, to_state.apps)

    def describe(self):
        return "Add uid, version, updated_at and updated_by to the synchronised models"


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0015_recurring_match"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        AddSyncFields(),
        migrations.CreateModel(
            name="SyncChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model", models.CharField(max_length=30)),
                ("uid", models.UUIDField()),
                ("seq", models.BigIntegerField()),
                ("deleted", models.BooleanField(default=False)),
                ("changed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["user", "seq"], name="sync_change_user_seq")],
                "constraints": [models.UniqueConstraint(fields=("user", "model", "uid"), name="uniq_sync_change")],
            },
        ),
    ]

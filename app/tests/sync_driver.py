"""A phone for the end-to-end sync test: runs in its own process with APP_MODE=1 and its own
SQLite file, reads one JSON command per line on stdin and answers one JSON line on stdout.

    python -m app.tests.sync_driver <folder>
"""
import json
import os
import secrets
import sys
import traceback
from pathlib import Path


def setup(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "DJANGO_SETTINGS_MODULE": "expensemanager.settings", "APP_MODE": "1", "DEBUG": "0",
        "SECRET_KEY": secrets.token_urlsafe(40), "DATABASE_PATH": str(folder / "celular.sqlite3"),
        "APP_TOKEN": "teste", "TIME_ZONE": "America/Sao_Paulo", "LANGUAGE_CODE": "pt-br", "DEVICE_NAME": "Celular de teste",
    })
    import django
    django.setup()
    from django.core.management import call_command
    call_command("migrate", interactive=False, verbosity=0)


def run(cmd):
    from decimal import Decimal

    from django.utils import timezone

    from app import services
    from app.localapp import local_user
    from app.models import Category, SyncConflict, SyncOutbox, SyncState, Transaction
    from app.sync import client, spec as specs
    from app.sync.spec import UidMap

    user = local_user()
    name = cmd["cmd"]
    if name == "setup":
        services.set_up_new_user(user)
        return {}
    if name == "login":
        config, start = client.login(cmd["server"], cmd["username"], cmd["password"])
        client.start(config, start, cmd.get("choice", "download"))
        return {"summary": client.run_cycle(force=True)}
    if name == "connect_only":
        config, start = client.login(cmd["server"], cmd["username"], cmd["password"])
        client.start(config, start, cmd.get("choice", "download"))
        return {}
    if name == "default_account":
        return {"uid": str(services.default_account(user).uid)}
    if name == "cycle":
        return {"summary": client.run_cycle(force=True)}
    if name == "create_tx":
        cat = Category.objects.get(user=user, name=cmd.get("category", "Mercado"))
        tx = Transaction.objects.create(user=user, name=cmd["name"], amount=Decimal(cmd.get("amount", "10.00")), kind="expense",
                                        category=cat, account=services.default_account(user), date=timezone.localdate())
        return {"uid": str(tx.uid)}
    if name == "create_category":
        return {"uid": str(Category.objects.create(user=user, name=cmd["name"]).uid)}
    if name == "edit":
        spec = specs.get(cmd["model"])
        obj = spec.manager.get(uid=cmd["uid"])
        for key, value in cmd["fields"].items():
            setattr(obj, key, Decimal(value) if key == "amount" else value)
        obj.save()
        return {"version": obj.version}
    if name == "delete":
        specs.get(cmd["model"]).manager.get(uid=cmd["uid"]).delete()
        return {}
    if name == "get":
        obj = specs.get(cmd["model"]).manager.filter(uid=cmd["uid"]).first()
        return {"data": specs.serialize(obj, UidMap(user)) if obj else None, "version": obj.version if obj else None}
    if name == "names":
        return {"names": sorted(specs.get(cmd["model"]).owned(user).values_list("name", flat=True))}
    if name == "count":
        return {"count": specs.get(cmd["model"]).owned(user).count()}
    if name == "outbox":
        return {"rows": list(SyncOutbox.objects.values("model", "uid", "action", "status"))}
    if name == "conflicts":
        return {"conflicts": [{"id": c.pk, "reason": c.reason, "model": c.model, "uid": str(c.uid), "local": c.local_data,
                               "server": c.server_data} for c in SyncConflict.objects.filter(resolved_at__isnull=True)]}
    if name == "resolve":
        client.resolve(SyncConflict.objects.get(pk=cmd["id"]), cmd["resolution"])
        return {"summary": client.run_cycle(force=True)}
    if name == "status":
        state = SyncState.get()
        return {**client.status(), "cursor": state.cursor}
    raise ValueError(f"comando desconhecido: {name}")


def main():
    setup(sys.argv[1])
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            answer = {"ok": True, **run(json.loads(line))}
        except Exception as exc:
            answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]}
        print(json.dumps(answer, default=str), flush=True)


if __name__ == "__main__":
    main()

"""The phone's side of the synchronisation (runs inside the app, in a background thread).

Cycle: snapshot (first time) -> push the outbox in batches of 200 -> pull everything after the
cursor. Without internet the outbox only grows; the cycle runs again when the network comes back
(Android calls request_sync), when the app returns to the front, every 5 minutes with the app
open, 10 seconds after a local change, and from the "Sincronizar agora" button.

The key, the server address and the device name live in sync.json beside the database, outside
the app's own backup (and excluded from the Android backup).
"""
import datetime
import gzip
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from app.models import SyncConflict, SyncOutbox, SyncState

from . import spec as specs
from .apply import apply_remote, remap_local, wipe_local
from .capture import set_connected
from .spec import MissingRef, UidMap

log = logging.getLogger(__name__)

PROTOCOL = 1
BATCH = 200
MAX_BATCHES = 50
PULL_LIMIT = 500
TIMEOUT = 20
BACKOFF = [30, 60, 120, 300, 600, 900]
HISTORY = 50


class Offline(Exception):
    """No network, the server did not answer, or answered 5xx/429."""


class ServerSaid(Exception):
    def __init__(self, status, data):
        super().__init__(data.get("message") if isinstance(data, dict) else str(status))
        self.status, self.data = status, data if isinstance(data, dict) else {}

    @property
    def code(self):
        return self.data.get("code", "")


# --- configuration (sync.json) ---------------------------------------------------------------

def config_path():
    return Path(str(settings.DATABASES["default"]["NAME"])).parent / "sync.json"


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_config():
    try:
        config_path().unlink()
    except OSError:
        pass


def device_name():
    return settings.DEVICE_NAME or "Aparelho"


def check_server_url(url):
    url = (url or settings.SYNC_SERVER_URL).strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "10.0.2.2")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("Use um endereço https:// para o servidor.")
    if not parts.hostname:
        raise ValueError("Endereço do servidor inválido.")
    return url


# --- HTTP ------------------------------------------------------------------------------------

def http(method, path, body=None, query=None, config=None, token=True):
    config = config if config is not None else load_config()
    url = config.get("server", settings.SYNC_SERVER_URL) + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Accept-Encoding", "gzip")
    request.add_header("X-Sync-Version", str(PROTOCOL))
    request.add_header("User-Agent", "FinancasPessoais-Android")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token and config.get("token"):
        request.add_header("Authorization", f"Bearer {config['token']}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw, status = response.read(), response.status
            encoding = response.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as exc:
        raw, status, encoding = exc.read(), exc.code, exc.headers.get("Content-Encoding", "")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise Offline(str(exc)) from None
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    if status >= 500 or status == 429:
        raise Offline(f"HTTP {status}")
    if status >= 400:
        raise ServerSaid(status, payload)
    return payload


# --- cycle ------------------------------------------------------------------------------------

_lock = threading.Lock()
_event = threading.Event()
_state = {"running": False, "delay_until": None, "force": False, "thread": None}


def local_user():
    from app.localapp import local_user as user
    return user()


def is_connected():
    return SyncState.get().mode == "account" and bool(load_config().get("token"))


def request_sync(reason="", delay=0):
    """Ask for a cycle soon (never blocks the caller: Android calls this from its own thread)."""
    if delay:
        _state["delay_until"] = timezone.now() + datetime.timedelta(seconds=delay)
    else:
        _state["delay_until"] = None
        if reason in ("button", "botao"):
            _state["force"] = True
    _event.set()
    return True


def start_worker():
    if _state["thread"] is not None:
        return
    thread = threading.Thread(target=_worker, name="financas-sync", daemon=True)
    _state["thread"] = thread
    thread.start()


def _worker():
    while True:
        _event.wait(timeout=300)
        _event.clear()
        wait_until = _state["delay_until"]
        if wait_until and wait_until > timezone.now():
            _event.wait(timeout=(wait_until - timezone.now()).total_seconds())
            if _event.is_set():
                continue
        _state["delay_until"] = None
        force, _state["force"] = _state["force"], False
        try:
            close_old_connections()
            run_cycle(force=force)
        except Exception:      # never let the thread die
            log.exception("sync cycle failed")
        finally:
            close_old_connections()


def run_cycle(force=False):
    """One complete cycle. Returns a summary dict (also kept in SyncState.history)."""
    # Waits for a screen write in progress (see SyncLockMiddleware): writes and cycles never mix.
    if not is_connected() or not _lock.acquire(timeout=30):
        return {"skipped": True}
    state = SyncState.get()
    if state.backoff_until and state.backoff_until > timezone.now() and not force:
        _lock.release()
        return {"skipped": True, "backoff": True}
    summary = {"start": timezone.now().isoformat(), "pushed": 0, "pulled": 0, "conflicts": 0, "error": ""}
    _state["running"] = True
    # Every save below names its fields: the cursor and the snapshot progress are saved as they
    # advance, and a full save of an older copy would move them back.
    try:
        user = local_user()
        if not state.snapshot_done:
            summary["pulled"] += download(user)
        summary.update(push_all(user, summary))
        summary["pulled"] += pull_all(user)
        SyncState.objects.filter(pk=1).update(last_sync_at=timezone.now(), last_error="", backoff_until=None)
        _set_attempts(0)
    except Offline as exc:
        summary["error"] = "Sem conexão com o servidor."
        _backoff(str(exc))
    except ServerSaid as exc:
        summary["error"] = _explain(exc)
        SyncState.objects.filter(pk=1).update(last_error=summary["error"][:300])
        if exc.code == "epoch_changed":
            try:
                restart_from_server(user)
            except (Offline, ServerSaid):
                pass
    except Exception as exc:     # a bug must show on the screen, not vanish in the thread
        log.exception("sync cycle failed")
        summary["error"] = f"Erro ao sincronizar: {type(exc).__name__}"
        SyncState.objects.filter(pk=1).update(last_error=summary["error"][:300])
    finally:
        _state["running"] = False
        if summary.get("pulled"):
            # The screens reload what they show when this number changes.
            _state["data_version"] = _state.get("data_version", 0) + 1
        summary["end"] = timezone.now().isoformat()
        fresh = SyncState.get()
        fresh.history = ([summary] + list(fresh.history or []))[:HISTORY]
        fresh.save(update_fields=["history"])
        _lock.release()
    return summary


_attempts = {"n": 0}


def _set_attempts(n):
    _attempts["n"] = n


def _backoff(message):
    n = _attempts["n"]
    _attempts["n"] = n + 1
    SyncState.objects.filter(pk=1).update(
        backoff_until=timezone.now() + datetime.timedelta(seconds=BACKOFF[min(n, len(BACKOFF) - 1)]),
        last_error="Sem conexão com o servidor.")


def _explain(exc):
    if exc.status == 401:
        return "Entre de novo na sua conta (a chave deste aparelho não vale mais)."
    if exc.status == 402:
        return "A sincronização não está disponível no seu plano."
    if exc.status == 426:
        return "Atualize o aplicativo para sincronizar."
    return exc.data.get("message") or f"O servidor recusou ({exc.status})."


# --- download (snapshot) ------------------------------------------------------------------------

def download(user):
    """Everything from the server, resumable page by page (SyncState.snapshot_progress)."""
    state = SyncState.get()
    progress = dict(state.snapshot_progress or {})
    if "cursor" not in progress:
        start = http("GET", "/api/v1/sync/snapshot/")
        progress = {"cursor": start["cursor"], "epoch": start["epoch"], "models": start["models"], "done": [], "after": 0}
        state.snapshot_progress = progress
        state.save(update_fields=["snapshot_progress"])
    count = 0
    uids = UidMap(user)
    for name in progress["models"]:
        if name in progress["done"]:
            continue
        try:
            specs.get(name)
        except KeyError:
            progress["done"].append(name)    # a model this app version does not know
            continue
        while True:
            page = http("GET", "/api/v1/sync/snapshot/", query={"model": name, "after": progress.get("after", 0), "limit": 1000})
            with transaction.atomic():
                count += _apply_items(user, [(name, item, False) for item in page["items"]], uids)
                progress["after"] = page.get("next_after") or 0
                state.snapshot_progress = progress
                state.save(update_fields=["snapshot_progress"])
            if not page.get("has_more"):
                break
        progress["done"].append(name)
        progress["after"] = 0
        state.snapshot_progress = progress
        state.save(update_fields=["snapshot_progress"])
    SyncState.objects.filter(pk=1).update(cursor=progress["cursor"], epoch=progress["epoch"], snapshot_done=True,
                                          snapshot_progress={})
    return count


def _apply_items(user, rows, uids):
    """Apply (model, item, deleted) in dependency order; items whose references are missing wait
    until the end, then the missing objects are fetched; still missing: kept as a sync error."""
    rows = sorted(rows, key=lambda r: (specs.rank(r[0]) if not r[2] else -specs.rank(r[0])))
    waiting, applied = [], 0
    for name, item, deleted in rows:
        if _has_pending(name, item["uid"]):
            SyncConflict.objects.filter(model=name, uid=item["uid"], resolved_at__isnull=True).update(
                server_data=item.get("data"), server_version=item.get("version"), server_deleted=deleted)
            continue
        if not deleted:
            local = _local(name, item["uid"], user)
            if local is not None and local.version >= (item.get("version") or 0) and name != "profile":
                continue    # the echo of what this phone sent
        try:
            with transaction.atomic():
                apply_remote(user, name, item, deleted, uids)
            applied += 1
        except MissingRef as missing:
            waiting.append((name, item, missing))
    for _ in range(5):
        if not waiting:
            break
        again = []
        for name, item, missing in waiting:
            try:
                with transaction.atomic():
                    apply_remote(user, name, item, False, uids)
                applied += 1
                continue
            except MissingRef as still:
                missing = still
            try:
                found = http("GET", "/api/v1/sync/object/", query={"model": missing.model, "uid": missing.uid})
                with transaction.atomic():
                    apply_remote(user, missing.model, found["item"], False, uids)
            except (ServerSaid, MissingRef):
                pass
            again.append((name, item, missing))
        if len(again) == len(waiting):
            waiting = again
            break
        waiting = again
    for name, item, missing in waiting:
        SyncConflict.objects.create(op_id=item["uid"], model=name, uid=item["uid"], reason="missing_ref",
                                    message="Chegou do servidor mas depende de um registro que não veio.",
                                    server_data=item.get("data"), server_version=item.get("version"))
    return applied


def _local(name, uid, user):
    spec = specs.get(name)
    if name == "profile":
        return spec.owned(user).first()
    return spec.manager.filter(uid=uid).first()


def _has_pending(name, uid):
    return SyncOutbox.objects.filter(model=name, uid=uid).exists()


# --- push ----------------------------------------------------------------------------------------

def _ready_rows():
    rows = list(SyncOutbox.objects.filter(status__in=("pending", "sent"), follows__isnull=True))
    upserts = sorted((r for r in rows if r.action == "upsert"), key=lambda r: (specs.rank(r.model), r.id))
    deletes = sorted((r for r in rows if r.action == "delete"), key=lambda r: (-specs.rank(r.model), r.id))
    return upserts + deletes


def _op(row, user, uids):
    op = {"op_id": str(row.op_id), "model": row.model, "uid": str(row.uid), "action": row.action,
          "base_version": row.base_version, "base": row.base, "force": row.force}
    if row.action == "upsert":
        obj = _local(row.model, row.uid, user)
        if obj is None:
            return None
        op["data"] = specs.serialize(obj, uids)
    return op


def push_all(user, summary):
    state = SyncState.get()
    for _ in range(MAX_BATCHES):
        rows = _ready_rows()[:BATCH]
        if not rows:
            break
        uids = UidMap(user)
        ops, sent = [], {}
        for row in rows:
            op = _op(row, user, uids)
            if op is None:
                row.delete()     # gone here (a cascade from a server deletion): nothing to send
                continue
            ops.append(op)
            sent[op["op_id"]] = (row, op)
        if not ops:
            continue
        SyncOutbox.objects.filter(pk__in=[r.pk for r, _ in sent.values()]).update(status="sent", sent_at=timezone.now())
        answer = http("POST", "/api/v1/sync/push/", {"epoch": state.epoch, "ops": ops})
        with transaction.atomic():
            for result in answer.get("results", []):
                pair = sent.get(str(result.get("op_id")))
                if pair:
                    _handle(user, pair[0], pair[1], result, summary)
        summary["pushed"] += len(ops)
    return {"pushed": summary["pushed"], "conflicts": summary["conflicts"]}


def _handle(user, row, op, result, summary):
    status = result.get("status")
    item = result.get("item") or {}
    followers = list(SyncOutbox.objects.filter(follows=row.op_id))
    if status in ("applied", "merged"):
        if followers:
            for f in followers:
                f.base_version, f.base, f.follows = item.get("version"), item.get("data"), None
                f.save(update_fields=["base_version", "base", "follows"])
        elif row.action == "upsert" and item.get("data") is not None:
            try:
                apply_remote(user, row.model, item)
            except MissingRef:
                pass
        _done(row)
    elif status == "remap":
        remap_local(user, row.model, str(row.uid), item)
        if result.get("conflict"):
            summary["conflicts"] += 1
            _conflict(row, op, item, result["conflict"].get("reason", "duplicate"), "Já existia um igual no servidor com outros valores.")
        _done(row)
    elif status == "conflict":
        row.status = "conflict"
        row.save(update_fields=["status"])
        summary["conflicts"] += 1
        _conflict(row, op, item, result.get("reason", "same_field"), "", deleted=result.get("reason") == "deleted_on_server")
    else:
        ref = result.get("ref") or {}
        waiting_for = SyncOutbox.objects.filter(model=ref.get("model"), uid=ref.get("uid") or None,
                                                status__in=("conflict", "rejected", "blocked")).first() if ref else None
        if result.get("code") == "missing_ref" and waiting_for is not None:
            row.status, row.blocked_by = "blocked", waiting_for.op_id
            row.save(update_fields=["status", "blocked_by"])
            return
        row.status = "rejected"
        row.last_error = (result.get("message") or "")[:300]
        row.save(update_fields=["status", "last_error"])
        summary["conflicts"] += 1
        _conflict(row, op, {}, result.get("code", "invalid"), result.get("message", ""))


def _done(row):
    SyncOutbox.objects.filter(blocked_by=row.op_id, status="blocked").update(status="pending", blocked_by=None)
    SyncConflict.objects.filter(op_id=row.op_id, resolved_at__isnull=True).update(resolved_at=timezone.now())
    row.delete()


def _conflict(row, op, item, reason, message, deleted=False):
    from django.utils.dateparse import parse_datetime
    SyncConflict.objects.update_or_create(op_id=row.op_id, resolved_at=None, defaults={
        "model": row.model, "uid": row.uid, "reason": reason, "message": (message or "")[:300],
        "local_data": op.get("data"), "server_data": item.get("data"), "server_version": item.get("version"),
        "server_deleted": deleted, "server_updated_by": item.get("updated_by") or "",
        "server_updated_at": parse_datetime(item["updated_at"]) if item.get("updated_at") else None})


# --- pull -------------------------------------------------------------------------------------------

def pull_all(user):
    total = 0
    uids = UidMap(user)
    while True:
        state = SyncState.get()
        page = http("GET", "/api/v1/sync/pull/", query={"cursor": state.cursor, "limit": PULL_LIMIT})
        if page.get("epoch") not in (None, state.epoch):
            restart_from_server(user)
            return total
        with transaction.atomic():
            total += _apply_items(user, [(c["model"], c["item"], c["deleted"]) for c in page["changes"]
                                         if c["model"] in specs.by_name()], uids)
            state.cursor = page["next_cursor"]
            state.save(update_fields=["cursor"])
        if not page.get("has_more"):
            return total


def restart_from_server(user):
    """The account was restored from a backup on the server: what was waiting here becomes a
    conflict to review, this phone's copy is replaced by the server's."""
    with transaction.atomic():
        for row in SyncOutbox.objects.all():
            obj = _local(row.model, row.uid, user) if row.action == "upsert" else None
            SyncConflict.objects.create(op_id=row.op_id, model=row.model, uid=row.uid, reason="restored_on_server",
                                        message="Os dados da conta foram restaurados de um backup.",
                                        local_data=specs.serialize(obj, UidMap(user)) if obj else None)
        conflicts = list(SyncConflict.objects.filter(resolved_at__isnull=True).values())
        wipe_local(user)
        for data in conflicts:
            data.pop("id", None)
            SyncConflict.objects.create(**data)
        SyncState.objects.filter(pk=1).update(snapshot_done=False, snapshot_progress={}, cursor=0)
    download(user)


# --- account: login, first choice, logout -------------------------------------------------------------

def login(server, username, password, create=False, full_name="", email=""):
    """Get a key for this device. Does not change local data yet (see start)."""
    server = check_server_url(server)
    payload = {"username": username, "password": password, "device_name": device_name(), "scopes": ["read", "write", "sync"]}
    path = "/api/v1/auth/token/"
    if create:
        payload.update({"full_name": full_name, "email": email})
        path = "/api/v1/auth/register/"
    answer = http("POST", path, payload, config={"server": server}, token=False)
    config = {"server": server, "token": answer["token"], "token_id": answer.get("token_id"),
              "username": answer["user"]["username"], "user_id": answer["user"]["id"], "device_name": device_name()}
    start = http("GET", "/api/v1/sync/snapshot/", config=config)
    return config, start


def local_has_data(user):
    from app.models import Transaction
    return Transaction.all_objects.filter(user=user).exists()


def start(config, start_info, choice):
    """Link this phone to the account. ``choice``: "download" (replace this phone's data) or
    "send" (send this phone's data to the account, then download)."""
    from . import outbox
    user = local_user()
    state = SyncState.get()
    if state.server_user_id not in (None, config["user_id"]):
        choice = "download" if choice != "send" else choice
    with transaction.atomic():
        if choice == "download":
            wipe_local(user)
        SyncOutbox.objects.all().delete()
        state.mode, state.server_user_id = "account", config["user_id"]
        state.cursor, state.epoch, state.snapshot_done, state.snapshot_progress = 0, start_info.get("epoch", 1), False, {}
        state.backoff_until, state.last_error = None, ""
        state.save()
        save_config(config)
        set_connected(True)
        if choice == "send":
            outbox.enqueue_all(user)
    if choice == "send":
        # Send first (names already on the server are joined), then download the rest.
        summary = {"pushed": 0, "conflicts": 0}
        push_all(user, summary)
    request_sync("button")


def logout(keep_data=True):
    config = load_config()
    if config.get("token"):
        try:
            http("POST", "/api/v1/auth/revoke/", {}, config=config)
        except (Offline, ServerSaid):
            pass
    clear_config()
    with transaction.atomic():
        SyncState.get()
        SyncState.objects.filter(pk=1).update(mode="local", server_user_id=None, snapshot_done=False, cursor=0)
        SyncOutbox.objects.all().delete()
        SyncConflict.objects.all().delete()
        if not keep_data:
            wipe_local(local_user())
    set_connected(False)


def status():
    from . import outbox
    state = SyncState.get()
    config = load_config()
    counts = outbox.counts()
    return {
        "mode": state.mode, "connected": state.mode == "account" and bool(config.get("token")),
        "server": config.get("server", settings.SYNC_SERVER_URL), "username": config.get("username", ""),
        "device": config.get("device_name") or device_name(), "running": _state["running"],
        "data_version": _state.get("data_version", 0),
        "last_sync_at": state.last_sync_at.isoformat() if state.last_sync_at else None, "last_error": state.last_error,
        "snapshot_done": state.snapshot_done, **counts,
        "open_conflicts": SyncConflict.objects.filter(resolved_at__isnull=True).count(),
    }


# --- resolving a conflict -------------------------------------------------------------------------

RESOLUTIONS = ("mine", "server", "manual", "retry")


def resolve(conflict, resolution):
    """mine: send this phone's version over the server's (force). server and manual: keep the
    server's (manual: the screen then opens a form with this phone's values). retry: send again
    as it is (after fixing the cause, such as reopening a closed month)."""
    from .capture import remote_writes
    user = local_user()
    row = SyncOutbox.objects.filter(op_id=conflict.op_id).first()
    with transaction.atomic():
        if resolution in ("mine", "retry"):
            if row is None:
                row = SyncOutbox(model=conflict.model, uid=conflict.uid, action="upsert")
                if conflict.local_data and resolution == "mine":
                    spec = specs.get(conflict.model)
                    obj = _local(conflict.model, conflict.uid, user)
                    if obj is not None:
                        values, m2m = specs.deserialize(spec, conflict.local_data, UidMap(user))
                        with remote_writes():
                            spec.manager.filter(pk=obj.pk).update(**values)
            row.status, row.force, row.blocked_by = "pending", resolution == "mine", None
            if resolution == "mine" and conflict.server_version:
                row.base_version = conflict.server_version
            # A new operation: the old op_id has a receipt on the server with the old answer.
            new_id = uuid.uuid4()
            if row.pk:
                SyncOutbox.objects.filter(blocked_by=row.op_id).update(blocked_by=new_id)
            row.op_id = new_id
            row.save()
        else:
            if conflict.server_deleted or conflict.server_data is None:
                obj = _local(conflict.model, conflict.uid, user)
                if obj is not None and conflict.model != "profile":
                    with remote_writes():
                        obj.delete()
            else:
                try:
                    apply_remote(user, conflict.model, {"uid": str(conflict.uid), "version": conflict.server_version or 1,
                                                        "updated_by": conflict.server_updated_by,
                                                        "updated_at": conflict.server_updated_at, "data": conflict.server_data})
                except MissingRef:
                    pass
            if row is not None:
                SyncOutbox.objects.filter(blocked_by=row.op_id).update(status="pending", blocked_by=None)
                row.delete()
        conflict.resolved_at = timezone.now()
        conflict.resolution = {"retry": "mine"}.get(resolution, resolution)
        conflict.save(update_fields=["resolved_at", "resolution"])
    request_sync("button")

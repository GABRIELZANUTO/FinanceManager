"""Daily backup of the whole database to the owner's Google Drive.

What goes up: one zip per day with
  banco.json            every table (Django dumpdata; restore with "manage.py loaddata banco.json"
                        on an empty, migrated database)
  usuarios/<nome>.json  each user's data in the app's own backup format (Backup > Restaurar)
  LEIA-ME.txt           how to restore

How: the Drive API with a refresh token of the owner's Google account (OAuth "app for computer",
scope drive.file: the app only sees the files it created). Plain urllib, no new dependency.
Settings (environment): GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN, optional
GDRIVE_FOLDER (folder name), GDRIVE_KEEP (how many zips to keep, default 30) and
GDRIVE_BACKUP_HOUR (local hour after which the day's backup runs, default 3).
Get the refresh token once with scripts/autorizar_google_drive.py.
"""
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,size"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_FOLDER = "Finanças Pessoais (backups)"

README = """Backup do Finanças Pessoais gerado em {when}.

banco.json: todas as tabelas. Para restaurar o servidor inteiro, num banco novo:
    python manage.py migrate
    python manage.py loaddata banco.json

usuarios/<nome>.json: os dados de cada pessoa no formato do próprio programa.
Para restaurar só uma pessoa: entre com ela no site, menu Backup, Restaurar.

Guarde este arquivo com cuidado: ele tem todos os dados financeiros e as senhas
(cifradas) das contas.
"""


class DriveError(Exception):
    pass


def configured():
    return all(os.environ.get(k) for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"))


# --- the zip -------------------------------------------------------------------------------------

def build_zip(now=None):
    """(file name, zip bytes)."""
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    from . import backup

    now = now or timezone.localtime()
    dump = io.StringIO()
    call_command("dumpdata", exclude=["sessions", "contenttypes", "auth.permission", "admin.logentry"],
                 natural_foreign=True, indent=None, stdout=dump)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("banco.json", dump.getvalue())
        for user in get_user_model().objects.order_by("pk"):
            archive.writestr(f"usuarios/{user.username}.json", backup.dumps(user))
        archive.writestr("LEIA-ME.txt", README.format(when=now.strftime("%d/%m/%Y %H:%M")))
    return f"financas-backup-{now:%Y-%m-%d-%H%M}.zip", buffer.getvalue()


# --- Google Drive ----------------------------------------------------------------------------------

def _request(url, data=None, headers=None, method=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise DriveError(f"Google respondeu {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"Sem conexão com o Google: {exc}") from None
    return json.loads(body.decode("utf-8") or "{}") if body else {}


def access_token():
    data = urllib.parse.urlencode({
        "client_id": os.environ["GDRIVE_CLIENT_ID"], "client_secret": os.environ["GDRIVE_CLIENT_SECRET"],
        "refresh_token": os.environ["GDRIVE_REFRESH_TOKEN"], "grant_type": "refresh_token",
    }).encode()
    answer = _request(TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in answer:
        raise DriveError("O Google não devolveu um token de acesso.")
    return answer["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _escape(text):
    return text.replace("\\", "\\\\").replace("'", "\\'")


def folder_id(token, name):
    query = f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    found = _request(f"{FILES_URL}?{urllib.parse.urlencode({'q': query, 'fields': 'files(id)', 'spaces': 'drive'})}",
                     headers=_auth(token)).get("files", [])
    if found:
        return found[0]["id"]
    made = _request(FILES_URL, json.dumps({"name": name, "mimeType": FOLDER_MIME}).encode(),
                    {**_auth(token), "Content-Type": "application/json"})
    return made["id"]


def upload(token, folder, name, content):
    boundary = uuid.uuid4().hex
    meta = json.dumps({"name": name, "parents": [folder], "mimeType": "application/zip"}).encode()
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta
            + f"\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n".encode() + content
            + f"\r\n--{boundary}--\r\n".encode())
    return _request(UPLOAD_URL, body, {**_auth(token), "Content-Type": f"multipart/related; boundary={boundary}"})


def prune(token, folder, keep):
    """Delete the oldest backups beyond ``keep`` (only zips in the app's folder)."""
    query = f"'{folder}' in parents and trashed = false and name contains 'financas-backup-'"
    files = _request(f"{FILES_URL}?{urllib.parse.urlencode({'q': query, 'orderBy': 'createdTime desc', 'fields': 'files(id,name)', 'pageSize': 1000})}",
                     headers=_auth(token)).get("files", [])
    removed = []
    for item in files[keep:]:
        _request(f"{FILES_URL}/{item['id']}", headers=_auth(token), method="DELETE")
        removed.append(item["name"])
    return removed


def run_backup():
    """Build the zip and send it. Returns a summary dict; raises DriveError on failure."""
    if not configured():
        raise DriveError("Faltam GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET e GDRIVE_REFRESH_TOKEN.")
    name, content = build_zip()
    token = access_token()
    folder = folder_id(token, os.environ.get("GDRIVE_FOLDER") or DEFAULT_FOLDER)
    sent = upload(token, folder, name, content)
    removed = prune(token, folder, max(1, int(os.environ.get("GDRIVE_KEEP") or 30)))
    summary = {"name": name, "bytes": len(content), "id": sent.get("id"), "removed": removed}
    _save_state({"last_ok": timezone.localdate().isoformat(), "last_name": name, "last_error": "",
                 "at": timezone.now().isoformat()})
    return summary


# --- once a day, inside the web process ------------------------------------------------------------

def _state_path():
    return Path(str(settings.DATABASES["default"]["NAME"])).parent / "backup_drive.json"


def _load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(data):
    state = {**_load_state(), **data}
    _state_path().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def due(now=None):
    now = now or timezone.localtime()
    hour = int(os.environ.get("GDRIVE_BACKUP_HOUR") or 3)
    return now.hour >= hour and _load_state().get("last_ok") != now.date().isoformat()


def _lock():
    """One process at a time (several gunicorn workers): a lock file, stale after 2 hours."""
    path = _state_path().with_suffix(".lock")
    try:
        if path.exists() and time.time() - path.stat().st_mtime > 7200:
            path.unlink()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return path
    except OSError:
        return None


def _loop():
    from django.db import close_old_connections
    failures = 0
    while True:
        try:
            if due():
                lock = _lock()
                if lock is not None:
                    try:
                        close_old_connections()
                        summary = run_backup()
                        log.info("backup no Google Drive: %s (%s bytes)", summary["name"], summary["bytes"])
                        failures = 0
                    finally:
                        lock.unlink()
                        close_old_connections()
        except Exception as exc:     # keep trying: next hour, then hourly
            failures += 1
            log.error("backup no Google Drive falhou: %s", exc)
            _save_state({"last_error": str(exc)[:300], "error_at": timezone.now().isoformat()})
        time.sleep(3600 if failures else 1800)


_started = {"thread": None}


def start_daily():
    """Called by the WSGI entry point (the server only); does nothing without the settings."""
    if _started["thread"] is not None or not configured() or settings.APP_MODE:
        return
    thread = threading.Thread(target=_loop, name="backup-drive", daemon=True)
    _started["thread"] = thread
    thread.start()


def status():
    state = _load_state()
    return {"configured": configured(), **state}

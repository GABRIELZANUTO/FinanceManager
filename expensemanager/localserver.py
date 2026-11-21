"""Run the whole system inside the Android app (called from the app's Java code through Chaquopy).

    url = start(files_dir, time_zone)   # -> "http://127.0.0.1:<port>/app/entrar/?token=..."

* The database and the secret key live in ``files_dir``, the app's private folder: other apps
  cannot read it, and uninstalling the app removes it (the Backup JSON is the way to keep a copy).
* A small web server (standard library) answers on 127.0.0.1 only, on a free port chosen by the
  system; the screens are shown by the app's built-in WebView.
* Each launch makes a new secret token; see app/localapp.py for why it is needed.
"""
import os
import secrets
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

_started = {}
_lock = threading.Lock()


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True          # the app closing must not wait for open connections
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):    # no request log on the phone
        pass


def _secret_key(folder):
    path = folder / "secret_key.txt"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(50), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _device_name():
    """ "Xiaomi 2201117TG" on Android (read through Chaquopy), empty elsewhere."""
    try:
        from java import jclass

        build = jclass("android.os.Build")
        maker = str(build.MANUFACTURER or "")
        return f"{maker[:1].upper()}{maker[1:]} {build.MODEL}".strip()[:60]
    except Exception:
        return ""


def start(files_dir, time_zone="America/Sao_Paulo"):
    """Prepare the database (first time and after updates) and start serving. Idempotent."""
    with _lock:
        if "url" in _started:
            return _started["url"]
        folder = Path(str(files_dir)) / "dados"
        folder.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        os.environ.update({
            "DJANGO_SETTINGS_MODULE": "expensemanager.settings",
            "APP_MODE": "1",
            "DEBUG": "0",
            "SECRET_KEY": _secret_key(folder),
            "DATABASE_PATH": str(folder / "financas.sqlite3"),
            "APP_TOKEN": token,
            "TIME_ZONE": str(time_zone or "America/Sao_Paulo"),
            "LANGUAGE_CODE": "pt-br",
            "DEVICE_NAME": _device_name(),
        })
        import django
        django.setup()
        from django.core.management import call_command
        call_command("migrate", interactive=False, verbosity=0)
        from django.core.wsgi import get_wsgi_application
        server = make_server("127.0.0.1", 0, get_wsgi_application(), server_class=_Server, handler_class=_QuietHandler)
        threading.Thread(target=server.serve_forever, name="financas-web", daemon=True).start()
        port = server.server_address[1]
        # Synchronisation with the online account (does nothing until one is connected).
        from app.sync import client as sync_client
        sync_client.start_worker()
        sync_client.request_sync("abrir")
        _started["url"] = f"http://127.0.0.1:{port}/app/entrar/?token={token}"
        return _started["url"]


def request_sync(reason=""):
    """Called by Android when the network comes back or the app returns to the front."""
    if "url" not in _started:
        return False
    from app.sync import client as sync_client
    return sync_client.request_sync(reason)

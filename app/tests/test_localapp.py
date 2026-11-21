"""Android app mode: the system started the way the app starts it (expensemanager/localserver.py)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

SCRIPT = r'''
import http.cookiejar, sys, urllib.error, urllib.request
from expensemanager import localserver

url = localserver.start(sys.argv[1], "America/Sao_Paulo")
assert localserver.start(sys.argv[1]) == url, "start must be idempotent"
base = url.split("/app/")[0]

def status(opener, path):
    try:
        return opener.open(base + path).status
    except urllib.error.HTTPError as e:
        return e.code

stranger = urllib.request.build_opener()
print("stranger", status(stranger, "/"), status(stranger, "/api/summary/"),
      status(stranger, "/app/entrar/?token=wrong"), status(stranger, "/static/app/css/app.css"))
app = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
first = app.open(url)
print("enter", first.status, first.url.replace(base, ""))
print("pages", status(app, "/"), status(app, "/api/summary/"), status(app, "/settings/"))
start = app.open(base + "/sync/inicio/").read().decode()
print("sync", status(app, "/sync/"), status(app, "/api/sync/status/"), "Usar só neste celular" in start)
html = app.open(base + "/settings/").read().decode()
print("settings", "Alterar senha" in html, "Excluir minha conta" in html, "Apagar meus dados" in html)
'''


@unittest.skipIf(os.environ.get("SKIP_SUBPROCESS_TESTS"), "subprocess tests disabled")
class LocalAppTests(unittest.TestCase):
    def test_app_mode_needs_the_launch_token_and_has_one_user(self):
        env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "DEBUG", "DATABASE_PATH", "APP_MODE")}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as folder:
            run = subprocess.run([sys.executable, "-c", SCRIPT, folder], cwd=ROOT, env=env, capture_output=True,
                                 text=True, timeout=180)
            self.assertEqual(run.returncode, 0, run.stderr)
            out = dict(line.split(" ", 1) for line in run.stdout.strip().splitlines())
            self.assertEqual(out["stranger"], "403 403 403 200")        # only the screens' files without the token
            self.assertEqual(out["enter"], "200 /sync/inicio/")         # first time: only here or with an account
            self.assertEqual(out["sync"], "200 200 True")
            self.assertEqual(out["pages"], "200 200 200")
            self.assertEqual(out["settings"], "False False True")       # no password, no account deletion
            self.assertTrue((Path(folder) / "dados" / "financas.sqlite3").exists())
            self.assertTrue((Path(folder) / "dados" / "secret_key.txt").exists())

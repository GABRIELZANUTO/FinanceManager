"""Daily backup to Google Drive: the zip, what is sent, retention and the once-a-day rule.
Google is never called: _request is replaced by a fake that records the calls."""
import io
import json
import os
import zipfile
from unittest import mock

from django.utils import timezone

from app import drive_backup

from .base import AppTestCase

ENV = {"GDRIVE_CLIENT_ID": "id", "GDRIVE_CLIENT_SECRET": "segredo", "GDRIVE_REFRESH_TOKEN": "refresh", "GDRIVE_KEEP": "2"}


class FakeGoogle:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = list(existing)

    def __call__(self, url, data=None, headers=None, method=None):
        self.calls.append((method or ("POST" if data else "GET"), url, data))
        if url == drive_backup.TOKEN_URL:
            return {"access_token": "tok"}
        if url.startswith(drive_backup.UPLOAD_URL.split("?")[0]):
            return {"id": "novo", "name": "x"}
        if method == "DELETE":
            return {}
        if "orderBy" in url:
            return {"files": [{"id": "novo", "name": "financas-backup-novo"}] + self.existing}
        if data:
            return {"id": "pasta"}
        return {"files": []}


class DriveBackupTests(AppTestCase):
    def test_zip_has_everything_and_reloads(self):
        self.tx(name="no backup")
        name, content = drive_backup.build_zip()
        self.assertTrue(name.startswith("financas-backup-") and name.endswith(".zip"))
        archive = zipfile.ZipFile(io.BytesIO(content))
        self.assertIn("banco.json", archive.namelist())
        self.assertIn("usuarios/alice.json", archive.namelist())
        rows = json.loads(archive.read("banco.json"))
        models = {r["model"] for r in rows}
        self.assertIn("app.transaction", models)
        self.assertNotIn("sessions.session", models)
        self.assertIn("no backup", archive.read("usuarios/alice.json").decode())

    def test_send_and_keep_only_the_newest(self):
        fake = FakeGoogle(existing=[{"id": "a", "name": "financas-backup-a"}, {"id": "b", "name": "financas-backup-b"}])
        with mock.patch.dict(os.environ, ENV), mock.patch.object(drive_backup, "_request", fake), \
                mock.patch.object(drive_backup, "_state_path", return_value=self.tmp_state()):
            summary = drive_backup.run_backup()
        self.assertEqual(summary["removed"], ["financas-backup-b"])
        upload = next(c for c in fake.calls if c[1].startswith("https://www.googleapis.com/upload"))
        self.assertIn(b"application/zip", upload[2])
        self.assertIn(b'"parents": ["pasta"]', upload[2])

    def test_once_a_day_after_the_hour(self):
        state = self.tmp_state()
        with mock.patch.dict(os.environ, {"GDRIVE_BACKUP_HOUR": "3"}), \
                mock.patch.object(drive_backup, "_state_path", return_value=state):
            early = timezone.localtime().replace(hour=2)
            late = timezone.localtime().replace(hour=4)
            self.assertFalse(drive_backup.due(early))
            self.assertTrue(drive_backup.due(late))
            drive_backup._save_state({"last_ok": late.date().isoformat()})
            self.assertFalse(drive_backup.due(late))

    def test_without_settings_it_says_what_is_missing(self):
        with mock.patch.dict(os.environ, {k: "" for k in ENV}):
            self.assertFalse(drive_backup.configured())
            with self.assertRaises(drive_backup.DriveError):
                drive_backup.run_backup()

    def tmp_state(self):
        import tempfile
        from pathlib import Path
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        return folder / "backup_drive.json"

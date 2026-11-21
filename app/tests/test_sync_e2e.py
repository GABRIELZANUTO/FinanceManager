"""Alteração 2 end to end: this test process is the site (LiveServerTestCase) and a phone runs in
another process (app/tests/sync_driver.py, APP_MODE=1, its own SQLite). Scenarios from the plan:
offline edits merged, same field in conflict (and its resolutions), delete against edit, the same
category created on both sides, a resend after a dropped answer and a full download."""
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import LiveServerTestCase, override_settings

from app import services
from app.models import Category, SyncChange, Transaction

from .base import PASSWORD, User

ROOT = Path(settings.BASE_DIR)


class Phone:
    def __init__(self, folder):
        self.proc = subprocess.Popen([sys.executable, "-m", "app.tests.sync_driver", str(folder)], cwd=str(ROOT),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8")
        ready = self.proc.stdout.readline()
        if not ready:
            raise RuntimeError("o celular de teste não iniciou: " + self.proc.stderr.read()[-2000:])

    def __call__(self, cmd, **args):
        self.proc.stdin.write(json.dumps({"cmd": cmd, **args}) + "\n")
        self.proc.stdin.flush()
        answer = json.loads(self.proc.stdout.readline())
        if not answer.get("ok"):
            raise AssertionError(f"{cmd}: {answer.get('error')}\n{answer.get('trace')}")
        return answer

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)


@override_settings(DEBUG=True)   # the live test server speaks plain http
class SyncEndToEndTests(LiveServerTestCase):
    host = "127.0.0.1"    # "localhost" makes urllib try IPv6 first on Windows (2 s per request)
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("ana", "ana@example.com", PASSWORD)
        services.set_up_new_user(self.user)
        self.folder = Path(tempfile.mkdtemp(prefix="celular-"))
        self.phone = Phone(self.folder)
        self.phone("setup")

    def tearDown(self):
        self.phone.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def login(self, choice="download"):
        return self.phone("login", server=self.live_server_url, username="ana", password=PASSWORD, choice=choice)

    def site_tx(self, name, amount="20.00"):
        return Transaction.objects.create(user=self.user, name=name, amount=Decimal(amount), kind="expense",
                                          category=Category.objects.get(user=self.user, name="Mercado"),
                                          account=services.default_account(self.user))

    def test_full_story(self):
        # Download: what the site has arrives on the phone.
        for i in range(30):
            self.site_tx(f"site {i}")
        self.login("download")
        self.assertEqual(self.phone("count", model="transaction")["count"], 30)
        self.assertEqual(self.phone("status")["pending"], 0)

        # Offline on the phone, a new expense; it reaches the site on the next cycle.
        new = self.phone("create_tx", name="Padaria", amount="7.50")["uid"]
        self.assertEqual(self.phone("status")["pending"], 1)
        self.phone("cycle")
        tx = Transaction.objects.get(uid=new)
        self.assertEqual((tx.name, tx.amount, tx.updated_by), ("Padaria", Decimal("7.50"), "Celular de teste"))
        self.assertEqual(self.phone("status")["pending"], 0)

        # Different fields on each side: merged.
        both = self.site_tx("Feira")
        self.phone("cycle")
        self.phone("edit", model="transaction", uid=str(both.uid), fields={"name": "Feira (celular)"})
        Transaction.objects.filter(pk=both.pk).update(note="nota do site")
        self.phone("cycle")
        both.refresh_from_db()
        self.assertEqual((both.name, both.note), ("Feira (celular)", "nota do site"))
        self.assertEqual(self.phone("get", model="transaction", uid=str(both.uid))["data"]["note"], "nota do site")

        # Same field on both sides: conflict, nothing lost; "mine" wins on purpose.
        self.phone("edit", model="transaction", uid=str(both.uid), fields={"amount": "11.00"})
        Transaction.objects.filter(pk=both.pk).update(amount=Decimal("33.00"))
        self.phone("cycle")
        [conflict] = self.phone("conflicts")["conflicts"]
        self.assertEqual((conflict["reason"], conflict["local"]["amount"], conflict["server"]["amount"]), ("same_field", "11.00", "33.00"))
        self.assertEqual(Transaction.objects.get(pk=both.pk).amount, Decimal("33.00"))
        resolved = self.phone("resolve", id=conflict["id"], resolution="mine")
        self.assertEqual(Transaction.objects.get(pk=both.pk).amount, Decimal("11.00"), (resolved, self.phone("outbox")))
        self.assertEqual(self.phone("conflicts")["conflicts"], [])

        # Again, and this time the server's version is kept.
        self.phone("edit", model="transaction", uid=str(both.uid), fields={"amount": "12.00"})
        Transaction.objects.filter(pk=both.pk).update(amount=Decimal("44.00"))
        self.phone("cycle")
        [conflict] = self.phone("conflicts")["conflicts"]
        self.phone("resolve", id=conflict["id"], resolution="server")
        self.assertEqual(self.phone("get", model="transaction", uid=str(both.uid))["data"]["amount"], "44.00")
        self.assertEqual(Transaction.objects.get(pk=both.pk).amount, Decimal("44.00"))

        # Deleted on the site, edited on the phone: a conflict, the phone's values kept for manual entry.
        gone = self.site_tx("Some no site")
        self.phone("cycle")
        self.phone("edit", model="transaction", uid=str(gone.uid), fields={"name": "Editada no celular"})
        Transaction.all_objects.filter(pk=gone.pk).delete()
        self.phone("cycle")
        [conflict] = self.phone("conflicts")["conflicts"]
        self.assertEqual((conflict["reason"], conflict["local"]["name"]), ("deleted_on_server", "Editada no celular"))
        self.phone("resolve", id=conflict["id"], resolution="manual")
        self.assertIsNone(self.phone("get", model="transaction", uid=str(gone.uid))["data"])

        # A deletion on the site reaches the phone.
        removed = self.site_tx("Apagar")
        self.phone("cycle")
        Transaction.all_objects.filter(pk=removed.pk).delete()
        self.phone("cycle")
        self.assertIsNone(self.phone("get", model="transaction", uid=str(removed.uid))["data"])

        # The same category created on both sides: one category, the phone's expense points to it.
        Category.objects.create(user=self.user, name="Pets")
        phone_cat = self.phone("create_category", name="pets")["uid"]
        pet_tx = self.phone("create_tx", name="Ração", category="pets")["uid"]
        self.phone("cycle")
        site_cat = Category.objects.get(user=self.user, name__iexact="pets")
        self.assertNotEqual(str(site_cat.uid), phone_cat)
        self.assertEqual(Transaction.objects.get(uid=pet_tx).category, site_cat)
        self.assertEqual(self.phone("names", model="category")["names"].count("Pets"), 1)
        self.assertNotIn("pets", self.phone("names", model="category")["names"])

        # Everything converged: same transactions on both sides, nothing waiting.
        site_names = sorted(Transaction.all_objects.filter(user=self.user).values_list("name", flat=True))
        self.assertEqual(self.phone("count", model="transaction")["count"], len(site_names))
        status = self.phone("status")
        self.assertEqual((status["pending"], status["conflicts"], status["last_error"]), (0, 0, ""))

    def test_send_this_phones_data_to_a_new_account(self):
        self.phone("create_tx", name="Do celular 1")
        self.phone("create_tx", name="Do celular 2")
        self.login("send")
        names = set(Transaction.objects.filter(user=self.user).values_list("name", flat=True))
        self.assertEqual(names, {"Do celular 1", "Do celular 2"})
        # Default categories were joined by name, not duplicated.
        self.assertEqual(Category.objects.filter(user=self.user, name="Mercado").count(), 1)
        self.assertEqual(self.phone("status")["pending"], 0)

    def test_default_account_recreated_by_a_screen_before_the_download(self):
        self.site_tx("do site")
        self.phone("connect_only", server=self.live_server_url, username="ana", password=PASSWORD, choice="download")
        self.phone("default_account")      # what the dashboard does when there is no account
        summary = self.phone("cycle")["summary"]
        self.assertEqual(summary["error"], "")
        self.assertEqual(self.phone("count", model="transaction")["count"], 1)
        self.assertEqual(self.phone("count", model="account")["count"], services.default_account(self.user) and 1)
        self.assertEqual(self.phone("status")["pending"], 0)

    def test_server_down_then_back(self):
        self.login("download")
        self.phone("create_tx", name="Sem internet")
        with override_settings(ALLOWED_HOSTS=["nada.invalid"]):
            summary = self.phone("cycle")["summary"]
        self.assertTrue(summary["error"])
        self.assertFalse(Transaction.objects.filter(name="Sem internet").exists())
        self.phone("cycle")
        self.assertEqual(Transaction.objects.filter(name="Sem internet").count(), 1)

    def test_seq_never_repeats_under_sync(self):
        self.login("download")
        for i in range(3):
            self.phone("create_tx", name=f"n{i}")
        self.phone("cycle")
        seqs = list(SyncChange.objects.filter(user=self.user).values_list("seq", flat=True))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertTrue(all(isinstance(uuid.UUID(str(u)), uuid.UUID) for u in SyncChange.objects.values_list("uid", flat=True)))

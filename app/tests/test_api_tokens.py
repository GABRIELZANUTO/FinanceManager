"""Alteração 1: /api/v1/ with an API key (no cookie, no CSRF token)."""
import datetime
import json

from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from app import tokens
from app.models import ApiToken, Transaction

from .base import PASSWORD, AppTestCase


class ApiTokenTests(AppTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.token, self.key = tokens.create(self.user, "Celular Xiaomi", ("read", "write"))
        self.api = Client(enforce_csrf_checks=True)

    def call(self, method, path, data=None, key=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key or self.key}"} if key is not False else {}
        payload = json.dumps(data) if data is not None else None
        return getattr(self.api, method)(path, payload, content_type="application/json", secure=True, **headers, **extra)

    def tx_payload(self):
        return {"name": "Pela API", "amount": "12.50", "kind": "expense", "category": self.cat().pk,
                "account": self.account().pk, "date": timezone.localdate().isoformat()}

    def test_key_reads_and_writes_without_csrf(self):
        self.assertEqual(self.call("get", "/api/v1/summary/").status_code, 200)
        response = self.call("post", "/api/v1/transactions/", self.tx_payload())
        self.assertEqual(response.status_code, 201, response.content)
        tx = Transaction.objects.get(name="Pela API")
        self.assertEqual((tx.user, tx.updated_by), (self.user, "Celular Xiaomi"))
        self.assertIn("uid", response.json()["transaction"])

    def test_session_without_csrf_is_still_refused(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        response = browser.post("/api/transactions/", json.dumps(self.tx_payload()), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_bad_revoked_or_expired_key(self):
        self.assertEqual(self.call("get", "/api/v1/summary/", key="fp_nada_nada").status_code, 401)
        self.assertEqual(self.call("get", "/api/v1/summary/", key=self.key + "x").json()["code"], "auth")
        tokens.revoke(self.token)
        self.assertEqual(self.call("get", "/api/v1/summary/").status_code, 401)
        old, old_key = tokens.create(self.user, "velho", expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertEqual(self.call("get", "/api/v1/summary/", key=old_key).status_code, 401)

    def test_read_only_key_cannot_write(self):
        _, key = tokens.create(self.user, "só leitura", ("read",))
        response = self.call("post", "/api/v1/transactions/", self.tx_payload(), key=key)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "scope"))
        self.assertFalse(Transaction.objects.filter(name="Pela API").exists())

    def test_no_key_on_v1_means_not_logged_in(self):
        self.assertEqual(self.call("get", "/api/v1/summary/", key=False).status_code, 401)

    def test_other_users_data_stays_hidden(self):
        theirs = self.tx(user=self.other)
        self.assertEqual(self.call("get", f"/api/v1/transactions/{theirs.pk}/").status_code, 404)

    @override_settings(DEBUG=False)
    def test_plain_http_is_refused(self):
        response = self.api.get("/api/v1/summary/", HTTP_AUTHORIZATION=f"Bearer {self.key}")
        self.assertEqual(response.status_code, 403)

    def test_secret_is_not_stored(self):
        secret = self.key.split("_", 2)[2]
        row = ApiToken.objects.get(pk=self.token.pk)
        self.assertNotIn(secret, json.dumps([row.key_hash, row.prefix, row.name]))
        self.assertEqual(len(row.key_hash), 64)

    def test_last_used_is_recorded(self):
        self.call("get", "/api/v1/summary/")
        self.assertIsNotNone(ApiToken.objects.get(pk=self.token.pk).last_used_at)

    @override_settings(API_TOKEN_RATE=3)
    def test_rate_limit_per_key(self):
        codes = [self.call("get", "/api/v1/auth/me/").status_code for _ in range(4)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)

    def test_body_limit_and_error_code(self):
        response = self.call("post", "/api/v1/transactions/", {"name": "x" * 70000})
        self.assertEqual((response.status_code, response.json()["code"]), (413, "payload_too_large"))
        response = self.call("post", "/api/v1/transactions/", {"name": ""})
        self.assertEqual(response.json()["code"], "validation")


class ApiAuthRouteTests(AppTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.api = Client(enforce_csrf_checks=True)

    def post(self, path, data):
        return self.api.post(path, json.dumps(data), content_type="application/json", secure=True)

    def test_get_a_key_with_password_and_use_it(self):
        response = self.post("/api/v1/auth/token/", {"username": "ALICE", "password": PASSWORD,
                                                     "device_name": "Celular", "scopes": ["read", "write", "sync"]})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertTrue(data["token"].startswith("fp_"))
        self.assertEqual((data["scopes"], data["plan"], data["user"]["username"]), (["read", "write", "sync"], "free", "alice"))
        me = self.api.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {data['token']}", secure=True).json()
        self.assertEqual(me["token"]["name"], "Celular")
        revoked = self.api.post("/api/v1/auth/revoke/", HTTP_AUTHORIZATION=f"Bearer {data['token']}", secure=True)
        self.assertEqual(revoked.status_code, 200)
        again = self.api.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {data['token']}", secure=True)
        self.assertEqual(again.status_code, 401)

    def test_wrong_password_is_throttled_like_the_login_page(self):
        for _ in range(5):
            self.assertEqual(self.post("/api/v1/auth/token/", {"username": "alice", "password": "errada"}).status_code, 401)
        blocked = self.post("/api/v1/auth/token/", {"username": "alice", "password": PASSWORD})
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (429, "rate_limited"))

    def test_register_gives_a_key(self):
        response = self.post("/api/v1/auth/register/", {"full_name": "Bruno Lima", "username": "bruno",
                                                        "email": "bruno@example.com", "password": "Outra-senha-segura-77",
                                                        "device_name": "Moto G"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["user"]["username"], "bruno")
        bad = self.post("/api/v1/auth/register/", {"username": "bruno", "password": "1"})
        self.assertEqual(bad.status_code, 422)

    @override_settings(ALLOW_REGISTRATION=False)
    def test_register_closed(self):
        self.assertEqual(self.post("/api/v1/auth/register/", {"username": "x"}).status_code, 404)

    def test_tokens_screen_routes(self):
        response = self.client.post("/api/tokens/", json.dumps({"name": "Planilha", "scopes": ["read"], "expires_days": 30}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        listed = self.client.get("/api/tokens/").json()["tokens"]
        self.assertEqual([t["name"] for t in listed], ["Planilha"])
        self.assertEqual(self.client.delete(f"/api/tokens/{listed[0]['id']}/").status_code, 200)
        self.assertEqual(self.client.get("/api/tokens/").json()["tokens"], [])
        # Someone else's key cannot be revoked.
        _, _ = tokens.create(self.other, "deles")
        theirs = ApiToken.objects.get(user=self.other)
        self.assertEqual(self.client.delete(f"/api/tokens/{theirs.pk}/").status_code, 404)

    def test_a_key_cannot_mint_keys(self):
        _, key = tokens.create(self.user, "c", ("read", "write"))
        response = self.api.post("/api/v1/auth/tokens/", json.dumps({"name": "x"}), content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {key}", secure=True)
        self.assertEqual(response.status_code, 403)

"""Alteração 1: the /api/v1/ contract. Changing the keys of a read answer breaks clients that already
exist (phones with an older app): such a change needs /api/v2/."""
import re

from django.test import Client
from django.utils import timezone

from app import api_docs, tokens, urls_api_v1
from app.models import Goal, Person

from .base import AppTestCase

# Keys of each read answer, frozen. Adding a key is fine (add it here); removing or renaming is not.
FROZEN = {
    "/api/v1/transactions/": {"ok", "transactions", "count", "page", "pages", "page_size", "totals"},
    "/api/v1/accounts/": {"ok", "accounts"},
    "/api/v1/categories/": {"ok", "categories"},
    "/api/v1/tags/": {"ok", "tags"},
    "/api/v1/people/": {"ok", "people"},
    "/api/v1/recurring/": {"ok", "items"},
    "/api/v1/goals/": {"ok", "goals"},
    "/api/v1/auth/me/": {"ok", "user", "plan", "token"},
}
ITEM_KEYS = {"uid", "version", "updated_at", "updated_by", "id"}


class ApiContractTests(AppTestCase):
    def setUp(self):
        super().setUp()
        _, self.key = tokens.create(self.user, "contrato", ("read",))
        self.api = Client()
        self.tx()
        Person.objects.create(user=self.user, name="Ana")
        Goal.objects.create(user=self.user, name="Viagem", target_amount="100.00")

    def get(self, path):
        return self.api.get(path, HTTP_AUTHORIZATION=f"Bearer {self.key}", secure=True)

    def test_read_answers_keep_their_keys(self):
        for path, keys in FROZEN.items():
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            missing = keys - set(response.json())
            self.assertFalse(missing, f"{path} perdeu {missing}")

    def test_items_carry_identity_and_version(self):
        lists = {"/api/v1/transactions/": "transactions", "/api/v1/accounts/": "accounts", "/api/v1/categories/": "categories",
                 "/api/v1/people/": "people", "/api/v1/goals/": "goals", "/api/v1/tags/": "tags"}
        for path, key in lists.items():
            items = self.get(path).json()[key]
            for item in items:
                self.assertFalse(ITEM_KEYS - set(item), f"{path}: {ITEM_KEYS - set(item)}")

    def test_every_route_is_documented(self):
        spec = api_docs.load()["paths"]
        for pattern in urls_api_v1.urlpatterns:
            path = "/api/v1/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", str(pattern.pattern))
            self.assertIn(path, spec, f"{path} falta no openapi.json (rode scripts/gerar_openapi.py)")
            for method in getattr(pattern.callback, "api_methods", []):
                if method != "HEAD":
                    self.assertIn(method.lower(), spec[path], f"{method} {path}")

    def test_screen_only_routes_stay_out(self):
        self.assertEqual(self.get("/api/v1/onboarding/").status_code, 404)
        self.assertEqual(self.get("/api/v1/sample-data/").status_code, 404)

    def test_docs_pages(self):
        self.assertEqual(self.api.get("/api/v1/openapi.json").json()["openapi"], "3.1.0")
        page = self.api.get("/api/v1/docs/")
        self.assertContains(page, "/api/v1/transactions/")

    def test_today_is_used(self):
        # Sanity check of the helper used above, so a future date problem is easy to spot.
        self.assertEqual(self.tx().date, timezone.localdate())

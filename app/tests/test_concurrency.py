"""Alteração 3: a write based on an old version is refused (409) instead of erasing what someone
else saved in between; "force" overwrites on purpose; old clients without a version still work."""
import json
from decimal import Decimal

from app.models import (
    Budget, CategoryRule, Contribution, Goal, LedgerEntry, Person, Profile, RecurringTransaction, SavedView, Tag, Transaction,
    Transfer,
)
from app.sync.capture import acting_as

from .base import AppTestCase, today


def changed_elsewhere(obj):
    """What another tab (or the phone) does: save the record again, with another name."""
    with acting_as("Outra aba"):
        type(obj)._default_manager.filter(pk=obj.pk).update(updated_by="Outra aba")
    obj.refresh_from_db()
    return obj


class ConflictTests(AppTestCase):
    # --- builders: each returns (object, url, method, payload)
    def case_transaction_edit(self):
        tx = self.tx()
        return tx, f"/api/transactions/{tx.pk}/", "post", self.payload(name="Minha edição", account=self.account().pk)

    def case_transaction_delete(self):
        tx = self.tx()
        return tx, f"/api/transactions/{tx.pk}/", "delete", None

    def case_category_edit(self):
        cat = self.cat(name="Rent")
        return cat, f"/api/categories/{cat.pk}/", "post", {"name": "Aluguel novo", "kind": "expense", "color": "blue", "icon": "tag"}

    def case_account_edit(self):
        acct = self.account()
        return acct, f"/api/accounts/{acct.pk}/", "post", {"name": "Banco novo", "type": "bank", "opening_balance": "0"}

    def case_tag_rename(self):
        tag = Tag.objects.create(user=self.user, name="viagem")
        return tag, f"/api/tags/{tag.pk}/", "post", {"name": "viagens"}

    def case_rule_delete(self):
        rule = CategoryRule.objects.create(user=self.user, pattern="ifood", category=self.cat())
        return rule, f"/api/rules/{rule.pk}/", "delete", None

    def case_goal_edit(self):
        goal = Goal.objects.create(user=self.user, name="Viagem", target_amount=Decimal("100.00"))
        return goal, f"/api/goals/{goal.pk}/", "post", {"name": "Viagem longa", "target_amount": "200"}

    def case_contribution_delete(self):
        goal = Goal.objects.create(user=self.user, name="Reserva", target_amount=Decimal("100.00"))
        item = Contribution.objects.create(goal=goal, amount=Decimal("5.00"))
        return item, f"/api/contributions/{item.pk}/", "delete", None

    def case_recurring_toggle(self):
        item = RecurringTransaction.objects.create(user=self.user, name="Aluguel", amount=Decimal("100.00"), category=self.cat(),
                                                   start_date=today(), next_due=today())
        return item, f"/api/recurring/{item.pk}/toggle/", "post", {}

    def case_person_rename(self):
        person = Person.objects.create(user=self.user, name="Ana")
        return person, f"/api/people/{person.pk}/", "post", {"name": "Ana Paula"}

    def case_entry_delete(self):
        person = Person.objects.create(user=self.user, name="Bia")
        entry = LedgerEntry.objects.create(person=person, kind="lent", amount=Decimal("10.00"))
        return entry, f"/api/entries/{entry.pk}/", "delete", None

    def case_budget_delete(self):
        budget = Budget.objects.create(user=self.user, category=self.cat(), month=today().replace(day=1), amount=Decimal("50.00"))
        return budget, f"/api/budgets/{budget.pk}/", "delete", None

    def case_view_delete(self):
        view = SavedView.objects.create(user=self.user, name="Mercado", query="q=mercado")
        return view, f"/api/views/{view.pk}/", "delete", None

    def case_transfer_delete(self):
        second = self.make_account("Poupança")
        transfer = Transfer.objects.create(user=self.user, from_account=self.account(), to_account=second, amount=Decimal("1.00"))
        return transfer, f"/api/transfers/{transfer.pk}/", "delete", None

    def make_account(self, name):
        from app.models import Account
        return Account.objects.create(user=self.user, name=name, type="bank")

    CASES = [name for name in dir() if name.startswith("case_")]

    def send(self, method, url, payload, **extra):
        if method == "delete":
            query = "&".join(f"{k}={v}" for k, v in extra.items())
            return self.client.delete(url + (f"?{query}" if query else ""))
        return self.client.post(url, json.dumps({**(payload or {}), **extra}), content_type="application/json")

    def test_old_version_is_refused_and_nothing_changes(self):
        for name in self.CASES:
            with self.subTest(name):
                obj, url, method, payload = getattr(self, name)()
                opened = obj.version
                changed_elsewhere(obj)
                response = self.send(method, url, payload, version=opened)
                self.assertEqual(response.status_code, 409, response.content)
                data = response.json()
                self.assertEqual((data["code"], data["updated_by"], data["version"]), ("conflict", "Outra aba", opened + 1))
                self.assertEqual(data["message"], "Este registro foi alterado em outro lugar.")
                self.assertEqual(data["current"]["version"], opened + 1)
                still = type(obj)._base_manager.filter(pk=obj.pk).first()
                self.assertIsNotNone(still, "apagado mesmo com versão velha")
                self.assertEqual(still.version, opened + 1)

    def test_force_overwrites_on_purpose(self):
        for name in self.CASES:
            with self.subTest(name):
                obj, url, method, payload = getattr(self, name)()
                opened = obj.version
                changed_elsewhere(obj)
                forced = self.send(method, url, payload, version=opened, force="true")
                self.assertLess(forced.status_code, 300, forced.content)

    def test_current_version_goes_through(self):
        for name in self.CASES:
            with self.subTest(name):
                obj, url, method, payload = getattr(self, name)()
                response = self.send(method, url, payload, version=obj.version)
                self.assertLess(response.status_code, 300, response.content)

    def test_without_version_writes_as_before(self):
        obj, url, method, payload = self.case_transaction_edit()
        changed_elsewhere(obj)
        self.assertEqual(self.send(method, url, payload).status_code, 200)

    def test_two_saves_from_the_same_version_only_the_first_wins(self):
        tx, url, _, payload = self.case_transaction_edit()
        first = self.send("post", url, {**payload, "name": "Aba 1"}, version=tx.version)
        second = self.send("post", url, {**payload, "name": "Aba 2"}, version=tx.version)
        self.assertEqual((first.status_code, second.status_code), (200, 409))
        self.assertEqual(Transaction.objects.get(pk=tx.pk).name, "Aba 1")
        self.assertEqual(second.json()["current"]["name"], "Aba 1")

    def test_if_match_header_works_too(self):
        tx = self.tx()
        etag = self.client.get(f"/api/transactions/{tx.pk}/")["ETag"]
        self.assertEqual(etag, f'"{tx.version}"')
        changed_elsewhere(tx)
        response = self.client.delete(f"/api/transactions/{tx.pk}/", HTTP_IF_MATCH=etag)
        self.assertEqual(response.status_code, 409)

    def test_deleted_elsewhere_is_gone(self):
        tx = self.tx()
        version = tx.version
        Transaction.all_objects.filter(pk=tx.pk).delete()
        response = self.send("post", f"/api/transactions/{tx.pk}/", self.payload(), version=version)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "gone"))

    def test_bulk_skips_rows_changed_elsewhere(self):
        a, b = self.tx(name="A"), self.tx(name="B")
        versions = {str(a.pk): a.version, str(b.pk): b.version}
        changed_elsewhere(b)
        response = self.client.post("/api/transactions/bulk/", json.dumps({"action": "reconcile", "ids": [a.pk, b.pk],
                                                                            "versions": versions}), content_type="application/json")
        data = response.json()
        self.assertEqual((data["ids"], data["skipped"]), ([a.pk], [b.pk]))
        self.assertTrue(Transaction.objects.get(pk=a.pk).reconciled)
        self.assertFalse(Transaction.objects.get(pk=b.pk).reconciled)

    def test_in_use_errors_are_not_edit_conflicts(self):
        tx = self.tx()
        response = self.client.delete(f"/api/accounts/{tx.account_id}/")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "in_use"))

    def test_settings_changed_elsewhere_keeps_theirs(self):
        profile = Profile.objects.get(user=self.user)
        opened = profile.version
        Profile.objects.filter(pk=profile.pk).update(currency_symbol="US$")
        response = self.client.post("/settings/", {"version": opened, "display_name": "Alice", "currency_code": "BRL",
                                                   "currency_symbol": "R$", "number_format": "eu"}, follow=True)
        self.assertContains(response, "as configurações mudaram em outro lugar")
        self.assertEqual(Profile.objects.get(pk=profile.pk).currency_symbol, "US$")

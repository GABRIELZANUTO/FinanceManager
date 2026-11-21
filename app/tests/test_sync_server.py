"""Alteração 2, server side: push (applied, merged, conflict, remap, rejected), receipts, pull,
snapshot and the request checks."""
import json
import uuid
from decimal import Decimal

from django.core.cache import cache
from django.test import Client, override_settings

from app import tokens
from app.models import Budget, Category, MonthLock, Profile, SyncChange, Tag, Transaction
from app.sync import spec as specs
from app.sync.spec import UidMap

from .base import AppTestCase, today


class SyncServerTests(AppTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        _, self.key = tokens.create(self.user, "Celular Xiaomi", ("read", "write", "sync"))
        self.api = Client()

    # --- helpers
    def call(self, method, path, data=None, key=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key or self.key}", **extra}
        if method == "get":
            return self.api.get(path, data or {}, secure=True, **headers)
        return self.api.post(path, json.dumps(data), content_type="application/json", secure=True, **headers)

    def push(self, *ops, epoch=None):
        body = {"ops": list(ops)}
        if epoch is not None:
            body["epoch"] = epoch
        response = self.call("post", "/api/v1/sync/push/", body)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def op(self, model, uid, data=None, action="upsert", base_version=None, base=None, **extra):
        return {"op_id": str(uuid.uuid4()), "model": model, "uid": str(uid), "action": action,
                "base_version": base_version, "base": base, "data": data or {}, **extra}

    def data_of(self, obj):
        return specs.serialize(obj, UidMap(self.user))

    def tx_data(self, **overrides):
        data = {"name": "Do celular", "amount": "15.00", "date": today().isoformat(), "kind": "expense",
                "category": str(self.cat().uid), "account": str(self.account().uid), "note": "", "payee": "", "tags": []}
        data.update(overrides)
        return data

    # --- push
    def test_create_then_pull(self):
        uid = uuid.uuid4()
        cursor = Profile.objects.get(user=self.user).sync_seq
        [result] = self.push(self.op("transaction", uid, self.tx_data()))
        self.assertEqual(result["status"], "applied", result)
        tx = Transaction.objects.get(uid=uid)
        self.assertEqual((tx.user, tx.updated_by, tx.amount), (self.user, "Celular Xiaomi", Decimal("15.00")))
        pulled = self.call("get", "/api/v1/sync/pull/", {"cursor": cursor}).json()
        self.assertIn(str(uid), [c["item"]["uid"] for c in pulled["changes"]])

    def test_resend_gets_the_same_answer(self):
        op = self.op("transaction", uuid.uuid4(), self.tx_data())
        first = self.push(op)
        second = self.push(op)
        self.assertEqual(first, second)
        self.assertEqual(Transaction.objects.filter(name="Do celular").count(), 1)

    def test_edit_from_the_same_version(self):
        tx = self.tx(name="Antes")
        base = self.data_of(tx)
        [result] = self.push(self.op("transaction", tx.uid, {**base, "name": "Depois"}, base_version=tx.version, base=base))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(Transaction.objects.get(pk=tx.pk).name, "Depois")

    def test_different_fields_merge(self):
        tx = self.tx(name="Antes", note="")
        base, opened = self.data_of(tx), tx.version
        Transaction.objects.filter(pk=tx.pk).update(note="mudou no site")
        [result] = self.push(self.op("transaction", tx.uid, {**base, "name": "Mudou no celular"}, base_version=opened, base=base))
        self.assertEqual(result["status"], "merged", result)
        tx.refresh_from_db()
        self.assertEqual((tx.name, tx.note), ("Mudou no celular", "mudou no site"))
        self.assertEqual(result["item"]["version"], tx.version)

    def test_same_field_is_a_conflict_and_nothing_changes(self):
        tx = self.tx(name="Antes")
        base, opened = self.data_of(tx), tx.version
        Transaction.objects.filter(pk=tx.pk).update(name="Site")
        [result] = self.push(self.op("transaction", tx.uid, {**base, "name": "Celular"}, base_version=opened, base=base))
        self.assertEqual((result["status"], result["reason"], result["fields"]), ("conflict", "same_field", ["name"]))
        self.assertEqual(result["item"]["data"]["name"], "Site")
        self.assertEqual(Transaction.objects.get(pk=tx.pk).name, "Site")

    def test_grouped_fields_conflict_together(self):
        tx = self.tx(amount="10.00")
        base, opened = self.data_of(tx), tx.version
        Transaction.objects.filter(pk=tx.pk).update(kind="income", category=self.cat(name="Salary"))
        [result] = self.push(self.op("transaction", tx.uid, {**base, "amount": "99.00"}, base_version=opened, base=base))
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(set(result["fields"]), {"amount", "kind"})

    def test_force_overwrites(self):
        tx = self.tx(name="Antes")
        base, opened = self.data_of(tx), tx.version
        Transaction.objects.filter(pk=tx.pk).update(name="Site")
        [result] = self.push(self.op("transaction", tx.uid, {**base, "name": "Celular"}, base_version=opened, base=base, force=True))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(Transaction.objects.get(pk=tx.pk).name, "Celular")

    def test_tags_merge_as_sets(self):
        a, b, c = (Tag.objects.create(user=self.user, name=n) for n in ("a", "b", "c"))
        tx = self.tx()
        tx.tags.set([a, b])
        base, opened = self.data_of(tx), Transaction.objects.get(pk=tx.pk).version
        tx.tags.add(c)                              # the site added c
        mine = {**base, "tags": sorted([str(a.uid)])}   # the phone removed b
        [result] = self.push(self.op("transaction", tx.uid, mine, base_version=opened, base=base))
        self.assertEqual(result["status"], "merged", result)
        self.assertEqual(set(tx.tags.values_list("name", flat=True)), {"a", "c"})

    def test_delete(self):
        tx = self.tx()
        [stale] = self.push(self.op("transaction", tx.uid, action="delete", base_version=tx.version - 1 or 99))
        self.assertEqual(stale["status"], "conflict")
        [result] = self.push(self.op("transaction", tx.uid, action="delete", base_version=tx.version))
        self.assertEqual(result["status"], "applied")
        self.assertFalse(Transaction.all_objects.filter(pk=tx.pk).exists())
        [again] = self.push(self.op("transaction", tx.uid, action="delete", base_version=tx.version))
        self.assertEqual(again["status"], "applied")

    def test_edit_of_something_deleted_on_the_site(self):
        tx = self.tx()
        base, uid, opened = self.data_of(tx), tx.uid, tx.version
        Transaction.all_objects.filter(pk=tx.pk).delete()
        [result] = self.push(self.op("transaction", uid, base, base_version=opened, base=base))
        self.assertEqual((result["status"], result["reason"]), ("conflict", "deleted_on_server"))
        [forced] = self.push(self.op("transaction", uid, base, base_version=opened, base=base, force=True))
        self.assertEqual(forced["status"], "applied")
        self.assertTrue(Transaction.objects.filter(uid=uid).exists())

    def test_same_name_created_on_both_sides_is_remapped(self):
        Category.objects.create(user=self.user, name="Pet")
        phone_uid = uuid.uuid4()
        tx_uid = uuid.uuid4()
        results = self.push(
            self.op("category", phone_uid, {"name": "pet", "kind": "both", "color": "gray", "icon": "tag", "parent": None}),
            self.op("transaction", tx_uid, self.tx_data(category=str(phone_uid))),
        )
        self.assertEqual(results[0]["status"], "remap")
        server_uid = Category.objects.get(user=self.user, name="Pet").uid
        self.assertEqual(results[0]["item"]["uid"], str(server_uid))
        self.assertEqual(results[1]["status"], "applied", results[1])
        self.assertEqual(Transaction.objects.get(uid=tx_uid).category.uid, server_uid)
        self.assertEqual(Category.objects.filter(user=self.user, name__iexact="pet").count(), 1)

    def test_duplicate_budget_with_other_values_reports_it(self):
        cat = self.cat()
        Budget.objects.create(user=self.user, category=cat, month=today().replace(day=1), amount=Decimal("500.00"))
        data = {"category": str(cat.uid), "month": today().replace(day=1).isoformat(), "amount": "600.00", "rollover": False,
                "warn_percent": 80}
        [result] = self.push(self.op("budget", uuid.uuid4(), data))
        self.assertEqual(result["status"], "remap")
        self.assertEqual(result["conflict"]["reason"], "duplicate")
        self.assertIn("amount", result["conflict"]["fields"])

    def test_profile_is_the_users_own(self):
        profile = Profile.objects.get(user=self.user)
        [result] = self.push(self.op("profile", uuid.uuid4(), {"currency_code": "BRL"}))
        self.assertEqual((result["status"], result["item"]["uid"]), ("remap", str(profile.uid)))

    def test_someone_elses_uid_is_refused(self):
        theirs = self.tx(user=self.other)
        [result] = self.push(self.op("transaction", theirs.uid, self.tx_data()))
        self.assertEqual((result["status"], result["code"]), ("rejected", "uid_taken"))
        self.assertEqual(Transaction.objects.get(pk=theirs.pk).user, self.other)
        # and their category is not usable
        [ref] = self.push(self.op("transaction", uuid.uuid4(), self.tx_data(category=str(self.cat(self.other).uid))))
        self.assertEqual((ref["status"], ref["code"]), ("rejected", "missing_ref"))

    def test_missing_reference_and_nullable_reference(self):
        [result] = self.push(self.op("transaction", uuid.uuid4(), self.tx_data(category=str(uuid.uuid4()))))
        self.assertEqual((result["status"], result["code"]), ("rejected", "missing_ref"))
        self.assertEqual(result["ref"]["model"], "category")
        [nullable] = self.push(self.op("transaction", uuid.uuid4(), self.tx_data(account=str(uuid.uuid4()))))
        self.assertEqual(nullable["status"], "applied", nullable)
        self.assertIn("warning", nullable)

    def test_closed_month(self):
        MonthLock.objects.create(user=self.user, month=today().replace(day=1))
        [result] = self.push(self.op("transaction", uuid.uuid4(), self.tx_data()))
        self.assertEqual((result["status"], result["code"]), ("rejected", "month_locked"))

    def test_invalid_data(self):
        [result] = self.push(self.op("transaction", uuid.uuid4(), self.tx_data(amount="-5.00")))
        self.assertEqual((result["status"], result["code"]), ("rejected", "invalid"))

    def test_recurring_occurrence_from_both_sides(self):
        from app.models import RecurringTransaction
        item = RecurringTransaction.objects.create(user=self.user, name="Aluguel", amount=Decimal("100.00"), category=self.cat(),
                                                   start_date=today(), next_due=today())
        site = Transaction.objects.create(user=self.user, name="Aluguel", amount=Decimal("100.00"), category=self.cat(),
                                          account=self.account(), recurring=item, recurring_date=today())
        same_uid = Transaction(recurring=item, recurring_date=today())
        from app.models import occurrence_uid
        self.assertEqual(site.uid, occurrence_uid(item.uid, today()))
        data = {**self.data_of(site), "note": "pago no celular"}
        [result] = self.push(self.op("transaction", site.uid, data))
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(Transaction.all_objects.filter(recurring=item).count(), 1)
        del same_uid

    # --- checks
    def test_scope_epoch_and_version(self):
        _, plain = tokens.create(self.user, "planilha", ("read", "write"))
        self.assertEqual(self.call("post", "/api/v1/sync/push/", {"ops": []}, key=plain).status_code, 403)
        response = self.call("post", "/api/v1/sync/push/", {"ops": [], "epoch": 99})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "epoch_changed"))
        old = self.call("get", "/api/v1/sync/pull/", {"cursor": 0}, HTTP_X_SYNC_VERSION="0")
        self.assertEqual((old.status_code, old.json()["code"]), (426, "upgrade_required"))

    @override_settings(SYNC_REQUIRE_PLAN=True)
    def test_plan(self):
        response = self.call("get", "/api/v1/sync/pull/", {"cursor": 0})
        self.assertEqual((response.status_code, response.json()["code"]), (402, "plan_required"))
        Profile.objects.filter(user=self.user).update(plan="sync")
        self.assertEqual(self.call("get", "/api/v1/sync/pull/", {"cursor": 0}).status_code, 200)

    # --- pull and snapshot
    def test_pull_pages_without_skipping(self):
        for i in range(7):
            self.tx(name=f"t{i}")
        seen, cursor = set(), 0
        while True:
            page = self.call("get", "/api/v1/sync/pull/", {"cursor": cursor, "limit": 3}).json()
            seen |= {(c["model"], c["item"]["uid"]) for c in page["changes"]}
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        expected = {(r.model, str(r.uid)) for r in SyncChange.objects.filter(user=self.user)}
        self.assertEqual(seen, expected)
        self.assertNotIn(str(self.tx(user=self.other).uid), {u for _, u in seen})

    def test_pull_reports_deletions(self):
        tx = self.tx()
        cursor = Profile.objects.get(user=self.user).sync_seq
        Transaction.all_objects.filter(pk=tx.pk).delete()
        page = self.call("get", "/api/v1/sync/pull/", {"cursor": cursor}).json()
        self.assertEqual(page["changes"], [{"model": "transaction", "seq": cursor + 1, "deleted": True, "item": {"uid": str(tx.uid)}}])

    def test_snapshot(self):
        for i in range(5):
            self.tx(name=f"s{i}")
        child = Category.objects.create(user=self.user, name="Filha")
        parent = Category.objects.create(user=self.user, name="Mãe")
        Category.objects.filter(pk=child.pk).update(parent=parent)
        start = self.call("get", "/api/v1/sync/snapshot/").json()
        self.assertEqual(start["models"].index("category") < start["models"].index("transaction"), True)
        self.assertEqual(start["counts"]["transaction"], 5)
        self.assertEqual(start["cursor"], Profile.objects.get(user=self.user).sync_seq)
        got, after = [], 0
        while True:
            page = self.call("get", "/api/v1/sync/snapshot/", {"model": "transaction", "after": after, "limit": 2}).json()
            got += page["items"]
            after = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual(len(got), 5)
        cats = [i["uid"] for i in self.call("get", "/api/v1/sync/snapshot/", {"model": "category"}).json()["items"]]
        self.assertLess(cats.index(str(parent.uid)), cats.index(str(child.uid)))

    def test_object(self):
        tx = self.tx()
        ok = self.call("get", "/api/v1/sync/object/", {"model": "transaction", "uid": str(tx.uid)}).json()
        self.assertEqual(ok["item"]["uid"], str(tx.uid))
        gone = self.call("get", "/api/v1/sync/object/", {"model": "transaction", "uid": str(uuid.uuid4())})
        self.assertEqual((gone.status_code, gone.json()["code"]), (404, "gone"))


class SyncSpecTests(AppTestCase):
    def test_every_owned_model_is_in_the_spec_in_dependency_order(self):
        from django.apps import apps
        names = [s.name for s in specs.specs()]
        owned = [m._meta.model_name for m in apps.get_app_config("app").get_models()
                 if any(f.name == "user" for f in m._meta.fields) or m._meta.model_name in specs.OWNER]
        missing = set(owned) - set(names) - {"auditentry", "syncchange", "apitoken", "syncreceipt"}
        self.assertFalse(missing, f"fora do spec: {missing}")
        for s in specs.specs():
            for target in list(s.fks.values()) + list(s.m2m.values()):
                if target != s.name:
                    self.assertLess(names.index(target), names.index(s.name), f"{target} antes de {s.name}")

    def test_round_trip_of_every_model(self):
        from app.models import Goal, LedgerEntry, Person, RecurringTransaction
        tag = Tag.objects.create(user=self.user, name="x")
        tx = self.tx(note="nota", payee="Loja")
        tx.tags.add(tag)
        person = Person.objects.create(user=self.user, name="Ana")
        LedgerEntry.objects.create(person=person, kind="lent", amount=Decimal("3.00"), transaction=tx)
        Goal.objects.create(user=self.user, name="Meta", target_amount=Decimal("10.00")).contributions.create(amount=Decimal("1.00"))
        RecurringTransaction.objects.create(user=self.user, name="R", amount=Decimal("1.00"), category=self.cat(),
                                            start_date=today(), next_due=today())
        uids = UidMap(self.user)
        for s in specs.specs():
            for obj in s.owned(self.user):
                data = specs.serialize(obj, uids)
                json.dumps(data)   # plain JSON
                values, m2m = specs.deserialize(s, data, uids)
                for key, value in values.items():
                    self.assertEqual(getattr(obj, key), value, f"{s.name}.{key}")
                for key, pks in m2m.items():
                    self.assertEqual(sorted(pks), sorted(getattr(obj, key).values_list("pk", flat=True)))

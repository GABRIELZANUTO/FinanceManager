"""Etapa 0 of the sync plan: uid, version, last writer and the change log."""
import json
from decimal import Decimal

from django.db.models import F

from app import backup, services
from app.models import Category, Goal, LedgerEntry, Person, Profile, SyncChange, Tag, Transaction
from app.sync.capture import acting_as, remote_writes, sync_models

from .base import AppTestCase, User


class SyncBaseTests(AppTestCase):
    def changes(self, user=None):
        return SyncChange.objects.filter(user=user or self.user)

    def change_of(self, obj):
        return SyncChange.objects.get(uid=obj.uid)

    def test_every_owned_model_is_synchronised(self):
        names = {m._meta.model_name for m in sync_models()}
        self.assertEqual(len(names), 17)
        self.assertNotIn("auditentry", names)
        self.assertNotIn("syncchange", names)

    def test_uid_is_unique_and_stable(self):
        tx = self.tx()
        uid = tx.uid
        tx.name = "Outro"
        tx.save()
        tx.refresh_from_db()
        self.assertEqual(tx.uid, uid)
        self.assertNotEqual(self.tx().uid, uid)

    def test_version_goes_up_on_every_kind_of_write(self):
        tx = self.tx()
        self.assertEqual(tx.version, 1)
        tx.name = "Dois"
        tx.save()
        self.assertEqual(tx.version, 2)
        tx.note = "três"
        tx.save(update_fields=["note"])
        tx.refresh_from_db()
        self.assertEqual((tx.version, tx.note), (3, "três"))
        Transaction.objects.filter(pk=tx.pk).update(reconciled=True)
        tx.refresh_from_db()
        self.assertEqual(tx.version, 4)
        tag = Tag.objects.create(user=self.user, name="viagem")
        tx.tags.add(tag)
        tx.refresh_from_db()
        self.assertEqual(tx.version, 5)
        tag.transactions.remove(tx)    # from the other side of the relation
        tx.refresh_from_db()
        self.assertEqual(tx.version, 6)

    def test_stale_instance_does_not_reuse_a_version(self):
        tx = self.tx()
        stale = Transaction.objects.get(pk=tx.pk)
        Transaction.objects.filter(pk=tx.pk).update(name="mudou")
        stale.note = "x"
        stale.save()
        self.assertEqual(stale.version, 3)

    def test_sequence_grows_per_user_without_repeating(self):
        start = Profile.objects.get(user=self.user).sync_seq
        a = self.tx()
        b = self.tx()
        a.name = "de novo"
        a.save()
        self.tx(user=self.other)
        seqs = list(self.changes().filter(seq__gt=start).order_by("seq").values_list("seq", flat=True))
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(Profile.objects.get(user=self.user).sync_seq, max(seqs))
        # One row per object: the second save moved "a" to a later number.
        self.assertGreater(self.change_of(a).seq, self.change_of(b).seq)
        self.assertFalse(self.changes().filter(uid=self.other.transactions.get().uid).exists())

    def test_trash_and_restore_are_logged(self):
        tx = self.tx()
        before = self.change_of(tx).seq
        services.delete_transactions(self.user, Transaction.objects.filter(pk=tx.pk))
        after_trash = self.change_of(tx).seq
        self.assertGreater(after_trash, before)
        self.assertFalse(self.change_of(tx).deleted)   # the trash is an edit, not a deletion
        services.restore_transactions(self.user, [tx.pk])
        self.assertGreater(self.change_of(tx).seq, after_trash)
        Transaction.all_objects.filter(pk=tx.pk).delete()
        self.assertTrue(self.change_of(tx).deleted)

    def test_deleting_a_person_marks_the_person_and_the_entries(self):
        person = Person.objects.create(user=self.user, name="Ana")
        entries = [LedgerEntry.objects.create(person=person, kind="lent", amount=Decimal("10.00")) for _ in range(2)]
        person.delete()
        for obj in [person] + entries:
            change = self.change_of(obj)
            self.assertTrue(change.deleted)
            self.assertEqual(change.user_id, self.user.pk)

    def test_child_rows_belong_to_the_owner_of_the_parent(self):
        goal = Goal.objects.create(user=self.user, name="Viagem", target_amount=Decimal("100.00"))
        contribution = goal.contributions.create(amount=Decimal("5.00"))
        self.assertEqual(self.change_of(contribution).user_id, self.user.pk)

    def test_bulk_create_is_logged(self):
        made = Category.objects.bulk_create([Category(user=self.user, name="Em lote")])
        self.assertTrue(self.changes().filter(uid=made[0].uid).exists())

    def test_category_merge_through_update_is_logged(self):
        tx = self.tx()
        target = self.cat(name="Rent")
        Transaction.all_objects.filter(category=tx.category).update(category=target)
        tx.refresh_from_db()
        self.assertEqual((tx.category, tx.version), (target, 2))

    def test_writer_name(self):
        with acting_as("Celular Xiaomi"):
            tx = self.tx()
        self.assertEqual(tx.updated_by, "Celular Xiaomi")
        self.client.post("/api/transactions/", json.dumps({
            "name": "Pelo site", "amount": "3.00", "kind": "expense", "category": self.cat().pk,
            "account": self.account().pk, "date": tx.date.isoformat()}), content_type="application/json",
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36")
        self.assertEqual(Transaction.objects.get(name="Pelo site").updated_by, "Chrome no Windows")

    def test_remote_writes_keep_version_and_skip_the_log(self):
        tx = self.tx()
        seq = self.change_of(tx).seq
        with remote_writes():
            Transaction.objects.filter(pk=tx.pk).update(name="do servidor", version=9)
            tx.refresh_from_db()
            tx.save()
        tx.refresh_from_db()
        self.assertEqual(tx.version, 9)
        self.assertEqual(self.change_of(tx).seq, seq)

    def test_deleting_a_user_with_data_works(self):
        self.tx(user=self.other)
        Person.objects.create(user=self.other, name="Bia")
        self.other.delete()
        self.assertFalse(User.objects.filter(username="mallory").exists())
        self.assertFalse(SyncChange.objects.filter(user_id=self.other.pk).exists())

    def test_backup_keeps_uid_and_version(self):
        tx = self.tx()
        tx.name = "editada"
        tx.save()
        person = Person.objects.create(user=self.user, name="Caio")
        entry = LedgerEntry.objects.create(person=person, kind="lent", amount=Decimal("4.00"))
        raw = backup.dumps(self.user).encode()
        backup.restore(self.user, backup.parse(raw))
        restored = Transaction.objects.get(user=self.user, name="editada")
        self.assertEqual((restored.uid, restored.version), (tx.uid, 2))
        self.assertEqual(LedgerEntry.objects.get(person__user=self.user).uid, entry.uid)
        self.assertTrue(self.change_of(restored).seq > 0)
        self.assertFalse(self.change_of(restored).deleted)

    def test_restore_into_another_account_never_takes_a_used_uid(self):
        tx = self.tx()
        raw = backup.dumps(self.user).encode()
        backup.restore(self.other, backup.parse(raw))
        copy = Transaction.objects.get(user=self.other, name=tx.name)
        self.assertNotEqual(copy.uid, tx.uid)
        self.assertEqual(Transaction.objects.get(pk=tx.pk).uid, tx.uid)

    def test_old_backup_without_ids_still_restores(self):
        self.tx(name="antiga")
        data = backup.export_data(self.user)
        for value in data.values():
            for row in value if isinstance(value, list) else []:
                if isinstance(row, dict):
                    row.pop("uid", None)
                    row.pop("version", None)
        data.pop("tag_ids")
        backup.restore(self.user, backup.parse(json.dumps(data).encode()))
        self.assertTrue(Transaction.objects.filter(user=self.user, name="antiga").exists())

    def test_profile_counter_is_not_a_change(self):
        before = Profile.objects.get(user=self.user)
        self.tx()
        after = Profile.objects.get(user=self.user)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.sync_seq, Profile.objects.filter(pk=after.pk).values_list(F("sync_seq"), flat=True)[0])

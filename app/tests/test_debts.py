import datetime
from decimal import Decimal

from app import debts, services, stats
from app.models import LedgerEntry, Person, Transaction

from .base import AppTestCase, today

D = Decimal


class DebtMoneyTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.account()
        self.bank.opening_balance = D("1000.00")
        self.bank.save()
        self.ana = Person.objects.create(user=self.user, name="Ana")

    def entry(self, **data):
        r = self.post(f"/api/people/{self.ana.pk}/entries/", data)
        self.assertEqual(r.status_code, 201, r.content)
        return self.json(r)["person"]

    def balance(self):
        return services.account_balance(self.bank)

    def income_expense(self):
        return services.income_expense(self.user)

    def test_lending_and_being_paid_back_move_the_account_like_a_transfer(self):
        person = self.entry(kind="lent", amount="500", account=self.bank.pk)
        self.assertEqual((person["net"], self.balance()), ("500.00", D("500.00")))
        self.assertEqual(self.income_expense(), (D("0.00"), D("0.00")))
        self.entry(kind="paid_me", amount="200", account=self.bank.pk)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("300.00"), D("700.00")))
        self.assertEqual(services.balance_for(self.user), D("700.00"))

    def test_older_entries_without_account_do_not_touch_balances(self):
        self.entry(kind="lent", amount="500")
        self.assertEqual((debts.net(self.ana), self.balance()), (D("500.00"), D("1000.00")))

    def test_you_paid_a_split_bill(self):
        food = self.cat(name="Dining out")
        person = self.entry(kind="split_me", total="200", my_share="80", account=self.bank.pk, category=food.pk,
                            description="Jantar")
        self.assertEqual(person["net"], "120.00")
        self.assertEqual(self.balance(), D("800.00"))
        share = Transaction.objects.get(user=self.user)
        self.assertEqual((share.amount, share.category, share.name), (D("80.00"), food, "Jantar"))
        self.assertEqual(self.income_expense(), (D("0.00"), D("80.00")))
        entry = LedgerEntry.objects.get(person=self.ana)
        self.assertEqual(self.delete(f"/api/entries/{entry.pk}/").status_code, 200)
        self.assertEqual((self.balance(), Transaction.objects.filter(user=self.user).count()), (D("1000.00"), 0))

    def test_the_other_person_paid_a_split_bill(self):
        food = self.cat(name="Dining out")
        self.entry(kind="split_them", total="200", my_share="80", account=self.bank.pk, category=food.pk)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("-80.00"), D("1000.00")))   # nothing left the bank
        self.assertEqual(self.income_expense(), (D("0.00"), D("80.00")))                     # but you spent it
        self.entry(kind="paid_them", amount="80", account=self.bank.pk)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("0.00"), D("920.00")))
        self.assertEqual(self.income_expense(), (D("0.00"), D("80.00")))                     # not twice

    def test_split_share_needs_a_category_when_it_moves_an_account(self):
        r = self.post(f"/api/people/{self.ana.pk}/entries/", {"kind": "split_me", "total": "200", "account": self.bank.pk})
        self.assertEqual(r.status_code, 422)

    def test_installments_are_planned_until_confirmed(self):
        first = today() + datetime.timedelta(days=20)
        person = self.entry(kind="lent", amount="500", account=self.bank.pk, installments=5, first_due=first.isoformat())
        self.assertEqual(len(person["planned"]), 5)
        self.assertEqual({p["amount"] for p in person["planned"]}, {"-100.00"})
        self.assertEqual((person["net"], person["next_due"]), ("500.00", first.isoformat()))
        planned = LedgerEntry.objects.filter(person=self.ana, planned=True).order_by("date")
        self.assertEqual(planned[1].date, services.add_months(first, 1, anchor_day=first.day))
        r = self.post(f"/api/entries/{planned[0].pk}/confirm/", {"account": self.bank.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("400.00"), D("600.00")))

    def test_installments_without_a_loan(self):
        person = self.entry(kind="paid_me", amount="190", account=self.bank.pk, installments=3,
                            first_due=(today() + datetime.timedelta(days=5)).isoformat())
        self.assertEqual([p["amount"] for p in person["planned"]], ["-190.00"] * 3)
        self.assertEqual(person["net"], "0.00")

    def test_settle_pays_the_whole_balance_and_drops_planned_installments(self):
        self.entry(kind="lent", amount="300", account=self.bank.pk, installments=3,
                   first_due=(today() + datetime.timedelta(days=5)).isoformat())
        r = self.post(f"/api/people/{self.ana.pk}/settle/", {"account": self.bank.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("0.00"), D("1000.00")))
        self.assertFalse(LedgerEntry.objects.filter(person=self.ana, planned=True).exists())
        self.assertEqual(self.post(f"/api/people/{self.ana.pk}/settle/", {}).status_code, 422)

    def test_split_among_several_people(self):
        bia = Person.objects.create(user=self.user, name="Bia")
        caio = Person.objects.create(user=self.user, name="Caio")
        r = self.post("/api/people/split/", {"people": [self.ana.pk, bia.pk, caio.pk], "total": "400.01",
                                             "account": self.bank.pk, "category": self.cat(name="Dining out").pk,
                                             "description": "Churrasco"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.balance(), D("599.99"))
        self.assertEqual(self.income_expense()[1], D("100.00"))
        self.assertEqual(sorted(debts.net(p) for p in (self.ana, bia, caio)), [D("100.00"), D("100.00"), D("100.01")])

    def test_edit_moves_the_money_difference(self):
        self.entry(kind="lent", amount="500", account=self.bank.pk)
        entry = LedgerEntry.objects.get(person=self.ana)
        r = self.post(f"/api/entries/{entry.pk}/", {"amount": "600", "account": self.bank.pk, "description": "carro"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("600.00"), D("400.00")))
        entry.refresh_from_db()
        self.assertEqual((entry.kind, entry.description), ("lent", "carro"))

    def test_edit_a_split_bill_updates_your_share(self):
        food = self.cat(name="Dining out")
        self.entry(kind="split_me", total="200", my_share="80", account=self.bank.pk, category=food.pk)
        entry = LedgerEntry.objects.get(person=self.ana)
        r = self.post(f"/api/entries/{entry.pk}/", {"total": "300", "my_share": "100", "account": self.bank.pk,
                                                     "category": food.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((debts.net(self.ana), self.balance()), (D("200.00"), D("700.00")))
        self.assertEqual(Transaction.objects.get(user=self.user).amount, D("100.00"))

    def test_person_with_money_moved_cannot_be_deleted_blindly(self):
        self.entry(kind="lent", amount="50", account=self.bank.pk)
        self.assertEqual(self.delete(f"/api/people/{self.ana.pk}/").status_code, 409)

    def test_summary_text(self):
        self.entry(kind="lent", amount="450", account=self.bank.pk, description="aluguel")
        text = self.json(self.client.get(f"/api/people/{self.ana.pk}/summary/"))["text"]
        self.assertIn("você me deve R$ 450,00", text.replace("\xa0", " "))
        self.assertIn("aluguel", text)

    def test_dashboard_money_counts_debts(self):
        self.entry(kind="lent", amount="300", account=self.bank.pk)
        data = stats.summary(self.user, services.month_start(today()))
        self.assertEqual(data["money_today"], D("700.00"))

    def test_other_users_entries_are_protected(self):
        foreign = Person.objects.create(user=self.other, name="X")
        entry = LedgerEntry.objects.create(person=foreign, kind="lent", amount=D("10"))
        self.assertEqual(self.post(f"/api/entries/{entry.pk}/", {"amount": "1"}).status_code, 404)
        self.assertEqual(self.post(f"/api/people/{foreign.pk}/settle/", {}).status_code, 404)


class DebtPlanTests(AppTestCase):
    def test_installments_enter_the_month_plan_and_the_bank_projection(self):
        from app import insights, planning
        bank = self.account()
        bank.opening_balance = D("1000.00")
        bank.save()
        ana = Person.objects.create(user=self.user, name="Ana")
        now = today()
        first = services.add_months(services.month_start(now), 1).replace(day=10)
        r = self.post(f"/api/people/{ana.pk}/entries/", {"kind": "lent", "amount": "300", "account": bank.pk,
                                                         "installments": 3, "first_due": first.isoformat()})
        self.assertEqual(r.status_code, 201, r.content)
        this = planning.month_plan(self.user, now)
        self.assertEqual(this["totals"]["debts"], D("-300.00"))            # the loan left the bank this month
        self.assertEqual(this["balance"]["end"] - this["balance"]["start"], this["totals"]["result"] - 300)
        nxt = planning.month_plan(self.user, first)
        rows = [r for r in nxt["debts"] if r["planned"]]
        self.assertEqual([(r["person"], r["amount"], r["receive"]) for r in rows], [("Ana", D("100.00"), True)])
        self.assertEqual(nxt["totals"]["debts"], D("100.00"))
        self.assertEqual(nxt["bank"]["end"] - nxt["bank"]["now"], D("100.00"))
        self.assertEqual(nxt["balance"]["start"], this["balance"]["end"])  # the chain carries on
        self.assertEqual(nxt["totals"]["income"], D("0.00"))              # being paid back is not income
        soon = [b for b in insights.bills(self.user, first - datetime.timedelta(days=3)) if b["type"] == "debt"]
        self.assertEqual([b["name"] for b in soon], ["Ana te paga"])

    def test_paying_for_someone_on_the_card_goes_into_the_invoice(self):
        from app import planning
        from app.models import Account
        card = Account.objects.create(user=self.user, name="Nubank", type="credit", due_day=28)
        ana = Person.objects.create(user=self.user, name="Ana")
        self.post(f"/api/people/{ana.pk}/entries/", {"kind": "lent", "amount": "120", "account": card.pk})
        due = planning.next_due_date(card, today())
        plan = planning.month_plan(self.user, due)
        invoice = next(i for i in plan["invoices"] if i["account"] == "Nubank")
        self.assertEqual(invoice["amount"], D("120.00"))


class DebtBackupTests(AppTestCase):
    def test_account_planned_and_group_survive_a_backup(self):
        from app import backup
        bank = self.account()
        ana = Person.objects.create(user=self.user, name="Ana")
        self.post(f"/api/people/{ana.pk}/entries/", {"kind": "lent", "amount": "300", "account": bank.pk, "installments": 3,
                                                     "first_due": (today() + datetime.timedelta(days=9)).isoformat()})
        parsed = backup.parse(backup.dumps(self.user).encode("utf-8"))
        fresh = self.make_user("bob")
        backup.restore(fresh, parsed)
        entries = LedgerEntry.objects.filter(person__user=fresh)
        self.assertEqual(entries.filter(planned=True).count(), 3)
        self.assertEqual(entries.filter(account__name=bank.name).count(), 4)
        self.assertEqual(len({e.group for e in entries}), 1)
        self.assertEqual(debts.net(Person.objects.get(user=fresh)), D("300.00"))


class DebtEditSplitThemTests(AppTestCase):
    def test_edit_your_share_of_a_bill_the_other_person_paid(self):
        bank = self.account()
        ana = Person.objects.create(user=self.user, name="Ana")
        food = self.cat(name="Dining out")
        self.post(f"/api/people/{ana.pk}/entries/", {"kind": "split_them", "total": "200", "my_share": "80",
                                                     "account": bank.pk, "category": food.pk})
        entry = LedgerEntry.objects.get(person=ana)
        r = self.post(f"/api/entries/{entry.pk}/", {"my_share": "90", "account": bank.pk, "category": food.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(debts.net(ana), D("-90.00"))
        self.assertEqual(Transaction.objects.get(user=self.user).amount, D("90.00"))
        self.assertEqual(services.account_balance(bank), D("0.00"))

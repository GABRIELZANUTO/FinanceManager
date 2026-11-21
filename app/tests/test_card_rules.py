"""Default account (item 7), card closing day (item 6) and the card date rule (item 2)."""
import datetime
from decimal import Decimal

from app import planning, quick, services
from app.models import Account, RecurringTransaction, Transaction

from .base import AppTestCase

D = datetime.date


class DefaultAccountTests(AppTestCase):
    def test_first_bank_of_the_list_unless_one_is_chosen(self):
        card = Account.objects.create(user=self.user, name="Aaa cartão", type="credit")
        self.assertEqual(services.default_account(self.user).type, "bank")      # never a card by accident
        profile = services.get_profile(self.user)
        profile.default_account = card
        profile.save()
        self.assertEqual(services.default_account(self.user), card)
        card.archived = True
        card.save()
        self.assertEqual(services.default_account(self.user).type, "bank")      # archived: back to the list

    def test_settings_page_saves_the_choice_and_lists_only_real_accounts(self):
        card = Account.objects.create(user=self.user, name="Nubank", type="credit")
        r = self.client.post("/settings/", {"display_name": "Alice", "currency_code": "BRL", "currency_symbol": "R$",
                                            "number_format": "eu", "default_account": card.pk,
                                            "card_date_mode": "purchase"})
        self.assertEqual(r.status_code, 302)
        services.forget_profile(self.user)
        profile = services.get_profile(self.user)
        self.assertEqual((profile.default_account, profile.card_date_mode), (card, "purchase"))
        self.assertNotContains(self.client.get("/recurring/"), "Conta padrão</option>")


class ClosingDayTests(AppTestCase):
    def test_purchase_after_closing_goes_to_the_next_invoice(self):
        card = Account(name="x", type="credit", due_day=10, closing_day=3)
        self.assertEqual(planning.invoice_due_for(card, D(2026, 11, 3)), D(2026, 11, 10))
        self.assertEqual(planning.invoice_due_for(card, D(2026, 11, 4)), D(2026, 12, 10))
        late_close = Account(name="y", type="credit", due_day=5, closing_day=27)
        self.assertEqual(planning.invoice_due_for(late_close, D(2026, 11, 27)), D(2026, 12, 5))
        self.assertEqual(planning.invoice_due_for(late_close, D(2026, 11, 28)), D(2027, 1, 5))
        # Without a closing day the card closes 7 days before the due day (10 -> 3).
        no_closing = Account(name="z", type="credit", due_day=10)
        self.assertEqual(planning.invoice_due_for(no_closing, D(2026, 11, 3)), D(2026, 11, 10))
        self.assertEqual(planning.invoice_due_for(no_closing, D(2026, 11, 4)), D(2026, 12, 10))

    def test_default_closing_is_seven_days_before_the_due_day(self):
        cases = {10: 3, 8: 1, 7: 30, 5: 28, 1: 24, 31: 24, 15: 8}
        for due, closing in cases.items():
            self.assertEqual(planning.default_closing_day(due), closing, due)
        # Due on the 5th, closes on the 28th of the month before.
        card = Account(name="w", type="credit", due_day=5)
        self.assertEqual(planning.invoice_due_for(card, D(2026, 10, 28)), D(2026, 11, 5))
        self.assertEqual(planning.invoice_due_for(card, D(2026, 10, 29)), D(2026, 12, 5))
        self.assertIsNone(planning.closing_day(Account(name="v", type="credit")))

    def test_migration_fills_the_closing_day_of_existing_cards(self):
        import importlib
        from django.apps import apps as real_apps
        migration = importlib.import_module("app.migrations.0019_card_default_closing")
        set_day = Account.objects.create(user=self.user, name="Com dia", type="credit", due_day=10, closing_day=2)
        without = Account.objects.create(user=self.user, name="Sem fechamento", type="credit", due_day=12)
        unknown = Account.objects.create(user=self.user, name="Sem nada", type="credit")
        bank = Account.objects.create(user=self.user, name="Banco x", type="bank")
        migration.fill(real_apps, None)
        for acct, expected in ((set_day, 2), (without, 5), (unknown, None), (bank, None)):
            acct.refresh_from_db()
            self.assertEqual(acct.closing_day, expected, acct.name)

    def test_invoice_estimate_uses_the_closing_day(self):
        card = Account.objects.create(user=self.user, name="Nubank", type="credit", due_day=10, closing_day=3)
        profile = services.get_profile(self.user)
        profile.card_date_mode = "purchase"
        profile.save()
        self.tx(amount="100", account=card, date=D(2026, 11, 2))       # closes on 3/11: invoice of 10/11
        self.tx(amount="40", account=card, date=D(2026, 11, 5))        # after the closing: invoice of 10/12
        nov = planning.month_plan(self.user, D(2026, 11, 1), today=D(2026, 11, 6))
        dec = planning.month_plan(self.user, D(2026, 12, 1), today=D(2026, 11, 6))
        self.assertEqual(next(i for i in nov["invoices"] if i["account"] == "Nubank")["amount"], Decimal("100.00"))
        self.assertEqual(next(i for i in dec["invoices"] if i["account"] == "Nubank")["amount"], Decimal("40.00"))

    def test_closing_day_needs_a_due_day_and_must_differ(self):
        r = self.post("/api/accounts/", {"name": "C1", "type": "credit", "opening_balance": "0", "closing_day": 3})
        self.assertEqual(r.status_code, 422)
        r = self.post("/api/accounts/", {"name": "C2", "type": "credit", "opening_balance": "0", "closing_day": 3,
                                         "due_day": 3})
        self.assertEqual(r.status_code, 422)
        r = self.post("/api/accounts/", {"name": "C3", "type": "credit", "opening_balance": "0", "closing_day": 3,
                                         "due_day": 10})
        self.assertEqual(self.json(r)["account"]["closing_day"], 3)


class CardDateRuleTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit", due_day=10, closing_day=3)

    def mode(self, value):
        profile = services.get_profile(self.user)
        profile.card_date_mode = value
        profile.save()

    def create(self, date, **extra):
        r = self.post("/api/transactions/", self.payload(account=self.card.pk, date=date.isoformat(), **extra))
        self.assertEqual(r.status_code, 201, r.content)
        return Transaction.objects.get(pk=self.json(r)["transaction"]["id"])

    def test_invoice_month_keeps_the_real_day_in_the_note(self):
        today = services.timezone.localdate()
        tx = self.create(today)
        due = planning.invoice_due_for(self.card, today)
        self.assertEqual((tx.date, tx.purchase_date), (due, today))
        self.assertIn(f"Data da compra: {today:%d/%m/%Y}.", tx.note)
        data = self.json(self.client.get(f"/api/transactions/{tx.pk}/"))["transaction"]
        self.assertEqual(data["purchase_date"], today.isoformat())

    def test_purchase_month_keeps_the_day(self):
        self.mode("purchase")
        today = services.timezone.localdate()
        tx = self.create(today)
        self.assertEqual((tx.date, tx.purchase_date, tx.note), (today, today, ""))

    def test_edit_keeps_the_booking_unless_the_purchase_day_changes(self):
        today = services.timezone.localdate()
        tx = self.create(today)
        booked = tx.date
        r = self.post(f"/api/transactions/{tx.pk}/", self.payload(account=self.card.pk, date=today.isoformat(), name="Outro"))
        self.assertEqual(r.status_code, 200, r.content)
        tx.refresh_from_db()
        self.assertEqual((tx.date, tx.name), (booked, "Outro"))
        earlier = today - datetime.timedelta(days=40)
        self.post(f"/api/transactions/{tx.pk}/", self.payload(account=self.card.pk, date=earlier.isoformat()))
        tx.refresh_from_db()
        self.assertEqual((tx.date, tx.purchase_date), (planning.invoice_due_for(self.card, earlier), earlier))
        self.assertEqual(tx.note.count("Data da compra"), 1)

    def test_other_accounts_are_not_touched(self):
        today = services.timezone.localdate()
        r = self.post("/api/transactions/", self.payload(date=today.isoformat()))
        tx = Transaction.objects.get(pk=self.json(r)["transaction"]["id"])
        self.assertEqual((tx.date, tx.purchase_date), (today, None))

    def test_quick_entry_on_the_card(self):
        today = services.timezone.localdate()
        p = quick.parse(self.user, "Tênis 199,90 compras nubank", today=today)
        self.assertEqual((p["date"], p["purchase_date"]), (planning.invoice_due_for(self.card, today), today))
        tx = quick.save(self.user, p)
        self.assertEqual(tx.purchase_date, today)
        self.assertIn("Data da compra", tx.note)

    def test_recurring_item_on_the_card_is_booked_in_the_invoice_month(self):
        item = RecurringTransaction.objects.create(
            user=self.user, name="Streaming", amount=Decimal("39.90"), kind="expense", category=self.cat(),
            account=self.card, frequency="monthly", start_date=D(2026, 11, 5), next_due=D(2026, 11, 5))
        services.apply_recurring_item(item, D(2026, 11, 5))
        tx = Transaction.objects.get(recurring=item)
        self.assertEqual((tx.date, tx.purchase_date, tx.recurring_date), (D(2026, 12, 10), D(2026, 11, 5), D(2026, 11, 5)))
        # and the plan shows the next one in the month of its invoice (5/12 -> invoice of 10/01)
        jan = planning.month_plan(self.user, D(2027, 1, 1), today=D(2026, 11, 6))
        self.assertEqual([r["date"] for r in jan["rows"] if r["name"] == "Streaming"], [D(2027, 1, 10)])
        dec = planning.month_plan(self.user, D(2026, 12, 1), today=D(2026, 11, 6))
        self.assertEqual([r["source"] for r in dec["rows"] if r["name"] == "Streaming"], ["transaction"])


class BudgetSpentAndPlannedTests(AppTestCase):
    def test_spent_is_what_happened_and_planned_what_is_coming(self):
        from app import budgeting
        from app.models import Budget
        groceries = self.cat()
        Budget.objects.create(user=self.user, category=groceries, month=D(2026, 3, 1), amount=Decimal("500"))
        self.tx(amount="100", category=groceries, date=D(2026, 3, 5))
        self.tx(amount="50", category=groceries, date=D(2026, 3, 20))          # typed in advance
        RecurringTransaction.objects.create(
            user=self.user, name="Feira", amount=Decimal("400"), kind="expense", category=groceries,
            account=self.account(), frequency="monthly", start_date=D(2026, 3, 25), next_due=D(2026, 3, 25))
        item = next(i for i in budgeting.status(self.user, D(2026, 3, 1), D(2026, 3, 10))["items"] if i["category"])
        self.assertEqual((item["spent"], item["planned"], item["with_planned"]), (Decimal("100.00"), Decimal("450.00"), Decimal("550.00")))
        self.assertTrue(item["planned_over"])
        self.assertEqual(item["level"], "ok")                                   # 20% really used so far
        past = next(i for i in budgeting.status(self.user, D(2026, 3, 1), D(2026, 4, 2))["items"] if i["category"])
        self.assertEqual(past["planned"], Decimal("0.00"))

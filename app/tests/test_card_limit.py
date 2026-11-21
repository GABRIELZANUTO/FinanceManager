"""Card limit: installments still to come hold the limit, like the bank does."""
import datetime
from decimal import Decimal

from app import planning, services
from app.models import Account, RecurringTransaction

from .base import AppTestCase, today


class CardLimitTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit", credit_limit=Decimal("3000.00"),
                                           due_day=10, closing_day=3)

    def plan(self, amount="300.00", first=2, total=5, start=None, **extra):
        start = start or services.add_months(today(), 1)
        end = services.add_months(start, total - first)
        return RecurringTransaction.objects.create(
            user=self.user, name=f"Geladeira (parcelado em {total}x)", amount=Decimal(amount), category=self.cat(),
            account=self.card, start_date=start, next_due=start, end_date=end, installment_first=first,
            installment_total=total, **extra)

    def account_json(self):
        accounts = self.json(self.client.get("/api/accounts/"))["accounts"]
        return next(a for a in accounts if a["id"] == self.card.pk)

    def test_remaining_installments_hold_the_limit(self):
        self.tx(amount="400.00", account=self.card)          # a purchase already made
        self.plan()                                         # installments 2 to 5 of R$ 300 still to come
        data = self.account_json()
        self.assertEqual((data["used_purchases"], data["installments_left"], data["installments_count"]), ("400.00", "1200.00", 4))
        self.assertEqual((data["used"], data["available"]), ("1600.00", "1400.00"))
        self.assertEqual(data["used_percent"], 53.3)
        [plan] = data["installment_plans"]
        self.assertEqual((plan["name"], plan["left"], plan["each"], plan["amount"]), ("Geladeira", 4, "300.00", "1200.00"))

    def test_a_posted_installment_moves_from_held_to_purchases(self):
        item = self.plan(start=today())
        before = self.account_json()
        services.apply_recurring_item(item, today())         # installment 2 posted today
        after = self.account_json()
        self.assertEqual(before["installments_count"] - 1, after["installments_count"])
        self.assertEqual(Decimal(after["installments_left"]), Decimal(before["installments_left"]) - Decimal("300.00"))
        self.assertEqual(after["used"], before["used"])     # the total held does not change

    def test_only_installment_plans_count(self):
        start = services.add_months(today(), 1)
        RecurringTransaction.objects.create(user=self.user, name="Streaming", amount=Decimal("40.00"), category=self.cat(),
                                            account=self.card, start_date=start, next_due=start)
        self.plan(active=False)
        self.assertEqual(self.account_json()["installments_left"], "0.00")

    def test_plan_without_end_date_ends_at_its_last_installment(self):
        start = services.add_months(today(), 1)
        RecurringTransaction.objects.create(user=self.user, name="TV", amount=Decimal("100.00"), category=self.cat(),
                                            account=self.card, start_date=start, next_due=start,
                                            installment_first=8, installment_total=10)
        held = planning.installments_left(self.card)
        self.assertEqual((held["count"], held["amount"]), (3, Decimal("300.00")))

    def test_other_accounts_are_not_affected(self):
        self.plan()
        bank = self.json(self.client.get("/api/accounts/"))["accounts"]
        self.assertTrue(all("installments_left" not in a for a in bank if a["type"] != "credit"))
        self.assertEqual(planning.installments_left(self.account())["amount"], Decimal("0"))

    def test_late_installment_not_posted_still_counts(self):
        start = today() - datetime.timedelta(days=40)
        self.plan(start=start, first=1, total=3)
        held = planning.installments_left(self.card)
        self.assertEqual(held["count"], 3)

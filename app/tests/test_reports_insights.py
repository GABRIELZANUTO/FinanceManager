import datetime
from decimal import Decimal

from django.urls import reverse

from app import insights, nlq, reports, services
from app.models import Account, Category, Goal, LedgerEntry, Person, RecurringTransaction, Transaction, Transfer

from .base import AppTestCase, today


class ReportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.month = today().replace(day=1)
        self.prev = services.add_months(self.month, -1)

    def build(self, start=None, end=None):
        return reports.build(self.user, start or self.month, end or services.month_end(self.month))

    def test_cash_flow_closing_matches_account_balances(self):
        self.tx(amount="100", kind="income", date=self.prev)
        self.tx(amount="30", date=self.month)
        data = self.build()
        flow = data["cash_flow"][0]
        self.assertEqual((flow["opening"], flow["inflow"], flow["outflow"]), (Decimal("100.00"), Decimal("0.00"), Decimal("30.00")))
        self.assertEqual(flow["closing"], services.account_balances(self.user)[self.account().pk])

    def test_net_worth_series_ends_at_balance(self):
        self.tx(amount="100", kind="income", date=self.prev)
        self.tx(amount="30", date=self.month)
        data = self.build(self.prev, services.month_end(self.month))
        self.assertEqual([r["balance"] for r in data["net_worth"]], [Decimal("100.00"), Decimal("70.00")])

    def test_payees_and_tags(self):
        t = self.tx(amount="9", payee="Shop", date=self.month)
        services.save_transaction(t, tags=["x"])
        self.tx(amount="1", payee="Shop", date=self.month)
        data = self.build()
        self.assertEqual(data["payees"][0], {"name": "Shop", "amount": Decimal("10.00"), "count": 2})
        self.assertEqual(data["tags"][0]["name"], "x")
        self.assertEqual(data["tags"][0]["expense"], Decimal("9.00"))

    def test_other_users_excluded(self):
        self.tx(user=self.other, amount="999", payee="Secret", date=self.month)
        data = self.build()
        self.assertEqual(data["statement"]["expense_total"], 0)
        self.assertEqual(data["payees"], [])

    def test_yearly_granularity_for_long_ranges(self):
        data = reports.build(self.user, datetime.date(2020, 1, 1), datetime.date(2025, 12, 31))
        self.assertTrue(data["yearly"])

    def test_resolve_period_presets(self):
        t = datetime.date(2025, 5, 15)
        self.assertEqual(reports.resolve_period("last_month", None, None, t)[1:], (datetime.date(2025, 4, 1), datetime.date(2025, 4, 30)))
        self.assertEqual(reports.resolve_period("quarter", None, None, t)[1:], (datetime.date(2025, 4, 1), datetime.date(2025, 6, 30)))
        self.assertEqual(reports.resolve_period("year", None, None, t)[1:], (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31)))
        self.assertEqual(reports.resolve_period("last_year", None, None, t)[1:], (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31)))
        self.assertEqual(reports.resolve_period("custom", datetime.date(2025, 3, 5), datetime.date(2025, 3, 1), t)[1:],
                         (datetime.date(2025, 3, 1), datetime.date(2025, 3, 5)))
        self.assertEqual(reports.resolve_period("junk", None, None, t)[0], "this_month")
        self.assertEqual(reports.previous_range(datetime.date(2025, 3, 1), datetime.date(2025, 3, 31)),
                         (datetime.date(2025, 1, 29), datetime.date(2025, 2, 28)))

    def test_print_stylesheet_and_markup(self):
        html = self.client.get(reverse("reports")).content.decode()
        self.assertIn("data-print", html)
        self.assertIn("no-print", html)
        from django.contrib.staticfiles import finders
        self.assertIn("@media print", open(finders.find("app/css/app.css"), encoding="utf-8").read())

    def test_change_helper(self):
        self.assertIsNone(reports.change(Decimal("5"), Decimal("0")))
        self.assertEqual(reports.change(Decimal("150"), Decimal("100")), 50.0)


class InsightTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.month = today().replace(day=1)

    def test_unusual_not_flagged_when_normal(self):
        for i in (1, 2, 3):
            self.tx(amount="100", date=services.add_months(self.month, -i))
        self.tx(amount="120", date=self.month)
        self.assertEqual(insights.unusual(self.user, today()), [])

    def test_subscription_detector(self):
        for i in range(4):
            self.tx(amount="9.99", name="Streaming", payee="NetStream", date=services.add_months(self.month, -i).replace(day=3))
        self.tx(amount="50", name="One off", date=self.month)
        self.tx(amount="10", name="Wobbly", date=services.add_months(self.month, -1))
        self.tx(amount="40", name="Wobbly", date=services.add_months(self.month, -2))
        self.tx(amount="90", name="Wobbly", date=services.add_months(self.month, -3))
        subs = insights.subscriptions(self.user, today())
        self.assertEqual([s["name"] for s in subs], ["NetStream"])
        self.assertFalse(subs[0]["tracked"])
        RecurringTransaction.objects.create(user=self.user, name="Streaming", amount=Decimal("9.99"), kind="expense",
                                            category=self.cat(), start_date=today(), next_due=today(), account=self.account())
        self.assertTrue(insights.subscriptions(self.user, today())[0]["tracked"])

    def test_two_a_month_is_not_a_subscription_but_three_per_month_neither(self):
        for i in range(4):
            for d in (2, 3, 4):
                self.tx(amount="5", name="Coffee", date=services.add_months(self.month, -i).replace(day=d))
        self.assertEqual(insights.subscriptions(self.user, today()), [])

    def test_goal_progress_and_endpoint(self):
        g = Goal.objects.create(user=self.user, name="Trip", target_amount=Decimal("100"))
        g.contributions.create(amount=Decimal("25"))
        self.assertEqual(insights.goal_progress(self.user)[0]["percent"], 25.0)
        data = self.json(self.client.get(reverse("api_insights")))
        for key in ("sentences", "movers", "subscriptions", "bills", "goals", "secondary"):
            self.assertIn(key, data)

    def test_weekly_digest(self):
        self.tx(amount="40", date=today())
        self.tx(amount="10", date=today() - datetime.timedelta(days=8))
        d = insights.weekly_digest(self.user, today())
        self.assertEqual((d["expense"], d["prev_expense"], d["expense_change"]), (Decimal("40.00"), Decimal("10.00"), 300))
        self.assertEqual(d["biggest"].amount, Decimal("40.00"))
        self.assertEqual(d["categories"][0]["total"], Decimal("40.00"))


class NaturalLanguageTests(AppTestCase):
    def parse(self, q, today_=datetime.date(2025, 6, 15)):
        cats = [{"id": 1, "name": "Groceries"}, {"id": 2, "name": "Dining out / Coffee"}]
        return nlq.parse(q, cats, [{"id": 7, "name": "Cash"}], [{"id": 9, "name": "work"}], today_)

    def test_ranges_and_types(self):
        self.assertEqual(self.parse("spent this year")[0]["type"], "expense")
        self.assertEqual(self.parse("income in march")[0]["date_from"], "2025-03-01")
        self.assertEqual(self.parse("coffee in december")[0]["date_from"], "2024-12-01")
        self.assertEqual(self.parse("anything in july 2023")[0]["date_to"], "2023-07-31")
        self.assertEqual(self.parse("yesterday")[0]["date_from"], "2025-06-14")
        self.assertEqual(self.parse("last week")[0]["date_from"], "2025-06-02")
        self.assertEqual(self.parse("this week")[0]["date_from"], "2025-06-09")
        self.assertEqual(self.parse("today")[0]["date_to"], "2025-06-15")

    def test_free_text_tags_accounts_notes(self):
        p, _ = self.parse("uber #work cash with notes")
        self.assertEqual((p["q"], p["tag"], p["account"], p["has_notes"]), ("uber", "9", "7", "1"))

    def test_subcategory_and_prefix_match(self):
        self.assertEqual(self.parse("coffee")[0]["category"], "2")
        self.assertEqual(self.parse("groc")[0]["category"], "1")

    def test_plain_text_is_not_structured(self):
        p, parts = self.parse("hello world")
        self.assertEqual(p, {"q": "hello world"})


import datetime
from decimal import Decimal

from app import planning, services
from app.models import Account, Budget, RecurringTransaction, Transaction, Transfer

from .base import AppTestCase

D = datetime.date
TODAY = D(2026, 3, 10)


class MonthPlanTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.account()
        self.bank.opening_balance = Decimal("1000.00")
        self.bank.save()
        self.card = Account.objects.create(user=self.user, name="Aaa Cartão", type="credit",
                                           credit_limit=Decimal("2000.00"), due_day=20, closing_day=19)

    def recurring(self, name, amount, kind, start, frequency="monthly", account=None, end=None, category=None):
        category = category or self.cat(name="Salary" if kind == "income" else "Groceries")
        return RecurringTransaction.objects.create(
            user=self.user, name=name, amount=Decimal(amount), kind=kind, category=category,
            account=account, frequency=frequency, start_date=start, next_due=start, end_date=end)

    def plan(self, month=TODAY):
        return planning.month_plan(self.user, month, today=TODAY)

    def test_recurring_income_is_planned_until_it_happens(self):
        self.recurring("Salário", "5000", "income", D(2026, 1, 15))
        self.tx(amount="5000", kind="income", name="Salário", date=D(2026, 2, 15))
        self.tx(amount="100", name="Mercado", date=D(2026, 3, 2))
        RecurringTransaction.objects.update(next_due=D(2026, 3, 15))
        plan = self.plan()
        salary = [r for r in plan["rows"] if r["name"] == "Salário"]
        self.assertEqual(len(salary), 1)
        self.assertEqual((salary[0]["date"], salary[0]["status"]), (D(2026, 3, 15), "planned"))
        self.assertEqual(plan["totals"]["income_planned"], Decimal("5000.00"))
        self.assertEqual(plan["totals"]["expense_done"], Decimal("100.00"))
        self.assertEqual(plan["kind"], "current")

    def test_future_months_show_every_occurrence_and_installments_end(self):
        self.recurring("Feira", "50", "expense", D(2026, 3, 3), frequency="weekly")
        self.recurring("Parcela TV (2/3)", "300", "expense", D(2026, 3, 10), account=self.card, end=D(2026, 4, 10))
        april = self.plan(D(2026, 4, 1))
        self.assertEqual(len([r for r in april["rows"] if r["name"] == "Feira"]), 4)
        self.assertEqual(len([r for r in april["rows"] if r["name"].startswith("Parcela")]), 1)
        may = self.plan(D(2026, 5, 1))
        self.assertFalse([r for r in may["rows"] if r["name"].startswith("Parcela")])
        self.assertEqual(may["kind"], "future")

    def test_posted_slots_are_not_counted_twice(self):
        item = self.recurring("Aluguel", "1500", "expense", D(2026, 3, 5))
        services.apply_recurring_item(item, TODAY)
        rows = [r for r in self.plan()["rows"] if r["name"] == "Aluguel"]
        self.assertEqual([(r["status"], r["source"]) for r in rows], [("done", "transaction")])

    def test_one_off_forecast_posts_once(self):
        data = {"name": "Freela", "amount": "500", "kind": "income", "category": self.cat(name="Salary").pk,
                "frequency": "once", "start_date": (services.timezone.localdate() + datetime.timedelta(days=10)).isoformat(),
                "end_date": ""}
        response = self.post("/api/recurring/", data)
        self.assertEqual(response.status_code, 201, response.content)
        item = RecurringTransaction.objects.get(name="Freela")
        self.assertEqual(item.end_date, item.start_date)
        self.assertEqual(services.apply_recurring_item(item, item.start_date + datetime.timedelta(days=40)), 1)
        item.refresh_from_db()
        self.assertFalse(item.active)
        self.assertEqual(Transaction.objects.filter(name="Freela").count(), 1)

    def test_card_invoice_and_bank_projection(self):
        self.recurring("Salário", "3000", "income", D(2026, 3, 15))
        self.tx(amount="400", name="Loja", date=D(2026, 3, 1), account=self.card)
        self.recurring("Parcela (1/2)", "100", "expense", D(2026, 3, 18), account=self.card, end=D(2026, 4, 18))
        Transfer.objects.create(user=self.user, from_account=self.bank, to_account=self.card, amount=Decimal("150"),
                                date=D(2026, 3, 5))
        plan = self.plan()
        invoice = plan["invoices"][0]
        self.assertEqual((invoice["due"], invoice["amount"], invoice["paid"], invoice["open"], invoice["status"]),
                         (D(2026, 3, 20), Decimal("500.00"), Decimal("150.00"), Decimal("350.00"), "open"))
        bank = plan["bank"]
        self.assertEqual(bank["now"], Decimal("850.00"))
        self.assertEqual(bank["end"], Decimal("850.00") + 3000 - 350)
        # card purchases are expenses once, never again as the invoice
        self.assertEqual(plan["totals"]["expense"], Decimal("500.00"))

    def test_bank_projection_warns_before_payday(self):
        self.recurring("Salário", "3000", "income", D(2026, 3, 25))
        self.tx(amount="1200", name="Loja", date=D(2026, 3, 1), account=self.card)
        bank = self.plan()["bank"]
        self.assertEqual(bank["first_negative"], D(2026, 3, 20))
        self.assertEqual(bank["lowest"]["balance"], Decimal("-200.00"))

    def test_estimates_use_budget_left_or_recent_average(self):
        groceries, dining = self.cat(name="Groceries"), self.cat(name="Dining out")
        Budget.objects.create(user=self.user, category=groceries, month=D(2026, 3, 1), amount=Decimal("500"))
        self.tx(amount="200", date=D(2026, 3, 3), category=groceries)
        for month in (12, 1, 2):
            self.tx(amount="90", name="Jantar", category=dining, date=D(2025 if month == 12 else 2026, month, 8))
        estimates = {e["category"]["name"]: (e["amount"], e["basis"]) for e in self.plan()["estimates"]}
        self.assertEqual(estimates[groceries.name], (Decimal("300.00"), "budget"))
        self.assertEqual(estimates[dining.name], (Decimal("90.00"), "average"))
        self.assertEqual(self.plan(D(2026, 2, 1))["estimates"], [])

    def test_next_due_date(self):
        self.assertEqual(planning.next_due_date(self.card, D(2026, 3, 20)), D(2026, 3, 20))
        self.assertEqual(planning.next_due_date(self.card, D(2026, 3, 21)), D(2026, 4, 20))
        self.card.due_day = 31
        self.assertEqual(planning.next_due_date(self.card, D(2026, 4, 1)), D(2026, 4, 30))

    def test_plan_api(self):
        response = self.client.get("/api/plan/?month=2026-04")
        self.assertEqual(response.status_code, 200)
        data = self.json(response)
        self.assertEqual(data["month"], "2026-04-01")
        self.assertIn("totals", data)
        self.assertEqual(self.client.get("/api/plan/?month=lixo").status_code, 200)


class AccountCardTests(AppTestCase):
    def test_banks_come_before_cards(self):
        Account.objects.create(user=self.user, name="Aaa cartão", type="credit")
        Account.objects.create(user=self.user, name="Zzz banco", type="bank")
        types = [a["type"] for a in self.json(self.client.get("/api/accounts/"))["accounts"]]
        self.assertEqual(types, sorted(types, key=lambda t: t == "credit"))

    def test_limit_and_due_day_only_on_cards(self):
        response = self.post("/api/accounts/", {"name": "Nubank", "type": "credit", "opening_balance": "0",
                                                "credit_limit": "3000", "due_day": 10})
        self.assertEqual(response.status_code, 201, response.content)
        card = Account.objects.get(name="Nubank")
        self.tx(amount="1200", account=card)
        data = self.json(self.client.get(f"/api/accounts/{card.pk}/"))["account"]
        self.assertEqual((data["used"], data["available"], data["used_percent"], data["due_day"]), ("1200.00", "1800.00", 40.0, 10))
        response = self.post("/api/accounts/", {"name": "Caixa", "type": "bank", "opening_balance": "0",
                                                "credit_limit": "3000", "due_day": 10})
        bank = Account.objects.get(name="Caixa")
        self.assertEqual((bank.credit_limit, bank.due_day), (None, None))
        response = self.post("/api/accounts/", {"name": "Outro", "type": "credit", "opening_balance": "0", "due_day": 40})
        self.assertEqual(response.status_code, 422)


class PlanPagesTests(AppTestCase):
    def test_pages_render_with_plan_pieces(self):
        page = self.client.get("/analytics/")
        self.assertContains(page, 'id="plano"')
        self.assertContains(page, 'id="pay-dialog"')
        self.assertNotContains(page, "Adicionar previsão")
        self.assertContains(page, "app/js/plan.js")
        self.assertContains(self.client.get("/accounts/"), 'name="due_day"')
        self.assertNotContains(self.client.get("/recurring/"), 'value="once"')
        self.assertContains(self.client.get("/"), 'id="plan-line"')

    def test_invoice_due_soon_shows_in_bills(self):
        from app import insights
        today = services.timezone.localdate()
        card = Account.objects.create(user=self.user, name="Nubank", type="credit",
                                      due_day=(today + datetime.timedelta(days=3)).day,
                                      closing_day=(today + datetime.timedelta(days=1)).day)
        self.tx(amount="250", account=card, date=services.timezone.localdate())
        bills = [b for b in insights.bills(self.user, services.timezone.localdate()) if b["type"] == "invoice"]
        self.assertEqual([(b["name"], b["amount"]) for b in bills], [("Fatura Nubank", Decimal("250.00"))])


class PlanBackupTests(AppTestCase):
    def test_card_fields_and_forecasts_survive_a_backup(self):
        from app import backup
        Account.objects.create(user=self.user, name="Nubank", type="credit", credit_limit=Decimal("3000"), due_day=10)
        RecurringTransaction.objects.create(user=self.user, name="Freela", amount=Decimal("500"), kind="income",
                                            category=self.cat(name="Salary"), frequency="once", start_date=D(2026, 12, 1),
                                            end_date=D(2026, 12, 1), next_due=D(2026, 12, 1))
        parsed = backup.parse(backup.dumps(self.user).encode("utf-8"))
        fresh = self.make_user("bob")
        backup.restore(fresh, parsed)
        card = Account.objects.get(user=fresh, name="Nubank")
        self.assertEqual((card.credit_limit, card.due_day), (Decimal("3000.00"), 10))
        self.assertEqual(RecurringTransaction.objects.get(user=fresh, name="Freela").frequency, "once")


class DashboardCurrentMonthTests(AppTestCase):
    def test_overview_counts_the_current_month_but_not_later_months(self):
        from app import stats
        card = Account.objects.create(user=self.user, name="Nubank", type="credit")
        today = D(2026, 3, 10)
        self.tx(amount="100", date=D(2026, 3, 2))
        self.tx(amount="300", date=D(2026, 3, 25), account=card, name="Ainda este mês")
        self.tx(amount="2000", kind="income", date=D(2026, 3, 28), name="Salário do dia 28")
        self.tx(amount="999", date=D(2026, 4, 5), account=card, name="Mês que vem")
        data = stats.summary(self.user, D(2026, 3, 1), today=today)
        self.assertEqual((data["income"], data["expense"]), (Decimal("2000.00"), Decimal("400.00")))
        self.assertEqual(data["balance"], services.balance_for(self.user) + 999)
        nubank = next(a for a in self.json(self.client.get("/api/accounts/?ate=2026-03-31"))["accounts"] if a["name"] == "Nubank")
        self.assertEqual(nubank["balance"], "-300.00")
        names = [t["name"] for t in self.json(self.client.get("/api/transactions/?date_to=2026-03-31"))["transactions"]]
        self.assertIn("Ainda este mês", names)
        self.assertNotIn("Mês que vem", names)


class BulkDateTests(AppTestCase):
    def bulk(self, **body):
        return self.post("/api/transactions/bulk/", {"action": "date", **body})

    def test_same_date_or_shift_keeping_the_day(self):
        a = self.tx(name="A", date=D(2026, 10, 3))
        b = self.tx(name="B", date=D(2026, 10, 31))
        response = self.bulk(ids=[a.pk, b.pk], shift_months=1)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.json(response)["moved"], 2)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.date, b.date), (D(2026, 11, 3), D(2026, 11, 30)))
        self.bulk(ids=[a.pk, b.pk], date="2026-11-19")
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.date, b.date), (D(2026, 11, 19), D(2026, 11, 19)))
        from app.models import AuditEntry
        self.assertTrue(AuditEntry.objects.filter(transaction_id=a.pk, summary="Alterado: data").exists())

    def test_rejects_bad_input_closed_months_and_other_users(self):
        from app.models import MonthLock
        a = self.tx(name="A", date=D(2026, 10, 3))
        self.assertEqual(self.bulk(ids=[a.pk]).status_code, 422)
        self.assertEqual(self.bulk(ids=[a.pk], date="1990-01-01").status_code, 422)
        MonthLock.objects.create(user=self.user, month=D(2026, 10, 1))
        self.assertEqual(self.bulk(ids=[a.pk], date="2026-11-01").status_code, 422)
        a.refresh_from_db()
        self.assertEqual(a.date, D(2026, 10, 3))
        foreign = self.tx(user=self.other, name="X", date=D(2026, 10, 3))
        self.assertEqual(self.bulk(ids=[foreign.pk], date="2026-11-01").status_code, 404)

    def test_transactions_page_has_the_button_and_dialog(self):
        self.tx(name="A")
        page = self.client.get("/transactions/")
        self.assertContains(page, "data-bulk-date")
        self.assertContains(page, 'id="date-dialog"')


class FutureTransactionTests(AppTestCase):
    def test_income_can_be_scheduled_on_a_bank_account_and_enters_the_plan(self):
        future = services.timezone.localdate() + datetime.timedelta(days=33)
        response = self.post("/api/transactions/", self.payload(name="Salário depois das férias", amount="2000", kind="income",
                                                                category=self.cat(name="Salary").pk, date=future.isoformat()))
        self.assertEqual(response.status_code, 201, response.content)
        bank = next(a for a in self.json(self.client.get("/api/accounts/"))["accounts"] if a["type"] == "bank")
        self.assertEqual(bank["scheduled"], "2000.00")
        plan = planning.month_plan(self.user, future)
        row = next(r for r in plan["rows"] if r["name"] == "Salário depois das férias")
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(plan["bank"]["end"] - plan["bank"]["now"], Decimal("2000.00"))
        too_far = services.timezone.localdate() + datetime.timedelta(days=400)
        response = self.post("/api/transactions/", self.payload(date=too_far.isoformat()))
        self.assertEqual(response.status_code, 422)


class PayInvoiceAndMoneyTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.account()
        self.bank.opening_balance = Decimal("1000.00")
        self.bank.save()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit", due_day=20, closing_day=19)

    def test_scheduled_payment_counts_once_in_the_bank_projection(self):
        now = services.timezone.localdate()
        due = planning.next_due_date(self.card, now)
        self.tx(amount="400", account=self.card, date=now)
        response = self.post("/api/transfers/", {"from_account": self.bank.pk, "to_account": self.card.pk,
                                                 "amount": "400", "date": due.isoformat(), "note": "Pagamento"})
        self.assertEqual(response.status_code, 201, response.content)
        plan = planning.month_plan(self.user, due, today=now)
        invoice = next(i for i in plan["invoices"] if i["due"] == due)
        self.assertEqual((invoice["status"], invoice["open"]), ("paid", Decimal("0.00")))
        if due != now:
            self.assertEqual(plan["bank"]["end"], plan["bank"]["now"] - 400)

    def test_dashboard_separates_money_and_card_debt(self):
        from app import stats
        self.tx(amount="300", account=self.card)
        now = services.timezone.localdate()
        data = stats.summary(self.user, services.month_start(now))
        plan = planning.month_plan(self.user, now)
        self.assertEqual((data["money_today"], data["card_debt"]), (Decimal("1000.00"), Decimal("300.00")))
        # "Nos bancos no fim do mês" and "Livre no fim do mês": the same numbers as the plan
        self.assertEqual((data["money"], data["balance"]), (plan["bank"]["end"], plan["balance"]["end"]))
        self.assertEqual(data["balance"], Decimal("700.00"))

    def test_future_income_does_not_pay_for_spending_today(self):
        profile = services.get_profile(self.user)
        profile.allow_negative_balance = False
        profile.save()
        services.forget_profile(self.user)
        self.tx(amount="5000", kind="income", date=services.timezone.localdate() + datetime.timedelta(days=20))
        response = self.post("/api/transactions/", self.payload(amount="1500"))
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(self.post("/api/transactions/", self.payload(amount="900")).status_code, 201)


class CountedOnceTests(AppTestCase):
    def test_recurring_already_typed_by_hand_counts_once_and_is_adopted(self):
        bank = self.account()
        item = RecurringTransaction.objects.create(
            user=self.user, name="Balão", amount=Decimal("4875"), kind="expense", category=self.cat(), account=bank,
            frequency="monthly", start_date=D(2026, 12, 10), end_date=D(2026, 12, 10), next_due=D(2026, 12, 10))
        typed = self.tx(amount="4870", name="Balão", date=D(2026, 12, 10), account=bank)
        plan = planning.month_plan(self.user, D(2026, 12, 1), today=TODAY)
        self.assertEqual(plan["totals"]["expense"], Decimal("4870.00"))
        self.assertEqual([m["transaction"] for m in plan["counted_once"]], ["Balão"])
        self.assertEqual(plan["bank"]["end"], plan["bank"]["now"] - Decimal("4870"))
        # on its date nothing is linked or posted on its own: it waits for the answer
        self.assertEqual(services.apply_recurring_item(item, D(2026, 12, 10)), 0)
        typed.refresh_from_db()
        self.assertIsNone(typed.recurring_id)
        self.assertEqual(Transaction.objects.filter(name="Balão").count(), 1)
        from app.models import RecurringMatch
        self.assertEqual(RecurringMatch.objects.get().decision, "pending")
        still_once = planning.month_plan(self.user, D(2026, 12, 1), today=D(2026, 12, 11))
        self.assertEqual(still_once["totals"]["expense"], Decimal("4870.00"))
        r = self.post(f"/api/recurring/{item.pk}/match/", {"slot": "2026-12-10", "transaction": typed.pk, "decision": "same"})
        self.assertEqual(r.status_code, 200, r.content)
        typed.refresh_from_db()
        self.assertEqual((typed.recurring_id, typed.recurring_date), (item.pk, D(2026, 12, 10)))

    def test_different_things_are_not_merged(self):
        bank = self.account()
        RecurringTransaction.objects.create(
            user=self.user, name="Aluguel", amount=Decimal("1500"), kind="expense", category=self.cat(), account=bank,
            frequency="monthly", start_date=D(2026, 12, 5), next_due=D(2026, 12, 5))
        self.tx(amount="1500", name="Notebook", date=D(2026, 12, 5), account=bank)
        self.tx(amount="900", name="Aluguel extra", date=D(2026, 12, 6), account=bank)
        plan = planning.month_plan(self.user, D(2026, 12, 1), today=TODAY)
        self.assertEqual(plan["counted_once"], [])


class MonthBalanceTests(AppTestCase):
    def test_each_month_starts_with_what_the_previous_one_left(self):
        bank = self.account()
        bank.opening_balance = Decimal("1000.00")
        bank.save()
        card = Account.objects.create(user=self.user, name="Cartão", type="credit")
        self.tx(amount="200", date=D(2026, 2, 10), account=card)          # last month, on the card
        RecurringTransaction.objects.create(
            user=self.user, name="Salário", amount=Decimal("3000"), kind="income", category=self.cat(name="Salary"),
            account=bank, frequency="monthly", start_date=D(2026, 3, 15), next_due=D(2026, 3, 15))
        RecurringTransaction.objects.create(
            user=self.user, name="Aluguel", amount=Decimal("2500"), kind="expense", category=self.cat(),
            account=bank, frequency="monthly", start_date=D(2026, 3, 5), next_due=D(2026, 3, 5))
        march = planning.month_plan(self.user, D(2026, 3, 1), today=TODAY)["balance"]
        # 800 at the start (1000 minus 200 owed on the card) + 3000 - 2500 - 66.67 estimated (average of 200 / 3)
        self.assertEqual((march["start"], march["end"]), (Decimal("800.00"), Decimal("1233.33")))
        april = planning.month_plan(self.user, D(2026, 4, 1), today=TODAY)["balance"]
        self.assertEqual((april["start"], april["end"]), (Decimal("1233.33"), Decimal("1666.66")))


class MoneyTodayTests(AppTestCase):
    def test_money_is_today_and_the_rest_of_the_month_comes_apart(self):
        from app import stats
        bank = self.account()
        bank.opening_balance = Decimal("2836.87")
        bank.save()
        self.tx(amount="77.80", date=D(2026, 11, 11), account=bank)
        self.tx(amount="2121", kind="income", date=D(2026, 11, 30), account=bank, name="13º")
        data = stats.summary(self.user, D(2026, 11, 1), today=D(2026, 11, 11))
        self.assertEqual((data["money_today"], data["money"]), (Decimal("2759.07"), Decimal("4880.07")))
        self.assertEqual(data["until"], D(2026, 11, 30))

    def test_dashboard_matches_the_plan_with_recurring_items_not_posted_yet(self):
        from app import stats
        bank = self.account()
        bank.opening_balance = Decimal("1000.00")
        bank.save()
        RecurringTransaction.objects.create(
            user=self.user, name="Mae", amount=Decimal("268.65"), kind="income", category=self.cat(name="Salary"),
            account=bank, frequency="monthly", start_date=D(2026, 11, 20), next_due=D(2026, 11, 20))
        data = stats.summary(self.user, D(2026, 11, 1), today=D(2026, 11, 11))
        plan = planning.month_plan(self.user, D(2026, 11, 1), today=D(2026, 11, 11))
        self.assertEqual(data["income"], plan["totals"]["income"])
        self.assertEqual(data["balance"], plan["balance"]["end"])
        self.assertEqual((data["money_today"], data["money"]), (Decimal("1000.00"), Decimal("1268.65")))



class MatchDecisionTests(AppTestCase):
    def test_different_posts_the_occurrence_and_is_not_suggested_again(self):
        bank = self.account()
        item = RecurringTransaction.objects.create(
            user=self.user, name="Uber", amount=Decimal("25"), kind="expense", category=self.cat(), account=bank,
            frequency="monthly", start_date=D(2026, 3, 5), next_due=D(2026, 3, 5))
        ride = self.tx(amount="25.50", name="Uber centro", date=D(2026, 3, 6), account=bank)
        services.apply_recurring_item(item, D(2026, 3, 7))
        r = self.post(f"/api/recurring/{item.pk}/match/", {"slot": "2026-03-05", "transaction": ride.pk, "decision": "different"})
        self.assertEqual(r.status_code, 200, r.content)
        posted = Transaction.objects.filter(recurring=item, recurring_date=D(2026, 3, 5))
        self.assertEqual(posted.count(), 1)                       # the recurring one entered too
        ride.refresh_from_db()
        self.assertIsNone(ride.recurring_id)
        plan = planning.month_plan(self.user, D(2026, 3, 1), today=D(2026, 3, 7))
        self.assertEqual(plan["counted_once"], [])
        self.assertEqual(self.post(f"/api/recurring/{item.pk}/match/", {"slot": "2026-03-05", "transaction": ride.pk,
                                                                        "decision": "maybe"}).status_code, 422)

    def test_dashboard_tells_there_is_something_to_confirm(self):
        from app import insights
        bank = self.account()
        item = RecurringTransaction.objects.create(
            user=self.user, name="Aluguel", amount=Decimal("1500"), kind="expense", category=self.cat(), account=bank,
            frequency="monthly", start_date=services.timezone.localdate(), next_due=services.timezone.localdate())
        self.tx(amount="1500", name="Aluguel", account=bank)
        services.apply_recurring_item(item)
        texts = [s["text"] for s in insights.sentences(self.user)]
        self.assertTrue(any("confirme no planejamento" in t for t in texts))
        self.assertTrue(any(t.startswith("Pelo planejamento") for t in texts))

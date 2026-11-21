import datetime
from decimal import Decimal

from app import quick
from app.models import Account, Category, Transaction

from .base import AppTestCase

D = datetime.date
TODAY = D(2026, 11, 11)


class QuickParseTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.main = self.account()
        self.main.name = "Conta principal"
        self.main.save()
        self.card = Account.objects.create(user=self.user, name="Cartão Nubank", type="credit", due_day=19)
        self.other_card = Account.objects.create(user=self.user, name="Cartão Itaú", type="credit")

    def parse(self, text):
        return quick.parse(self.user, text, today=TODAY)

    def test_name_amount_category_and_account(self):
        p = self.parse("Bolo 50,00 restaurante conta principal")
        self.assertEqual((p["name"], p["amount"], p["kind"]), ("Bolo", Decimal("50.00"), "expense"))
        self.assertEqual(p["category"].name, "Restaurantes")
        self.assertEqual(p["account"], self.main)
        self.assertEqual(p["date"], TODAY)

    def test_card_goes_to_the_next_invoice_due_date(self):
        p = self.parse("Tênis 299,90 compras nubank")
        self.assertEqual((p["account"], p["date"], p["category"].name), (self.card, D(2026, 11, 19), "Compras"))
        after_due = quick.parse(self.user, "Tênis 299,90 compras nubank", today=D(2026, 11, 20))
        self.assertEqual(after_due["date"], D(2026, 12, 19))

    def test_card_without_due_day_keeps_today_and_says_why(self):
        p = self.parse("Mercado 80 mercado itau")
        self.assertEqual((p["account"], p["date"]), (self.other_card, TODAY))
        self.assertTrue(any("vencimento" in n for n in p["notes"]))

    def test_typos_and_accents_are_forgiven(self):
        p = self.parse("Uber 23,50 trasnporte cartao nubnk")
        self.assertEqual((p["category"].name, p["account"]), ("Transporte", self.card))

    def test_unknown_category_is_created_on_save(self):
        p = self.parse("Ração 120 pet conta principal")
        self.assertIsNone(p["category"])
        self.assertEqual(p["new_category"], "pet")
        tx = quick.save(self.user, p)
        self.assertEqual((tx.category.name, tx.category.kind, tx.account), ("Pet", "expense", self.main))
        again = self.parse("Banho 60 pet")
        self.assertEqual(again["category"].name, "Pet")             # found the next time

    def test_multi_word_category_and_account(self):
        Category.objects.create(user=self.user, name="Comida japonesa", kind="expense")
        p = self.parse("Temaki 45 comida japonesa conta principal")
        self.assertEqual((p["category"].name, p["account"]), ("Comida japonesa", self.main))

    def test_only_category_uses_the_default_account(self):
        p = self.parse("café 15,50 restaurantes")
        self.assertEqual((p["name"], p["category"].name, p["account_typed"]), ("Café", "Restaurantes", False))

    def test_income_and_dates(self):
        p = self.parse("Salário +5.000,00 salario")
        self.assertEqual((p["kind"], p["amount"], p["category"].name), ("income", Decimal("5000.00"), "Salário"))
        self.assertEqual(self.parse("Pão 8 ontem mercado")["date"], TODAY - datetime.timedelta(days=1))

    def test_numbers_inside_the_name(self):
        p = self.parse("Parcela 2 do carro 300 transporte")
        self.assertEqual((p["name"], p["amount"]), ("Parcela 2 do carro", Decimal("300.00")))

    def test_errors(self):
        from django.core.exceptions import ValidationError
        for text in ("", "bolo restaurante", "50"):
            with self.assertRaises(ValidationError, msg=text):
                self.parse(text)

    def test_api_preview_then_save(self):
        r = self.post("/api/quick/", {"text": "Bolo 50,00 restaurante conta principal"})
        self.assertEqual(r.status_code, 200, r.content)
        preview = self.json(r)["preview"]
        self.assertEqual((preview["category"]["name"], preview["account"]["name"]), ("Restaurantes", "Conta principal"))
        self.assertEqual(Transaction.objects.count(), 0)
        r = self.post("/api/quick/", {"text": "Bolo 50,00 restaurante conta principal", "save": True})
        self.assertEqual(r.status_code, 201, r.content)
        tx = Transaction.objects.get()
        self.assertEqual((tx.name, tx.amount, tx.category.name), ("Bolo", Decimal("50.00"), "Restaurantes"))
        self.assertEqual(self.post("/api/quick/", {"text": "bolo"}).status_code, 422)


class QuickSuggestionTests(AppTestCase):
    def test_close_new_category_gets_a_suggestion_and_can_use_it(self):
        p = quick.parse(self.user, "Compra 90 supermercado", today=TODAY)
        self.assertEqual((p["new_category"], p["suggestion"].name), ("supermercado", "Mercado"))
        far = quick.parse(self.user, "Ração 80 bichos", today=TODAY)
        self.assertIsNone(far["suggestion"])                         # nothing really close: just the new one
        groceries = self.cat(name="Groceries")
        r = self.post("/api/quick/", {"text": "Compra 90 supermercado", "save": True, "category": groceries.pk})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Transaction.objects.get().category, groceries)
        self.assertFalse(Category.objects.filter(user=self.user, name__iexact="supermercado").exists())
        foreign = self.cat(self.other)
        self.assertEqual(self.post("/api/quick/", {"text": "Compra 90 supermercado", "category": foreign.pk}).status_code, 422)

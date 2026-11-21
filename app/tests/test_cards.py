import datetime
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from app import cards, services
from app.models import Account, CardInvoice, CategoryRule, MonthLock, RecurringTransaction, Transaction, Transfer

from .base import AppTestCase, today


def d(days_ago):
    return today() - datetime.timedelta(days=days_ago)


def nubank_csv(lines):
    body = "date,title,amount\n" + "".join(f'{day.isoformat()},{title},"{amount}"\n' for day, title, amount in lines)
    return body.encode("utf-8")


def ai_line(day, description, amount, k=0, n=0, kind="purchase", card="0891", category=""):
    return {"date": day.isoformat(), "description": description, "amount": amount, "installment_number": k,
            "installment_total": n, "card_last_digits": card, "merchant_category": "", "suggested_category": category,
            "line_type": kind}


class HelperTests(AppTestCase):
    def test_split_installment(self):
        self.assertEqual(cards.split_installment("Bela Comfort Comercio - Parcela 1/2"), ("Bela Comfort Comercio", 1, 2))
        self.assertEqual(cards.split_installment("CV PHONE CENTER LT05/12"), ("CV PHONE CENTER LT", 5, 12))
        self.assertEqual(cards.split_installment("52.827.349 - NuPay - 2/2"), ("52.827.349 - NuPay", 2, 2))
        self.assertEqual(cards.split_installment("Padaria 12/10"), ("Padaria 12/10", None, None))
        self.assertEqual(cards.split_installment("Uber"), ("Uber", None, None))

    def test_line_types_and_keywords(self):
        self.assertEqual(cards.guess_type("Pagamento recebido", Decimal("-212.88")), "payment")
        self.assertEqual(cards.guess_type("Valor pendente do mês anterior", Decimal("105.74")), "previous_balance")
        self.assertEqual(cards.guess_type("IOF de compra internacional", Decimal("4.00")), "fee")
        self.assertEqual(cards.guess_type("Loja", Decimal("-10")), "refund")
        self.assertEqual(cards.keyword_category("Dl *Uberrides"), "Transporte")
        self.assertEqual(cards.keyword_category("Amazon Prime Canais"), "Assinaturas")
        self.assertEqual(cards.keyword_category("Shopee *Lovare"), "Compras")
        self.assertEqual(cards.keyword_category("PLANOS SUPERMERCADOS"), "Mercado")
        self.assertIsNone(cards.keyword_category("Walcirjose"))

    def test_nubank_detection_and_amounts(self):
        raw = nubank_csv([(d(1), "Pagamento recebido", "- 212,88"), (d(2), "Super Golf", "1.016,77")])
        invoice = cards.read_upload("Nubank.csv", raw, self.user)
        self.assertEqual(invoice["source"], "csv-nubank")
        self.assertEqual([l["amount"] for l in invoice["lines"]], [Decimal("-212.88"), Decimal("1016.77")])
        self.assertEqual(invoice["lines"][0]["type"], "payment")


class StandardCsvTests(AppTestCase):
    def test_read_standard(self):
        text = ("# emissor: Carrefour\n# titular: Gabriel\n# fechamento: 03/10/2026\n# vencimento: 14/10/2026\n"
                "# total: 884,13\ndata;descricao;valor;parcela;total_parcelas;cartao;tipo;categoria\n"
                "07/07/2026;DH E LN OTICA,LONDRINA;100,00;3;7;5926;compra;\n"
                "02/08/2026;Desconto Exclusivo Cartão;-4,02;;;5926;credito;\n"
                "04/09/2026;Pagamento Banco CSF;-1620,40;;;5926;pagamento;\n")
        self.assertTrue(cards.is_standard(text))
        self.assertFalse(cards.is_nubank(text))
        invoice = cards.read_standard(text)
        self.assertEqual((invoice["issuer"], invoice["holder"], invoice["total"]), ("Carrefour", "Gabriel", Decimal("884.13")))
        self.assertEqual(invoice["closing_date"], datetime.date(2026, 10, 3))
        first, discount, payment = invoice["lines"]
        self.assertEqual((first["k"], first["n"], first["card"], first["base"]), (3, 7, "5926", "DH E LN OTICA,LONDRINA"))
        self.assertEqual((discount["type"], discount["amount"]), ("other_credit", Decimal("-4.02")))
        self.assertEqual(payment["type"], "payment")


class NubankPreviewTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit")
        self.invoice = cards.read_upload("fatura.csv", nubank_csv([
            (d(0), "Bela Comfort Comercio - Parcela 1/2", "149,95"),
            (d(3), "IOF de compra internacional", "4,00"),
            (d(3), "Dl *Uberrides", "8,48"),
            (d(5), "Walcirjose", "15,00"),
            (d(8), "Pagamento recebido", "- 212,88"),
            (d(8), "Juros por fatura atrasada", "4,41"),
            (d(16), "Valor pendente do mês anterior", "105,74"),
            (d(18), "Amazon Prime Canais", "19,99"),
            (d(20), "Amazon - Parcela 5/5", "40,50"),
        ]), self.user)

    def preview(self, account=None):
        rows, reference = cards.build_preview(self.user, self.invoice, account or self.card)
        return {r["description"]: r for r in rows}, rows, reference

    def test_statuses_and_categories(self):
        by_name, rows, _ = self.preview()
        self.assertEqual(by_name["Pagamento recebido"]["status"], "payment")
        self.assertEqual(by_name["Valor pendente do mês anterior"]["status"], "ignore")
        self.assertNotEqual(by_name["Bela Comfort Comercio - Parcela 1/2"]["category"], "Parcelas")  # keeps the real category
        self.assertEqual(by_name["IOF de compra internacional"]["category"], "Juros e tarifas")
        self.assertEqual(by_name["Dl *Uberrides"]["category"], "Transporte")
        self.assertEqual(by_name["Amazon Prime Canais"]["category"], "Assinaturas")
        self.assertEqual(by_name["Walcirjose"]["category"], "Outros")

    def test_user_rule_wins_over_keywords(self):
        CategoryRule.objects.create(user=self.user, pattern="uberrides", category=self.cat(name="Compras"))
        by_name, _, _ = self.preview()
        self.assertEqual(by_name["Dl *Uberrides"]["category"], "Compras")
        self.assertEqual(by_name["Dl *Uberrides"]["category_source"], "regra")

    def test_installment_plan_only_when_installments_remain(self):
        by_name, _, _ = self.preview()
        plan = by_name["Bela Comfort Comercio - Parcela 1/2"]["plan"]
        self.assertEqual((plan["first"], plan["total"], plan["remaining"]), (2, 2, 1))
        self.assertEqual(plan["start"], services.add_months(d(0), 1))
        self.assertEqual(plan["end"], services.add_months(d(0), 1))
        self.assertIsNone(by_name["Amazon - Parcela 5/5"]["plan"])

    def test_summary(self):
        _, rows, _ = self.preview()
        summary = cards.summarise(rows, self.invoice)
        self.assertEqual(summary["expense"], Decimal("242.33"))
        self.assertEqual(summary["plans"], 1)
        self.assertEqual(summary["payments"], Decimal("212.88"))


class ItauPreviewTests(AppTestCase):
    """Lines as the AI returns them for a PDF statement."""

    def invoice(self, lines, closing):
        return cards.from_ai({"issuer": "Itaú", "closing_date": closing.isoformat(), "due_date": "",
                              "invoice_total": "", "lines": lines}, "pdf-ia")

    def test_refunds_cancel_purchases_in_the_same_invoice(self):
        closing = d(4)
        invoice = self.invoice([
            ai_line(d(30), "EBN*TIKTOK SH", "686.58"),
            ai_line(d(30), "EBN*TIKTOK SH", "-603.08", kind="refund"),
            ai_line(d(30), "EBN*TIKTOK SH", "-83.50", kind="refund"),
            ai_line(d(32), "CARREFOUR COM", "74.75", 1, 4), ai_line(d(32), "CARREFOUR COM", "74.75", 2, 4),
            ai_line(d(32), "CARREFOUR COM", "74.75", 3, 4), ai_line(d(32), "CARREFOUR COM", "74.75", 4, 4),
            ai_line(d(32), "CARREFOUR COM", "-299.00", kind="refund"),
            ai_line(d(20), "LOJA X", "100.00"), ai_line(d(10), "Estorno de LOJA X", "-30.00", kind="refund"),
            ai_line(d(10), "CREDITO AVULSO", "-12.00", kind="other_credit"),
        ], closing)
        rows, _ = cards.build_preview(self.user, invoice, None)
        statuses = [(r["description"], r["status"], r["amount"], r["kind"]) for r in rows]
        self.assertTrue(all(s == "ignore" for name, s, _, _ in statuses[:8]), statuses)
        self.assertEqual(statuses[8][1:3], ("import", Decimal("70.00")))
        self.assertEqual(statuses[9][1], "ignore")
        self.assertEqual(statuses[10][1:], ("import", Decimal("12.00"), "income"))
        self.assertEqual(rows[10]["category"], "Estornos e descontos")
        self.assertFalse(any(r["plan"] for r in rows))

    def test_statement_with_carried_balance_payment_and_discounts(self):
        closing = d(4)
        invoice = cards.from_ai({"issuer": "Carrefour", "closing_date": closing.isoformat(), "due_date": "",
                                 "invoice_total": "884.13", "lines": [
            ai_line(d(30), "SALDO FATURA ANTERIOR", "1620.40", kind="previous_balance", card=""),
            ai_line(d(90), "DH E LN OTICA,LONDRINA", "100.00", 3, 7, card="5926"),
            ai_line(d(60), "Desconto Exclusivo Cartão", "4,02-", kind="other_credit", card="5926"),
            ai_line(d(30), "Pagamento Banco CSF", "-1620.40", kind="payment", card="5926"),
            ai_line(d(20), "POSTO 353 PLD LONDRINA - 1/1", "235.58", card="5926"),
            ai_line(d(10), "Wellhub Gabriel de Sou, Sao Paulo", "552.57", card="0316"),
        ]}, "pdf-ia")
        rows, _ = cards.build_preview(self.user, invoice, None)
        self.assertEqual([r["status"] for r in rows], ["ignore", "import", "import", "payment", "import", "import"])
        self.assertEqual((rows[2]["kind"], rows[2]["amount"], rows[2]["category"]), ("income", Decimal("4.02"), "Estornos e descontos"))
        self.assertEqual(rows[1]["plan"]["first"], 4)
        self.assertEqual(rows[4]["name"], "POSTO 353 PLD LONDRINA")
        self.assertIsNone(rows[4]["plan"])
        self.assertEqual(rows[5]["category"], "Saúde")
        summary = cards.summarise(rows, invoice)
        self.assertTrue(summary["total_ok"], summary["lines_sum"])
        self.assertEqual(summary["by_card"][0][0], "0316")

    def test_statement_listing_only_the_payment_still_checks_the_total(self):
        invoice = cards.from_ai({"issuer": "Nubank", "holder": "LETICIA", "closing_date": d(3).isoformat(),
                                 "due_date": "", "invoice_total": "2683.95", "lines": [
            ai_line(d(30), "Mercadolivre*Mercadol", "129.83", 2, 2, card="3670"),
            ai_line(d(20), "Planos Supermercados", "2554.12", card="3670"),
            ai_line(d(25), "Pagamento em 06 OUT", "−2326.06", kind="payment", card=""),
        ]}, "pdf-ia")
        rows, _ = cards.build_preview(self.user, invoice, None)
        self.assertEqual(rows[2]["status"], "payment")
        self.assertEqual(rows[2]["amount"], Decimal("2326.06"))
        summary = cards.summarise(rows, invoice)
        self.assertTrue(summary["total_ok"])
        self.assertEqual(summary["cards_seen"], ["3670"])
        self.assertIsNone(rows[0]["plan"])

    def test_old_installment_is_booked_on_the_closing_date(self):
        closing = d(4)
        invoice = self.invoice([ai_line(d(160), "CV PHONE CENTER LT", "262.00", 5, 12)], closing)
        rows, reference = cards.build_preview(self.user, invoice, None)
        row = rows[0]
        self.assertEqual(reference, closing)
        self.assertEqual(row["date"], closing)
        self.assertEqual(row["name"], "CV PHONE CENTER LT (5/12)")
        self.assertEqual(row["plan"]["first"], 6)
        self.assertEqual(row["plan"]["end"], services.add_months(closing, 7))


class ImportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit")

    def run_import(self, lines, payment_account=None, choices=None):
        invoice = cards.read_upload("f.csv", nubank_csv(lines), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        preview = cards.from_session(cards.to_session({
            "account_id": self.card.pk, "new_account_name": "", "issuer": "Nubank", "source": "csv-nubank",
            "file_name": "f.csv", "reference_date": reference, "closing_date": None, "due_date": None, "total": None,
            "rows": rows,
        }))
        cats = {c.name.lower(): c for c in self.user.categories.all()}
        if choices is None:
            choices = {r["i"]: {"include": True, "category": cards.default_choice(r, cats), "rule": False}
                       for r in preview["rows"] if r["status"] == "import"}
        if not choices:
            return None, preview["rows"]
        return cards.apply_import(self.user, preview, choices, payment_account), preview["rows"]

    def test_import_creates_transactions_plan_tag_and_history(self):
        invoice, _ = self.run_import([(d(0), "Bela Comfort - Parcela 1/2", "149,95"), (d(2), "Dl *Uberrides", "8,48")])
        txs = Transaction.objects.filter(user=self.user, account=self.card)
        self.assertEqual(txs.count(), 2)
        self.assertNotIn("Parcelas", [t.category.name for t in txs])
        self.assertTrue(all(t.tags.filter(name__startswith="Fatura Nubank").exists() for t in txs))
        installment = txs.get(name__startswith="Bela")
        self.assertTrue(installment.tags.filter(name="parcelado").exists())
        self.assertFalse(txs.get(name__startswith="Dl").tags.filter(name="parcelado").exists())
        plan = RecurringTransaction.objects.get(user=self.user)
        self.assertEqual((plan.installment_first, plan.installment_total, plan.amount), (2, 2, Decimal("149.95")))
        self.assertEqual(plan.category, installment.category)
        services.apply_recurring_item(plan, plan.start_date)
        posted = Transaction.objects.get(recurring=plan)
        self.assertTrue(posted.tags.filter(name="parcelado").exists())
        self.assertEqual(plan.start_date, services.add_months(d(0), 1))
        self.assertEqual(plan.end_date, plan.start_date)
        self.assertEqual(plan.account, self.card)
        self.assertEqual((invoice.lines_imported, invoice.plans_created, invoice.amount_imported), (2, 1, Decimal("158.43")))
        self.assertEqual(CardInvoice.objects.filter(user=self.user).count(), 1)

    def test_reimporting_the_same_file_marks_duplicates(self):
        lines = [(d(2), "Dl *Uberrides", "8,48"), (d(2), "Dl *Uberrides", "8,48"), (d(3), "Bela - Parcela 1/3", "50,00")]
        self.run_import(lines)
        _, rows = self.run_import(lines)
        self.assertEqual([r["status"] for r in rows], ["duplicate", "duplicate", "duplicate"])
        self.assertEqual(RecurringTransaction.objects.filter(user=self.user).count(), 1)

    def test_next_invoice_fills_the_scheduled_installment(self):
        self.run_import([(d(40), "Bela - Parcela 1/3", "50,00")])
        plan = RecurringTransaction.objects.get(user=self.user)
        slot = plan.start_date
        _, rows = self.run_import([(d(5), "Bela - Parcela 2/3", "50,00")])
        self.assertEqual(rows[0]["recurring_id"], plan.pk)
        tx = Transaction.objects.get(user=self.user, recurring=plan)
        self.assertEqual(tx.recurring_date, slot)
        plan.refresh_from_db()
        self.assertEqual(plan.next_due, services.following_date(plan, slot))
        self.assertEqual(RecurringTransaction.objects.filter(user=self.user).count(), 1)
        services.apply_due(self.user, today=slot)
        self.assertEqual(Transaction.objects.filter(user=self.user, recurring=plan).count(), 1)

    def test_installment_generated_by_recurrence_is_not_imported_again(self):
        self.run_import([(d(40), "Bela - Parcela 1/3", "50,00")])
        plan = RecurringTransaction.objects.get(user=self.user)
        services.apply_due(self.user, today=plan.start_date)
        _, rows = self.run_import([(d(5), "Bela - Parcela 2/3", "50,00")])
        self.assertEqual(rows[0]["status"], "duplicate")

    def test_payment_becomes_transfer_and_rule_is_created(self):
        bank = self.account()
        rows_lines = [(d(1), "Pagamento recebido", "- 100,00"), (d(2), "Walcirjose", "15,00")]
        invoice = cards.read_upload("f.csv", nubank_csv(rows_lines), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        choices = {1: {"include": True, "category": f"c:{self.cat(name='Transporte').pk}", "rule": True}}
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, choices, payment_account=bank)
        transfer = Transfer.objects.get(user=self.user)
        self.assertEqual((transfer.from_account, transfer.to_account, transfer.amount), (bank, self.card, Decimal("100.00")))
        self.assertTrue(CategoryRule.objects.filter(user=self.user, pattern="walcirjose", category__name="Transporte").exists())

    def test_renamed_installment_keeps_shop_for_matching(self):
        lines = [(d(40), "Shopee *Loja - Parcela 1/3", "30,00")]
        invoice = cards.read_upload("f.csv", nubank_csv(lines), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Parcelas", "rule": False,
                                                    "name": "Fone bluetooth", "note": "presente da Ana"}})
        tx = Transaction.objects.get(user=self.user)
        self.assertEqual((tx.name, tx.payee), ("Fone bluetooth (1/3)", "Shopee *Loja"))
        self.assertTrue(tx.note.startswith("presente da Ana"))
        plan = RecurringTransaction.objects.get(user=self.user)
        self.assertEqual((plan.name, plan.payee), ("Fone bluetooth (parcelado em 3x)", "Shopee *Loja"))
        _, again = self.run_import(lines)
        self.assertEqual(again[0]["status"], "duplicate")
        _, following = self.run_import([(d(5), "Shopee *Loja - Parcela 2/3", "30,00")])
        self.assertEqual(following[0]["recurring_id"], plan.pk)

    def test_edited_date_and_amount_are_used_and_validated(self):
        from django.core.exceptions import ValidationError
        invoice = cards.read_upload("f.csv", nubank_csv([(d(3), "Loja - Parcela 1/2", "30,00")]), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        bad = {0: {"include": True, "category": "n:Parcelas", "amount": "0"}}
        with self.assertRaises(ValidationError):
            cards.apply_import(self.user, preview, bad)
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Parcelas",
                                                    "date": d(1).isoformat(), "amount": "31.50"}})
        tx = Transaction.objects.get(user=self.user)
        self.assertEqual((tx.date, tx.amount), (d(1), Decimal("31.50")))
        plan = RecurringTransaction.objects.get(user=self.user)
        self.assertEqual((plan.amount, plan.start_date), (Decimal("31.50"), services.add_months(d(1), 1)))

    def test_preview_page_has_the_edit_modal(self):
        upload = SimpleUploadedFile("f.csv", nubank_csv([(d(1), "Dl *Uberrides", "8,48")]), "text/csv")
        self.client.post(reverse("cards"), {"file": upload, "date_mode": "purchase"})
        page = self.client.get(reverse("cards"))
        self.assertContains(page, 'id="row-dialog"')
        self.assertContains(page, 'data-edit-row="0"')
        self.assertContains(page, 'name="amount_0" value="8.48"')
        self.assertContains(page, "app/js/cards.js?v=")

    def test_a_rule_can_be_learnt_from_an_installment(self):
        invoice = cards.read_upload("f.csv", nubank_csv([(d(1), "Shopee *Loja - Parcela 1/3", "30,00")]), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Compras", "rule": True}})
        self.assertEqual(CategoryRule.objects.get(user=self.user).category.name, "Compras")

    def test_due_date_mode_books_on_the_future_due_date(self):
        due = today() + datetime.timedelta(days=5)
        invoice = cards.from_ai({"issuer": "Itaú", "closing_date": d(2).isoformat(), "due_date": due.isoformat(),
                                 "invoice_total": "", "lines": [ai_line(d(10), "OBRAMAX", "49.29")]}, "pdf-ia")
        rows, reference = cards.build_preview(self.user, invoice, self.card, "due")
        self.assertEqual((rows[0]["status"], rows[0]["date"]), ("import", due))
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Itaú", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Compras", "rule": False}})
        self.assertEqual(Transaction.objects.get(user=self.user).date, due)

    def test_future_dates_up_to_a_year_on_any_account(self):
        from django.core.exceptions import ValidationError
        future = today() + datetime.timedelta(days=60)
        tx = Transaction(user=self.user, name="X", amount=Decimal("1.00"), kind="expense", date=future,
                         category=self.cat(name="Compras"), account=self.card)
        tx.full_clean()
        tx.account = self.account()
        tx.full_clean()
        tx.date = today() + datetime.timedelta(days=400)
        with self.assertRaises(ValidationError):
            tx.full_clean()

    def test_closed_month_blocks_the_import(self):
        MonthLock.objects.create(user=self.user, month=services.month_start(d(0)))
        _, rows = self.run_import([(d(0), "Dl *Uberrides", "8,48")], choices={})
        self.assertEqual(rows[0]["status"], "error")


class ViewTests(AppTestCase):
    def test_upload_preview_and_confirm(self):
        upload = SimpleUploadedFile("Nubank_fatura.csv", nubank_csv([(d(1), "Dl *Uberrides", "8,48")]), "text/csv")
        response = self.client.post(reverse("cards"), {"file": upload, "date_mode": "purchase"})
        self.assertRedirects(response, reverse("cards"))
        page = self.client.get(reverse("cards"))
        self.assertContains(page, "Cartão Nubank")
        self.assertContains(page, "Transporte")
        stored = self.client.session["card_preview"]
        response = self.client.post(reverse("card_confirm"), {"token": stored["token"], "inc_0": "on",
                                                              "cat_0": f"c:{self.cat(name='Transporte').pk}"})
        card = Account.objects.get(user=self.user, name="Cartão Nubank")
        self.assertEqual(card.type, "credit")
        self.assertRedirects(response, f"{reverse('transactions')}?account={card.pk}", fetch_redirect_response=False)
        self.assertEqual(Transaction.objects.filter(user=self.user, account=card).count(), 1)

    def test_pdf_without_api_key_explains_what_is_missing(self):
        upload = SimpleUploadedFile("fatura.pdf", b"%PDF-1.4 test", "application/pdf")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            response = self.client.post(reverse("cards"), {"file": upload, "date_mode": "purchase"})
        self.assertContains(response, "leitura automática de PDF está desligada")
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 0)

    def test_pasted_standard_csv(self):
        pasted = (
            "```\n# emissor: Itaú\n# titular: Fulano\n# fechamento: " + d(3).strftime("%d/%m/%Y") + "\n# total: 311,29\n"
            "data;descricao;valor;parcela;total_parcelas;cartao;tipo;categoria\n"
            + d(160).strftime("%d/%m/%Y") + ";CV PHONE CENTER LT;262,00;5;12;0891;compra;\n"
            + d(20).strftime("%d/%m/%Y") + ";OBRAMAX;49,29;;;0891;compra;Compras\n```\n"
        )
        response = self.client.post(reverse("cards"), {"pasted": pasted, "date_mode": "purchase"})
        self.assertRedirects(response, reverse("cards"))
        page = self.client.get(reverse("cards"))
        self.assertContains(page, "CSV padrão de fatura")
        self.assertContains(page, "Os lançamentos somam o total da fatura.")
        self.assertContains(page, "Agenda as parcelas 6 a 12")
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 0)

    def test_page_offers_the_prompt_with_my_categories(self):
        page = self.client.get(reverse("cards"))
        self.assertContains(page, "data;descricao;valor;parcela;total_parcelas;cartao;tipo;categoria")
        self.assertContains(page, "Mercado")

    def test_nothing_sent_means_a_form_error(self):
        response = self.client.post(reverse("cards"), {"date_mode": "purchase"})
        self.assertContains(response, "Escolha o arquivo da fatura ou cole o CSV")

    def test_pdf_goes_through_the_ai_reader(self):
        data = {"issuer": "Itaú", "closing_date": d(3).isoformat(), "due_date": "", "invoice_total": "49.29",
                "lines": [ai_line(d(10), "OBRAMAX", "49.29", category="Compras")]}
        upload = SimpleUploadedFile("fatura.pdf", b"%PDF-1.4 test", "application/pdf")
        with mock.patch("app.invoice_ai.available", return_value=True), \
                mock.patch("app.invoice_ai.read_pdf", return_value=data) as reader:
            self.client.post(reverse("cards"), {"file": upload, "date_mode": "purchase"})
        reader.assert_called_once()
        page = self.client.get(reverse("cards"))
        self.assertContains(page, "Cartão Itaú")
        self.assertContains(page, "sugestão da IA")
        self.assertContains(page, "Os lançamentos somam o total da fatura.")

    def test_other_users_cannot_use_my_card(self):
        mine = Account.objects.create(user=self.other, name="Dela", type="credit")
        upload = SimpleUploadedFile("f.csv", nubank_csv([(d(1), "Uber", "8,48")]), "text/csv")
        response = self.client.post(reverse("cards"), {"file": upload, "account": mine.pk, "date_mode": "purchase"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("card_preview", self.client.session)


class TypedByHandTests(AppTestCase):
    """Purchases typed by hand before the invoice arrives are not counted twice."""

    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit")

    def preview(self, lines):
        invoice = cards.read_upload("f.csv", nubank_csv(lines), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        return rows, reference

    def parcelada(self, name="Geladeira", amount="300", total=10, first=1, start=None):
        start = start or d(40)
        response = self.post("/api/recurring/", {
            "name": name, "amount": amount, "kind": "expense", "category": self.cat(name="Shopping").pk,
            "account": self.card.pk, "frequency": "monthly", "start_date": start.isoformat(), "end_date": "",
            "installment_total": total, "installment_first": first})
        self.assertEqual(response.status_code, 201, response.content)
        return RecurringTransaction.objects.get(user=self.user, installment_total=total)

    def test_recurring_installment_purchase_looks_like_the_card_ones(self):
        plan = self.parcelada(start=d(40))
        self.assertEqual(plan.name, "Geladeira (parcelado em 10x)")
        self.assertEqual((plan.installment_first, plan.frequency, plan.end_date),
                         (1, "monthly", services.add_months(d(40), 9, anchor_day=d(40).day)))
        posted = Transaction.objects.filter(recurring=plan).order_by("date")
        self.assertEqual([t.name for t in posted][:2], ["Geladeira (1/10)", "Geladeira (2/10)"])
        self.assertTrue(all(t.tags.filter(name="parcelado").exists() for t in posted))
        self.assertEqual(self.post("/api/recurring/", {
            "name": "X", "amount": "10", "kind": "expense", "category": self.cat(name="Shopping").pk, "frequency": "monthly",
            "start_date": d(1).isoformat(), "installment_total": 1}).status_code, 422)

    def test_invoice_recognises_installments_typed_by_hand(self):
        plan = self.parcelada(start=d(40))          # installments 1 and 2 already posted by the plan
        rows, _ = self.preview([(d(40), "MAGAZINE LUIZA - Parcela 1/10", "300,00"),
                                (d(5), "MAGAZINE LUIZA - Parcela 3/10", "300,00")])
        self.assertEqual(rows[0]["status"], "duplicate")
        self.assertEqual((rows[1]["status"], rows[1]["recurring_id"]), ("import", plan.pk))
        self.assertIsNone(rows[1]["plan"])            # no second plan for the same purchase
        preview = {"account_id": self.card.pk, "reference_date": d(0), "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {1: {"include": True, "category": "n:Compras", "rule": False}})
        plan.refresh_from_db()
        self.assertEqual(plan.payee, "MAGAZINE LUIZA")
        self.assertEqual(RecurringTransaction.objects.filter(user=self.user).count(), 1)

    def test_single_purchase_typed_by_hand_is_left_unticked(self):
        Transaction.objects.create(user=self.user, name="Tênis", amount=Decimal("250.00"), kind="expense",
                                   category=self.cat(name="Shopping"), account=self.card, date=d(3))
        rows, _ = self.preview([(d(1), "NETSHOES", "250,00"), (d(1), "PADARIA", "12,00")])
        self.assertEqual(rows[0]["status"], "import")
        self.assertEqual(rows[0]["typed"]["name"], "Tênis")
        self.assertNotIn("typed", rows[1])
        self.assertEqual(cards.summarise(rows, {"total": None, "lines": []})["typed"], 1)


class AlreadyRegisteredTests(AppTestCase):
    """Alteração 5: purchases already in the card are recognised in the invoice review."""

    def setUp(self):
        super().setUp()
        self.card = Account.objects.create(user=self.user, name="Nubank", type="credit", due_day=19)

    def preview(self, lines, due=None):
        invoice = cards.from_ai({"issuer": "Nubank", "closing_date": d(1).isoformat(),
                                 "due_date": due.isoformat() if due else "", "invoice_total": "",
                                 "lines": [ai_line(day, text, value) for day, text, value in lines]}, "pdf-ia")
        rows, reference = cards.build_preview(self.user, invoice, self.card, "due" if due else "purchase")
        return rows, reference

    def typed(self, name, amount, date):
        return Transaction.objects.create(user=self.user, name=name, amount=Decimal(amount), kind="expense",
                                          category=self.cat(name="Shopping"), account=self.card, date=date)

    def test_quick_entry_dated_at_the_due_date_is_recognised(self):
        due = today() + datetime.timedelta(days=8)
        self.card.due_day = due.day                               # the invoice below has no due date
        self.card.save()
        self.typed("Tênis", "199.90", due)                       # quick entry on a card books the due date
        rows, _ = self.preview([(d(10), "NETSHOES", "199.90")])  # purchase date, far from the due date
        self.assertEqual(rows[0]["typed"]["name"], "Tênis")
        rows, _ = self.preview([(d(10), "NETSHOES", "199.90")], due=due)
        self.assertEqual(rows[0]["typed"]["name"], "Tênis")

    def test_similar_amount_counts_but_not_a_different_one(self):
        self.typed("Farmácia", "100.00", d(3))
        rows, _ = self.preview([(d(2), "DROGASIL", "101.50"), (d(2), "DROGARIA", "103.00")])
        self.assertIn("typed", rows[0])
        self.assertNotIn("typed", rows[1])

    def test_a_subscription_not_posted_yet_is_taken_by_the_invoice_line(self):
        item = RecurringTransaction.objects.create(
            user=self.user, name="Meli +", amount=Decimal("74.90"), kind="expense", category=self.cat(name="Shopping"),
            account=self.card, frequency="monthly", start_date=d(2), next_due=d(2))
        rows, reference = self.preview([(d(3), "MERCADOLIVRE*MELIMAIS", "74.90")])
        self.assertEqual((rows[0]["status"], rows[0]["recurring_id"], rows[0]["slot"]), ("import", item.pk, d(2)))
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Assinaturas", "rule": False}})
        self.assertEqual(Transaction.objects.filter(recurring=item, recurring_date=d(2)).count(), 1)
        self.assertEqual(services.apply_recurring_item(item, d(0)), 0)   # not posted a second time
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 1)

    def test_a_duplicate_can_still_be_ticked(self):
        lines = [(d(2), "Dl *Uberrides", "8,48")]
        invoice = cards.read_upload("f.csv", nubank_csv(lines), self.user)
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Transporte", "rule": False}})
        rows, reference = cards.build_preview(self.user, invoice, self.card)
        self.assertEqual(rows[0]["status"], "duplicate")
        preview = {"account_id": self.card.pk, "reference_date": reference, "issuer": "Nubank", "rows": rows}
        cards.apply_import(self.user, preview, {0: {"include": True, "category": "n:Transporte", "rule": False}})
        self.assertEqual(Transaction.objects.filter(user=self.user, account=self.card).count(), 2)

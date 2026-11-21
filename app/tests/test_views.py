import datetime
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from app import services
from app.models import Account, Budget, Category, MonthLock, Profile, Transaction

from .base import AppTestCase, PASSWORD, today

User = get_user_model()


class PageTests(AppTestCase):
    def test_logout_form_is_post_with_csrf(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertRegex(html, r'<form method="post" action="/logout/">\s*<input type="hidden" name="csrfmiddlewaretoken"')

    def test_money_template_uses_profile(self):
        profile = services.get_profile(self.user)
        profile.currency_symbol, profile.number_format = "$", "intl"
        profile.save()
        services.forget_profile(self.user)
        self.tx(amount="1234.5")
        self.assertContains(self.client.get(reverse("transactions")), "-$1,234.50")

    def test_dashboard_applies_due_recurring(self):
        from app.models import RecurringTransaction
        RecurringTransaction.objects.create(user=self.user, name="Gym", amount=Decimal("5"), kind="expense", category=self.cat(),
                                            start_date=today(), next_due=today(), account=self.account())
        self.client.get(reverse("dashboard"))
        self.assertTrue(Transaction.objects.filter(name="Gym").exists())

class SidebarMarkupTests(AppTestCase):
    def test_at_most_seven_top_level_items(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
        top_links = len(re.findall(r'^\s{8}<a ', nav, re.M))
        groups = len(re.findall(r'class="nav-group"', nav))
        self.assertLessEqual(top_links + groups, 7)

    def test_help_dialog_lists_sidebar_shortcut(self):
        self.assertContains(self.client.get(reverse("dashboard")), "<kbd>[</kbd>")

    def test_bottom_nav_and_fab(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('class="bottom-nav"', html)
        self.assertIn('class="fab"', html)

    def test_static_assets_exist(self):
        from django.contrib.staticfiles import finders
        for path in ("app/js/sidebar-init.js", "app/js/layout.js", "app/css/app.css", "app/icons/sprite.svg", "app/icons/LICENSE",
                     "app/js/password.js", "app/js/register.js", "app/js/common-passwords.js"):
            self.assertIsNotNone(finders.find(path), path)

    def test_css_has_collapsed_and_drawer_rules(self):
        from django.contrib.staticfiles import finders
        css = open(finders.find("app/css/app.css"), encoding="utf-8").read()
        self.assertIn('html[data-sidebar="collapsed"]', css)
        self.assertIn("html:not(.js) .sidebar", css)
        self.assertIn("prefers-reduced-motion", css)
        js = open(finders.find("app/js/layout.js"), encoding="utf-8").read()
        for needle in ("localStorage", "inert", "Escape", "aria-expanded", "event.key === '['"):
            self.assertIn(needle, js)

    def test_icon_tag(self):
        from django.template import Context, Template
        out = Template('{% icon "wallet" %}{% icon "nope" %}').render(Context())
        self.assertIn('href="/static/app/icons/sprite.svg#wallet"', out)
        self.assertEqual(out.count("<svg"), 1)


class SettingsPageTests(AppTestCase):
    def valid(self, **kw):
        d = {"display_name": "Alice Smith", "currency_code": "usd", "currency_symbol": "$", "number_format": "indian",
             "allow_negative_balance": "no", "secondary_currency_code": "", "secondary_rate": ""}
        d.update(kw)
        return d

    def test_save_settings(self):
        r = self.client.post(reverse("settings"), self.valid())
        self.assertRedirects(r, reverse("settings"))
        profile = Profile.objects.get(user=self.user)
        self.assertEqual((profile.currency_code, profile.number_format, profile.allow_negative_balance), ("USD", "indian", False))
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Alice", "Smith"))

    def test_single_word_display_name(self):
        self.client.post(reverse("settings"), self.valid(display_name="Cher"))
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Cher", ""))

    def test_validation(self):
        for bad in ({"currency_code": "US"}, {"currency_code": "12$"}, {"currency_symbol": "a b"}, {"number_format": "zz"},
                    {"secondary_currency_code": "EUR"}, {"secondary_rate": "2"}, {"secondary_currency_code": "EU", "secondary_rate": "1"}):
            r = self.client.post(reverse("settings"), self.valid(**bad))
            self.assertEqual(r.status_code, 200, bad)
            self.assertTrue(r.context["form"].errors, bad)

    def test_secondary_currency(self):
        self.client.post(reverse("settings"), self.valid(secondary_currency_code="eur", secondary_rate="0.0071"))
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.secondary_currency_code, "EUR")
        self.assertEqual(profile.secondary_rate, Decimal("0.0071"))
        self.assertEqual(self.json(self.client.get(reverse("api_summary")))["secondary"]["code"], "EUR")

    def test_change_password(self):
        r = self.client.post(reverse("change_password"), {"old_password": PASSWORD, "new_password1": "new-Secret-pass-99", "new_password2": "new-Secret-pass-99"})
        self.assertRedirects(r, reverse("settings"))
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)  # session kept
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-Secret-pass-99"))

    def test_change_password_errors_are_inline(self):
        r = self.client.post(reverse("change_password"), {"old_password": "bad", "new_password1": "short", "new_password2": "short"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "field-error")
        self.assertNotContains(r, "<ul><li>Your password")

    def test_lock_validation(self):
        self.client.post(reverse("lock_month"), {"month": "junk", "action": "close"})
        future = services.add_months(today().replace(day=1), 3).strftime("%Y-%m")
        self.client.post(reverse("lock_month"), {"month": future, "action": "close"})
        self.assertFalse(MonthLock.objects.exists())

    def test_closed_month_blocks_bulk_and_delete_and_transfers(self):
        t = self.tx()
        MonthLock.objects.create(user=self.user, month=today().replace(day=1))
        self.assertEqual(self.delete(reverse("api_transaction", args=[t.pk])).status_code, 422)
        r = self.post(reverse("api_transactions_bulk"), {"action": "category", "ids": [t.pk], "category": self.cat(name="Health").pk})
        self.assertEqual(r.status_code, 422)

    def test_delete_account(self):
        self.tx()
        self.client.post(reverse("delete_account"), {"confirm": "wrong", "password": PASSWORD})
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.client.post(reverse("delete_account"), {"confirm": "alice", "password": PASSWORD})
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(Transaction.all_objects.filter(user=self.user.pk).count(), 0)
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)


class WelcomeFlowTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.newbie = self.make_user("newbie", setup=False)
        self.client.force_login(self.newbie)

    def form(self, **kw):
        d = {"currency_code": "npr", "currency_symbol": "Rs", "number_format": "indian", "main_name": "Everyday", "main_opening": "1500",
             "cash": "on", "cash_opening": "200", "categories": ["Groceries", "Salary", "Rent"], "monthly_budget": "30000"}
        d.update(kw)
        return d

    def test_dashboard_redirects_until_setup(self):
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("welcome"))

    def test_skip(self):
        self.client.post(reverse("welcome"), {"skip": "1"})
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertTrue(self.newbie.categories.exists())

    def test_validation_errors_shown(self):
        r = self.client.post(reverse("welcome"), self.form(currency_code="x", main_name=""))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "field-error")
        self.assertFalse(services.get_profile(self.newbie).setup_done)

class TrashAndDigestPageTests(AppTestCase):
    def test_trash_page_and_flow(self):
        t = self.tx(name="Gone")
        self.delete(reverse("api_transaction", args=[t.pk]))
        self.assertEqual(self.client.get(reverse("trash")).status_code, 200)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_digest_numbers(self):
        self.tx(amount="40")
        self.tx(amount="100", kind="income")
        r = self.client.get(reverse("digest"))
        self.assertEqual(r.context["d"]["expense"], Decimal("40.00"))
        self.assertEqual(r.context["d"]["count"], 2)


class PrivacyPageTests(AppTestCase):
    def test_public_and_says_nothing_leaves_the_phone(self):
        self.client.logout()
        r = self.client.get("/privacidade/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "somente no seu celular")
        self.assertContains(r, "Nenhum.")

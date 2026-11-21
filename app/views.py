"""HTML pages. Data-heavy pages fetch their content from app.api."""
import csv
import datetime
import os
import re
import secrets
import unicodedata

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import backup as backup_lib
from . import cards, csvio, invoice_ai, insights as insight_lib, reports as report_lib, services
from .filters import PAGE_SIZES, SORT_OPTIONS, TransactionFilter
from .forms import CardInvoiceForm, ChangePasswordForm, ImportForm, RegisterForm, SettingsForm, SetupForm
from .icons import CATEGORY_ICONS
from .models import ACCOUNT_TYPES, COLOR_CHOICES, LEDGER_KINDS, CardInvoice, Category, MonthLock, Profile, Tag, Transaction
from .formatting import month_label
from .ratelimit import Throttle, client_ip

USERNAME_RE = re.compile(r"^[\w.@+-]+$")

login_by_user = Throttle("login-user", settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_LOCKOUT_SECONDS)
login_by_ip = Throttle("login-ip", settings.LOGIN_MAX_ATTEMPTS * 4, settings.LOGIN_LOCKOUT_SECONDS)
register_by_ip = Throttle("register-ip", settings.REGISTER_MAX_PER_HOUR, 3600)


class LoginForm(AuthenticationForm):
    """Accepts the username in any letter case (registration stores it lowercase)."""

    def clean_username(self):
        raw = self.cleaned_data["username"].strip()
        match = User.objects.filter(username__iexact=raw).values_list("username", flat=True).first()
        return match or raw


class LoginView(auth_views.LoginView):
    authentication_form = LoginForm
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        return super().get_context_data(allow_registration=settings.ALLOW_REGISTRATION, **kwargs)

    def post(self, request, *args, **kwargs):
        self.throttle_name = (request.POST.get("username") or "").strip().lower()[:150]
        self.throttle_ip = client_ip(request)
        if login_by_user.blocked(self.throttle_name, self.throttle_ip) or login_by_ip.blocked(self.throttle_ip):
            form = self.get_form_class()(request)
            response = self.render_to_response(self.get_context_data(form=form, locked=True))
            response.status_code = 429
            return response
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        login_by_user.hit(self.throttle_name, self.throttle_ip)
        login_by_ip.hit(self.throttle_ip)
        return super().form_invalid(form)

    def form_valid(self, form):
        login_by_user.reset(self.throttle_name, self.throttle_ip)
        return super().form_valid(form)


@require_POST
def logout_view(request):
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


PRIVACY_UPDATED = datetime.date(2026, 11, 13)


@require_GET
def privacy(request):
    """Privacy policy of the Android app (public: the Play Store links to it)."""
    return render(request, "app/privacy.html", {"updated": PRIVACY_UPDATED,
                                                "contact": os.environ.get("PRIVACY_CONTACT", "").strip()})


@never_cache
def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if not settings.ALLOW_REGISTRATION:
        raise Http404
    ip = client_ip(request)
    if request.method == "POST":
        if register_by_ip.blocked(ip):
            messages.error(request, "Muitos cadastros a partir deste endereço. Tente novamente mais tarde.")
            return render(request, "registration/register.html", {"form": RegisterForm()}, status=429)
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            register_by_ip.hit(ip)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Boas-vindas! Sua conta está pronta.")
            return redirect("dashboard")
    else:
        form = RegisterForm()
    return render(request, "registration/register.html", {"form": form})


username_check = Throttle("username-check", 30, 60)


@require_GET
def username_available(request):
    """Is this username free? Reveals no more than the sign-up form itself (rate limited)."""
    if not settings.ALLOW_REGISTRATION:
        raise Http404
    ip = client_ip(request)
    if username_check.hit(ip) > username_check.limit:
        response = JsonResponse({"ok": False, "message": "Muitas verificações. Tente novamente em um minuto."}, status=429)
        response["Retry-After"] = "60"
        return response
    raw = unicodedata.normalize("NFKC", (request.GET.get("u") or "")).strip().lower()
    valid = bool(raw) and len(raw) <= 150 and USERNAME_RE.match(raw) is not None
    available = valid and not User.objects.filter(username__iexact=raw).exists()
    response = JsonResponse({"ok": True, "valid": valid, "available": available})
    response["Cache-Control"] = "no-store"
    return response


# --- pages -------------------------------------------------------------------

@login_required
def dashboard(request):
    from .sync.local_views import first_run_redirect
    first = first_run_redirect(request)
    if first is not None:
        return first
    if not services.get_profile(request.user).setup_done:
        return redirect("welcome")
    services.apply_due(request.user)
    return render(request, "app/dashboard.html", {"month": timezone.localdate().strftime("%Y-%m")})


def _results_context(request, flt):
    paginator = Paginator(flt.queryset(), flt.page_size)
    page = paginator.get_page(flt.page)
    income, expense = services.income_expense_for(flt.queryset())
    return {
        "flt": flt, "page": page, "paginator": paginator, "income": income, "expense": expense,
        "net": income - expense,
        "headers": [
            flt.sort_header("date", "Data"), flt.sort_header("name", "Descrição"),
            flt.sort_header("category", "Categoria"),
        ],
        "amount_header": flt.sort_header("amount", "Valor", numeric=True),
        "pages": [
            {"gap": True} if n == paginator.ELLIPSIS else
            {"number": n, "href": flt.page_href(n), "current": n == page.number}
            for n in paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1)
        ],
        "prev_href": flt.page_href(page.previous_page_number()) if page.has_previous() else "",
        "next_href": flt.page_href(page.next_page_number()) if page.has_next() else "",
    }


@login_required
def transactions(request):
    services.apply_due(request.user)
    flt = TransactionFilter(request.user, request.GET)
    context = _results_context(request, flt)
    if request.GET.get("fragment") == "1":
        return render(request, "app/_transactions_results.html", context)
    context.update(
        page_sizes=PAGE_SIZES, categories=request.user.categories.all(), tag_list=Tag.objects.filter(user=request.user),
        accounts=request.user.accounts.all(), sort_options=SORT_OPTIONS)
    return render(request, "app/transactions.html", context)


@login_required
def categories(request):
    return render(request, "app/categories.html", {"colors": COLOR_CHOICES, "icons": CATEGORY_ICONS})


@login_required
def budgets(request):
    return render(request, "app/budgets.html", {"month": services.parse_month(request.GET.get("month")).strftime("%Y-%m")})


@login_required
def goals(request):
    return render(request, "app/goals.html", {"today": timezone.localdate()})


@login_required
def accounts(request):
    return render(request, "app/accounts.html", {"account_types": ACCOUNT_TYPES, "today": timezone.localdate()})


@login_required
def recurring(request):
    return render(request, "app/recurring.html", {"today": timezone.localdate()})


@login_required
def analytics(request):
    today = timezone.localdate()
    return render(request, "app/analytics.html", {
        "start": services.month_start(today), "end": today, "today": today,
        "categories": request.user.categories.all(), "tag_list": Tag.objects.filter(user=request.user),
        "accounts": request.user.accounts.filter(archived=False)})


def _report_range(request):
    today = timezone.localdate()

    def day(name):
        try:
            return datetime.date.fromisoformat(request.GET.get(name, ""))
        except ValueError:
            return None

    return report_lib.resolve_period(request.GET.get("preset", "this_month"), day("start"), day("end"), today)


@login_required
def reports(request):
    preset, start, end = _report_range(request)
    data = report_lib.build(request.user, start, end)
    return render(request, "app/reports.html", {
        "data": data, "preset": preset, "presets": report_lib.PRESETS, "start": start, "end": end,
        "report_list": report_lib.REPORTS, "query": f"start={start.isoformat()}&end={end.isoformat()}&preset={preset}",
        "generated": timezone.localtime()})


@login_required
def report_export(request):
    name = request.GET.get("report", "")
    if name not in dict(report_lib.REPORTS):
        raise Http404
    _, start, end = _report_range(request)
    header, rows = report_lib.csv_rows(name, report_lib.build(request.user, start, end))
    writer = csv.writer(csvio.Echo())

    def lines():
        yield "\ufeff"
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow(row)

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="report-{name}-{start.isoformat()}-{end.isoformat()}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    profile_version = services.get_profile(request.user).version
    if request.method == "POST":
        form = SettingsForm(request.POST, user=request.user)
        with transaction.atomic():
            # Changed in another tab, the site or the phone since this page was opened: keep theirs.
            sent = request.POST.get("version")
            saved = Profile.objects.select_for_update().filter(user=request.user).values_list("version", flat=True).first()
            if sent and sent.isdigit() and saved is not None and int(sent) != saved:
                messages.error(request, "Suas alterações não foram salvas: as configurações mudaram em outro lugar. "
                                        "Confira os valores atuais abaixo e salve de novo.")
                services.forget_profile(request.user)
                return redirect("settings")
            if form.is_valid():
                form.save()
                messages.success(request, "Configurações salvas.")
                return redirect("settings")
    else:
        form = SettingsForm(user=request.user)
    return render(request, "app/settings.html", {
        "form": form, "password_form": ChangePasswordForm(request.user), "locks": MonthLock.objects.filter(user=request.user),
        "current_month": timezone.localdate().strftime("%Y-%m"), "profile_version": profile_version})


@login_required
@require_POST
def change_password(request):
    form = ChangePasswordForm(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "Senha alterada.")
        return redirect("settings")
    return render(request, "app/settings.html", {
        "form": SettingsForm(user=request.user), "password_form": form, "locks": MonthLock.objects.filter(user=request.user),
        "current_month": timezone.localdate().strftime("%Y-%m")})


# --- CSV ---------------------------------------------------------------------

@login_required
@require_GET
def export_csv(request):
    flt = TransactionFilter(request.user, request.GET)
    qs = flt.queryset()
    bom = request.GET.get("bom", "1") != "0"
    response = StreamingHttpResponse(csvio.export_lines(qs, bom=bom), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="transactions-{timezone.localdate().isoformat()}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def import_page(request):
    form = ImportForm()
    if request.method == "POST":
        form = ImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                rows = csvio.parse(form.cleaned_data["file"].read(), request.user)
            except csvio.CsvImportError as exc:
                form.add_error("file", str(exc))
            else:
                request.session["import_preview"] = {"token": secrets.token_urlsafe(16), "rows": csvio.serialise(rows)}
                return redirect("import")
    preview = request.session.get("import_preview")
    context = {"form": form}
    if preview:
        rows = csvio.deserialise(preview["rows"])
        valid = [r for r in rows if not r["errors"]]
        context.update(
            token=preview["token"], rows=rows,
            n_valid=len([r for r in valid if not r["duplicate"]]),
            n_duplicate=len([r for r in valid if r["duplicate"]]),
            n_error=len(rows) - len(valid),
            new_categories=sorted({r["category"] for r in valid if r["new_category"]}, key=str.lower),
            n_rules=len([r for r in valid if r.get("rule_matched")]),
        )
    return render(request, "app/import.html", context)


@login_required
@require_POST
def import_cancel(request):
    request.session.pop("import_preview", None)
    return redirect("import")


@login_required
@require_POST
def import_confirm(request):
    preview = request.session.get("import_preview")
    if not preview or not secrets.compare_digest(preview["token"], request.POST.get("token", "")):
        messages.error(request, "Essa importação expirou. Envie o arquivo novamente.")
        return redirect("import")
    skip_duplicates = request.POST.get("skip_duplicates") == "on"
    rows = [r for r in csvio.deserialise(preview["rows"]) if not r["errors"] and not (skip_duplicates and r["duplicate"])]
    if not rows:
        messages.error(request, "Não há linhas válidas para importar.")
        return redirect("import")
    user = request.user
    try:
        with services.locked(user):
            cats = {c.name.lower(): c for c in user.categories.all()}
            accounts = {a.name.lower(): a for a in user.accounts.all()}
            fallback = services.default_account(user)
            made = []
            for r in rows:
                cat = cats.get(r["category"].lower())
                if cat is None:
                    cat = Category.objects.create(user=user, name=r["category"], kind="both")
                    cats[r["category"].lower()] = cat
                account = accounts.get(r["account"].lower()) if r["account"] else fallback
                made.append((Transaction(
                    user=user, name=r["name"], amount=r["amount"], kind=r["kind"], date=r["date"], category=cat,
                    account=account, payee=r["payee"], note=r["note"]), r["tags"]))
            services.ensure_unlocked(user, *{tx.date for tx, _ in made})
            services.ensure_effects(user, [(tx.account, tx.effect) for tx, _ in made])
            for tx, tag_names in made:
                tx.save()
                if tag_names:
                    services.set_tags(tx, tag_names)
                services.record(user, tx.pk, "imported", f"Importada: {tx.name} ({tx.amount:.2f})", None, services.snapshot(tx))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("import")
    created = made
    request.session.pop("import_preview", None)
    messages.success(request, f"{len(created)} transação(ões) importada(s).")
    return redirect("transactions")


# --- credit card statements ---------------------------------------------------------

CARD_SESSION_KEY = "card_preview"


def _card_target(user, form, invoice):
    """(account or None, name of the card to create)."""
    if form.cleaned_data.get("account"):
        return form.cleaned_data["account"], ""
    name = form.cleaned_data.get("new_account_name") or ""
    if name:
        existing = user.accounts.filter(name__iexact=name).first()
        if existing is not None and not existing.is_credit:
            raise cards.InvoiceError(f"Já existe a conta “{existing.name}” e ela não é um cartão. Escolha outro nome.")
        return existing, "" if existing else name
    found = cards.find_card_account(user, invoice["issuer"])
    if found is not None:
        return found, ""
    return None, f"Cartão {invoice['issuer']}".strip() if invoice["issuer"] else "Cartão de crédito"


@login_required
@require_http_methods(["GET", "POST"])
def card_invoices(request):
    user = request.user
    form = CardInvoiceForm(user=user)
    if request.method == "POST":
        form = CardInvoiceForm(request.POST, request.FILES, user=user)
        if form.is_valid():
            upload = form.cleaned_data["file"]
            if upload:
                file_name, raw = upload.name, upload.read()
            else:
                file_name, raw = "colado.csv", form.cleaned_data["pasted"].encode("utf-8")
            try:
                invoice = cards.read_upload(file_name, raw, user)
                account, new_name = _card_target(user, form, invoice)
                # One rule for every card entry (Settings): the invoice's due date or the purchase date.
                date_mode = "due" if services.get_profile(user).card_date_mode == "invoice" else "purchase"
                rows, reference = cards.build_preview(user, invoice, account, date_mode)
            except cards.InvoiceError as exc:
                form.add_error("file", str(exc))
            else:
                request.session[CARD_SESSION_KEY] = cards.to_session({
                    "token": secrets.token_urlsafe(16), "account_id": account.pk if account else None,
                    "new_account_name": new_name, "issuer": invoice["issuer"], "holder": invoice.get("holder", ""),
                    "source": invoice["source"],
                    "file_name": invoice["file_name"], "reference_date": reference,
                    "closing_date": invoice["closing_date"], "due_date": invoice["due_date"], "total": invoice["total"],
                    "date_mode": date_mode, "rows": rows,
                })
                return redirect("cards")
    context = {"form": form, "ai_problem": invoice_ai.unavailable_reason(), "ai_label": invoice_ai.PROVIDER_LABEL,
               "history": CardInvoice.objects.filter(user=user).select_related("account")[:12],
               "source_labels": cards.SOURCE_LABELS, "standard_prompt": cards.standard_prompt(user)}
    stored = request.session.get(CARD_SESSION_KEY)
    if stored:
        preview = cards.from_session(stored)
        rows = preview["rows"]
        account = user.accounts.filter(pk=preview["account_id"]).first() if preview["account_id"] else None
        user_categories = {c.name.lower(): c for c in user.categories.all()}
        for row in rows:
            # Duplicates and purchases already typed stay visible and can still be ticked on purpose.
            row["selectable"] = row["status"] in ("import", "duplicate")
            row["checked"] = row["status"] == "import" and not row.get("typed")
            row["choice"] = cards.default_choice(row, user_categories) if row["selectable"] else ""
            row["status_label"] = cards.STATUS_LABELS[row["status"]]
        context.update(
            preview=preview, rows=rows, account=account, summary=cards.summarise(rows, preview),
            options=cards.category_options(user, rows),
            payment_accounts=user.accounts.filter(archived=False).exclude(pk=preview["account_id"] or 0),
            already=CardInvoice.objects.filter(user=user, account=account, reference_date=preview["reference_date"]).first()
            if account else None,
        )
    return render(request, "app/cards.html", context)


@login_required
@require_POST
def card_cancel(request):
    request.session.pop(CARD_SESSION_KEY, None)
    return redirect("cards")


@login_required
@require_POST
def card_confirm(request):
    stored = request.session.get(CARD_SESSION_KEY)
    if not stored or not secrets.compare_digest(stored["token"], request.POST.get("token", "")):
        messages.error(request, "Essa importação expirou. Envie a fatura novamente.")
        return redirect("cards")
    preview = cards.from_session(stored)
    choices = {
        row["i"]: {"include": request.POST.get(f"inc_{row['i']}") == "on", "category": request.POST.get(f"cat_{row['i']}", ""),
                   "rule": request.POST.get(f"rule_{row['i']}") == "on",
                   "name": request.POST.get(f"name_{row['i']}", "")[:120],
                   "note": request.POST.get(f"note_{row['i']}", "")[:500],
                   "date": request.POST.get(f"date_{row['i']}", "")[:10],
                   "amount": request.POST.get(f"amount_{row['i']}", "")[:20]}
        for row in preview["rows"] if row["status"] == "import"
    }
    payment_account = None
    if request.POST.get("payment_account", "").isdigit():
        payment_account = request.user.accounts.filter(pk=request.POST["payment_account"]).first()
    try:
        invoice = cards.apply_import(request.user, preview, choices, payment_account)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("cards")
    request.session.pop(CARD_SESSION_KEY, None)
    text = f"{invoice.lines_imported} lançamento(s) da fatura importado(s) no cartão {invoice.account.name}."
    if invoice.plans_created:
        text += f" {invoice.plans_created} compra(s) parcelada(s) agendada(s) em Contas recorrentes."
    messages.success(request, text)
    return redirect(f"{reverse('transactions')}?account={invoice.account.pk}")


# --- first-run setup, digest, trash, people, backup ---------------------------

@login_required
@require_http_methods(["GET", "POST"])
def welcome(request):
    if request.method == "POST":
        if "skip" in request.POST:
            profile = services.get_profile(request.user)
            profile.setup_done = True
            profile.save(update_fields=["setup_done"])
            services.forget_profile(request.user)
            return redirect("dashboard")
        form = SetupForm(request.POST, user=request.user)
        if form.is_valid():
            form.apply()
            messages.success(request, "Tudo pronto. Adicione sua primeira transação quando quiser.")
            return redirect("dashboard")
    else:
        form = SetupForm(user=request.user)
    return render(request, "app/welcome.html", {"form": form})


@login_required
def digest(request):
    return render(request, "app/digest.html", {"d": insight_lib.weekly_digest(request.user)})


@login_required
def trash(request):
    return render(request, "app/trash.html", {"days": services.TRASH_DAYS})


@login_required
def people(request):
    return render(request, "app/people.html", {"kinds": LEDGER_KINDS, "today": timezone.localdate(),
                                               "default_account": services.default_account(request.user).pk})


@login_required
@require_GET
def backup_export(request):
    response = HttpResponse(backup_lib.dumps(request.user), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="finance-manager-backup-{timezone.localdate().isoformat()}.json"'
    response["Cache-Control"] = "no-store"
    return response


def _phone_connected():
    """App mode with an online account linked (see app/sync)."""
    if not settings.APP_MODE:
        return False
    from .sync import client as sync_client
    return sync_client.is_connected()


@login_required
@require_http_methods(["GET", "POST"])
def backup_page(request):
    context = {"problems": [], "summary": None, "phone_connected": _phone_connected()}
    if request.method == "POST" and context["phone_connected"] and "restore" in request.POST:
        # Restoring here would replace the online account's data through the outbox, item by item.
        context["problems"] = ["Com a conta online conectada, restaure o backup pelo site. Este celular recebe tudo "
                               "na próxima sincronização."]
        return render(request, "app/backup.html", context)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if upload is None:
            context["problems"] = ["Escolha um arquivo de backup."]
        else:
            try:
                parsed = backup_lib.parse(upload.read(backup_lib.MAX_BYTES + 1))
            except backup_lib.BackupError as exc:
                context["problems"] = exc.problems
            else:
                context["summary"] = backup_lib.summary(parsed)
                if "restore" in request.POST:
                    if request.POST.get("confirm") != "on":
                        context["problems"] = ["Marque a caixa para confirmar que isso substitui seus dados atuais."]
                    else:
                        backup_lib.restore(request.user, parsed)
                        messages.success(request, "Backup restaurado.")
                        return redirect("dashboard")
    return render(request, "app/backup.html", context)


@login_required
@require_POST
def lock_month(request):
    month = services.parse_month(request.POST.get("month"), default=None) if request.POST.get("month") else None
    if month is None or request.POST.get("month") != month.strftime("%Y-%m"):
        messages.error(request, "Escolha um mês para fechar.")
    elif request.POST.get("action") == "reopen":
        MonthLock.objects.filter(user=request.user, month=month).delete()
        messages.success(request, f"O mês de {month_label(month)} foi reaberto.")
    elif month > services.month_start(timezone.localdate()):
        messages.error(request, "Só é possível fechar o mês atual ou meses anteriores.")
    else:
        MonthLock.objects.get_or_create(user=request.user, month=month)
        messages.success(request, f"O mês de {month_label(month)} foi fechado. As transações dele estão protegidas contra alterações.")
    return redirect("settings")


def _confirmed_password(request):
    if settings.APP_MODE and not request.user.has_usable_password():
        return True    # Android app: one person, no password; typing APAGAR is the confirmation
    return request.user.check_password(request.POST.get("password", ""))


@login_required
@require_POST
def erase_data(request):
    if request.POST.get("confirm") != "APAGAR" or not _confirmed_password(request):
        messages.error(request, "Nada foi apagado. Digite APAGAR e informe sua senha para confirmar.")
        return redirect("settings")
    if _phone_connected():
        # Only this phone: it leaves the online account (whose data stays as it is).
        from .sync import client as sync_client
        sync_client.logout(keep_data=False)
        services.set_up_new_user(request.user)
        messages.success(request, "Os dados deste celular foram apagados e ele saiu da conta online. "
                                  "Os dados da conta continuam no site; para apagá-los, use Configurações no site.")
        return redirect("settings")
    with services.locked(request.user):
        backup_lib.erase(request.user)
        services.set_up_new_user(request.user)
    messages.success(request, "Todos os seus dados foram apagados. Sua conta de usuário continua aqui.")
    return redirect("settings")


@login_required
@require_POST
def delete_account(request):
    if request.POST.get("confirm") != request.user.username or not _confirmed_password(request):
        messages.error(request, "Sua conta não foi excluída. Digite seu nome de usuário e informe sua senha para confirmar.")
        return redirect("settings")
    user = request.user
    logout(request)
    user.delete()
    messages.success(request, "Sua conta e todos os dados dela foram excluídos.")
    return redirect("login")

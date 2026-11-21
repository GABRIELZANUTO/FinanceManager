"""Android app mode (APP_MODE=1): the whole system runs on the phone, for one person.

* No sign-up, login or password: the app creates the user the first time and signs in by itself.
* The server only listens on the phone itself (127.0.0.1), but other apps on the same phone can
  reach that address too. So every launch of the app makes a secret token (APP_TOKEN), and without
  the session it opens nothing is shown: not a screen, not a number.
* The data lives in the app's private folder (see expensemanager/localserver.py).
"""
import hmac
import os

from django.contrib.auth import get_user_model, login
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from . import services

LOCAL_USERNAME = "eu"
ENTRY_PATH = "/app/entrar/"
# Pages that only make sense with several users and passwords.
NOT_IN_THE_APP = ("/login/", "/logout/", "/register/", "/settings/password/", "/settings/delete-account/", "/admin/")
CLOSED = ("<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Finanças Pessoais</title>"
          "<p style='font-family:sans-serif;padding:24px'>Abra o aplicativo Finanças Pessoais para ver os seus dados.</p></html>")


def local_user():
    User = get_user_model()
    user = User.objects.filter(username=LOCAL_USERNAME).first()
    if user is None:
        user = User.objects.create_user(LOCAL_USERNAME, first_name="Você")
        user.set_unusable_password()
        user.save()
        services.set_up_new_user(user)
    return user


def enter(request):
    """The app opens this address with the launch token: sign in the only user and go on."""
    expected = os.environ.get("APP_TOKEN", "")
    if not expected or not hmac.compare_digest(request.GET.get("token", ""), expected):
        return HttpResponseForbidden(CLOSED)
    login(request, local_user(), backend="django.contrib.auth.backends.ModelBackend")
    target = request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = "/"
    return redirect(target)


class LocalAppMiddleware:
    """Nothing without the session opened by the app; no multi-user pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path == ENTRY_PATH or path.startswith("/static/"):
            return self.get_response(request)
        if not request.user.is_authenticated:
            return HttpResponseForbidden(CLOSED)
        if path.startswith(NOT_IN_THE_APP):
            return redirect("dashboard")
        return self.get_response(request)

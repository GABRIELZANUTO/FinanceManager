"""/api/v1/auth/: get a key with username and password, create an account, list and revoke keys.

The key routes (token, register) take no session and no CSRF token: the answer is a key in the
body, never a cookie. They share the login and sign-up throttles of the screens.
"""
import datetime
import json

from django.conf import settings
from django.contrib.auth import authenticate
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from . import services, tokens
from .api import ApiError, api, body, error_response, form_errors
from .forms import RegisterForm
from .models import ApiToken
from .ratelimit import client_ip
from .views import login_by_ip, login_by_user, register_by_ip

MAX_EXPIRY_DAYS = 3650


def _json_body(request):
    if request.content_type != "application/json" or len(request.body) > 8 * 1024:
        raise ApiError("Envie um objeto JSON pequeno (Content-Type: application/json).", 400)
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("O corpo da requisição não é um JSON válido.") from None
    if not isinstance(data, dict):
        raise ApiError("O corpo da requisição deve ser um objeto JSON.")
    return data


def _expiry(data):
    days = data.get("expires_days")
    if days in (None, ""):
        return None
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_EXPIRY_DAYS:
        raise ApiError(f"expires_days deve ser um número de 1 a {MAX_EXPIRY_DAYS}.", 422)
    return timezone.now() + datetime.timedelta(days=days)


def user_json(user):
    return {"id": user.pk, "username": user.username, "name": user.get_full_name() or user.username}


def _issue(user, data, status):
    token, key = tokens.create(user, str(data.get("device_name") or "Sem nome"), data.get("scopes") or ("read", "write"),
                               expires_at=_expiry(data))
    profile = services.get_profile(user)
    return JsonResponse({"ok": True, "token": key, "token_id": token.pk, "scopes": token.scopes.split(),
                         "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                         "user": user_json(user), "plan": profile.plan}, status=status)


def _public(view):
    """POST only, JSON errors, no session or CSRF."""
    @csrf_exempt
    @never_cache
    def wrapper(request):
        if request.method != "POST":
            response = error_response("Método não permitido.", 405)
            response["Allow"] = "POST"
            return response
        try:
            return view(request)
        except ApiError as exc:
            return error_response(exc.message, exc.status, exc.errors, exc.code)

    wrapper.__name__ = view.__name__
    return wrapper


@_public
def token(request):
    data = _json_body(request)
    username = str(data.get("username") or "").strip().lower()[:150]
    password = str(data.get("password") or "")
    ip = client_ip(request)
    if login_by_user.blocked(username, ip) or login_by_ip.blocked(ip):
        return error_response("Muitas tentativas. Aguarde alguns minutos e tente de novo.", 429)
    user = authenticate(request, username=username, password=password) if username and password else None
    if user is None:
        login_by_user.hit(username, ip)
        login_by_ip.hit(ip)
        return error_response("Usuário ou senha incorretos.", 401)
    login_by_user.reset(username, ip)
    return _issue(user, data, 201)


@_public
def register(request):
    if not settings.ALLOW_REGISTRATION:
        return error_response("Novos cadastros estão fechados.", 404)
    ip = client_ip(request)
    if register_by_ip.blocked(ip):
        return error_response("Muitos cadastros a partir deste endereço. Tente novamente mais tarde.", 429)
    data = _json_body(request)
    password = str(data.get("password") or "")
    form = RegisterForm({"full_name": data.get("full_name") or "", "username": data.get("username") or "",
                         "email": data.get("email") or "", "password1": password, "password2": password})
    if not form.is_valid():
        return error_response("Corrija os campos destacados.", 422, form_errors(form))
    user = form.save()
    register_by_ip.hit(ip)
    return _issue(user, data, 201)


@api("GET")
def me(request):
    current = getattr(request, "api_token", None)
    return JsonResponse({"ok": True, "user": user_json(request.user), "plan": services.get_profile(request.user).plan,
                         "token": tokens.token_json(current) if current else None})


@api("POST")
def revoke(request):
    current = getattr(request, "api_token", None)
    if current is None:
        raise ApiError("Esta rota revoga a chave usada na requisição.", 400)
    tokens.revoke(current)
    return JsonResponse({"ok": True})


@api("GET", "POST")
def token_list(request):
    """GET: the person's keys. POST (screens only, with session): create one, shown once."""
    if request.method == "POST":
        if getattr(request, "api_token", None) is not None:
            raise ApiError("Crie chaves pela tela de Configurações ou por /api/v1/auth/token.", 403, code="forbidden")
        data = body(request)
        scopes = data.getlist("scopes") if hasattr(data, "getlist") else data.get("scopes")
        return _issue(request.user, {"device_name": data.get("name"), "scopes": scopes,
                                     "expires_days": _days(data.get("expires_days"))}, 201)
    rows = ApiToken.objects.filter(user=request.user, revoked_at__isnull=True)
    return JsonResponse({"ok": True, "tokens": [tokens.token_json(t) for t in rows]})


def _days(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError("Validade inválida.", 422) from None


@api("DELETE")
def token_detail(request, pk):
    found = ApiToken.objects.filter(user=request.user, pk=pk).first()
    if found is None:
        raise ApiError("Não encontrado.", 404)
    tokens.revoke(found)
    return JsonResponse({"ok": True})


def _password_ok(request, data):
    password = str(data.get("password") or "")
    return bool(password) and request.user.check_password(password)


@api("POST")
def me_wipe(request):
    """Erase all of this account's data (the account stays). Asks for the password."""
    from . import backup
    data = body(request)
    if not _password_ok(request, data):
        raise ApiError("Senha incorreta. Nada foi apagado.", 403, code="auth")
    with services.locked(request.user):
        backup.erase(request.user)
    return JsonResponse({"ok": True})


@api("DELETE")
def me_delete(request):
    """Delete the online account and everything in it (Google Play asks apps that create accounts
    to offer this). Asks for the password."""
    data = body(request) if request.body else {}
    if not _password_ok(request, data):
        raise ApiError("Senha incorreta. A conta não foi excluída.", 403, code="auth")
    request.user.delete()
    return JsonResponse({"ok": True})

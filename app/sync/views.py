"""/api/v1/sync/: thin views over app/sync/server.py (contract in the plan, Alteração 2)."""
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.gzip import gzip_page

from app.api import ApiError, api, body
from app.models import ApiToken, Profile

from . import server

PROTOCOL = 1          # X-Sync-Version this server speaks
MIN_PROTOCOL = 1      # older phones get 426 "Atualize o aplicativo"
PUSH_MAX_BODY = 2 * 1024 * 1024


def _checks(request):
    raw = request.META.get("HTTP_X_SYNC_VERSION", str(PROTOCOL))
    try:
        version = int(raw)
    except ValueError:
        version = 0
    if version < MIN_PROTOCOL:
        raise ApiError("Atualize o aplicativo para sincronizar.", 426, code="upgrade_required")
    profile = Profile.objects.get(user=request.user)
    if settings.SYNC_REQUIRE_PLAN and profile.plan != "sync":
        raise ApiError("A sincronização não está disponível no seu plano.", 402, code="plan_required")
    token = getattr(request, "api_token", None)
    if token is not None:
        ApiToken.objects.filter(pk=token.pk).update(last_sync_at=timezone.now())
    return profile


def _int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


@gzip_page
@api("POST", scope="sync", max_body=PUSH_MAX_BODY)
def push(request):
    profile = _checks(request)
    data = body(request)
    ops = data.get("ops")
    if not isinstance(ops, list) or len(ops) > server.MAX_OPS:
        raise ApiError(f"Envie de 1 a {server.MAX_OPS} operações em ops.", 400)
    epoch = data.get("epoch")
    if epoch is not None and epoch != profile.sync_epoch:
        raise ApiError("Os dados da conta foram restaurados de um backup: baixe tudo de novo.", 409, code="epoch_changed")
    return JsonResponse({"ok": True, "results": server.push(request.user, ops), "epoch": profile.sync_epoch})


@gzip_page
@api("GET", scope="sync")
def pull(request):
    profile = _checks(request)
    cursor = _int(request.GET.get("cursor"), 0, 0, 2 ** 62)
    limit = _int(request.GET.get("limit"), 500, 1, 500)
    return JsonResponse({"ok": True, "epoch": profile.sync_epoch, **server.pull(request.user, cursor, limit)})


@gzip_page
@api("GET", scope="sync")
def snapshot(request):
    _checks(request)
    name = request.GET.get("model")
    if not name:
        return JsonResponse({"ok": True, **server.snapshot_start(request.user)})
    try:
        page = server.snapshot_page(request.user, name, _int(request.GET.get("after"), 0, 0, 2 ** 62),
                                    _int(request.GET.get("limit"), 1000, 1, 1000))
    except KeyError:
        raise ApiError("Tipo desconhecido.", 404) from None
    return JsonResponse({"ok": True, **page})


@api("GET", scope="sync")
def one(request):
    _checks(request)
    try:
        found = server.one(request.user, request.GET.get("model", ""), request.GET.get("uid", ""))
    except (KeyError, ValueError):
        found = None
    if found is None:
        raise ApiError("Este registro não existe mais.", 404, code="gone")
    return JsonResponse({"ok": True, "item": found})

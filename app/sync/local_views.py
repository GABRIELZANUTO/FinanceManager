"""Screens and routes of the synchronisation on the phone (app mode only): first run, the
Sincronização page and the small JSON routes its script calls."""
from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required

from app.api import ApiError, api, body
from app.models import SyncConflict, SyncOutbox, SyncState

from . import client
from . import spec as specs

MODEL_LABELS = {
    "transaction": "Transação", "category": "Categoria", "account": "Conta", "tag": "Tag", "person": "Pessoa",
    "savedview": "Filtro salvo", "budget": "Orçamento", "goal": "Meta", "contribution": "Aporte",
    "recurringtransaction": "Recorrência", "recurringmatch": "Confirmação de recorrência", "ledgerentry": "Lançamento de dívida",
    "transfer": "Transferência", "cardinvoice": "Fatura de cartão", "monthlock": "Mês fechado", "profile": "Configurações",
    "categoryrule": "Regra de categoria",
}
REASONS = {
    "same_field": "Alterado no site e neste celular", "changed_on_server": "Alterado no site depois que você apagou aqui",
    "deleted_on_server": "Apagado no site", "duplicate": "Já existia no site com outros valores", "month_locked": "Mês fechado",
    "invalid": "Dados inválidos", "missing_ref": "Depende de um registro que não está no servidor", "uid_taken": "Identificador em uso",
    "restored_on_server": "Dados da conta restaurados de um backup", "in_use": "Ainda é usado por outros registros",
}


def _app_only():
    if not settings.APP_MODE:
        raise Http404


def describe(model, data):
    """ "Mercado, R$ 50,00" from an object's data (for the lists)."""
    if not data:
        return ""
    parts = [str(data.get(k)) for k in ("name", "description", "pattern") if data.get(k)]
    if data.get("amount"):
        parts.append(str(data["amount"]).replace(".", ","))
    if data.get("month"):
        parts.append(str(data["month"])[:7])
    return ", ".join(parts[:2])


@login_required
def start_page(request):
    """First run on the phone: only on this phone, or with an online account."""
    _app_only()
    return render(request, "app/sync_start.html", {"server": settings.SYNC_SERVER_URL,
                                                   "has_data": client.local_has_data(request.user)})


@login_required
def sync_page(request):
    _app_only()
    return render(request, "app/sync.html", {"nav": "sync", "server": settings.SYNC_SERVER_URL,
                                             "has_data": client.local_has_data(request.user)})


@api("GET")
def status(request):
    _app_only()
    return JsonResponse({"ok": True, **client.status()})


@api("POST")
def run(request):
    _app_only()
    client.request_sync("button")
    return JsonResponse({"ok": True, **client.status()})


@api("POST")
def local_only(request):
    _app_only()
    state = SyncState.get()
    if state.mode != "account":
        SyncState.objects.filter(pk=1).update(mode="local")
    return JsonResponse({"ok": True})


@api("POST")
def login(request):
    """Credentials -> key. Decides what happens to the data on each side; when both have
    transactions, answers needs_choice and the screen asks (the key is revoked meanwhile)."""
    _app_only()
    data = body(request)
    create = bool(data.get("create"))
    try:
        config, start = client.login(data.get("server") or settings.SYNC_SERVER_URL, str(data.get("username") or "").strip(),
                                     str(data.get("password") or ""), create=create,
                                     full_name=str(data.get("full_name") or ""), email=str(data.get("email") or ""))
    except ValueError as exc:
        raise ApiError(str(exc), 422, {"server": [str(exc)]}) from None
    except client.Offline:
        raise ApiError("Não foi possível falar com o servidor. Confira a internet e o endereço.", 503) from None
    except client.ServerSaid as exc:
        raise ApiError(exc.data.get("message") or "O servidor recusou.", exc.status if exc.status < 500 else 400,
                       exc.data.get("errors") or {}) from None
    phone_has = client.local_has_data(request.user)
    server_has = (start.get("counts") or {}).get("transaction", 0) > 0
    choice = data.get("choice")
    if choice not in ("send", "download"):
        if not phone_has:
            choice = "download"
        elif not server_has:
            choice = "send"
        else:
            try:
                client.http("POST", "/api/v1/auth/revoke/", {}, config=config)
            except (client.Offline, client.ServerSaid):
                pass
            return JsonResponse({"ok": True, "needs_choice": True,
                                 "server_count": start["counts"].get("transaction", 0)})
    client.start(config, start, choice)
    return JsonResponse({"ok": True, "choice": choice, **client.status()})


@api("POST")
def logout(request):
    _app_only()
    data = body(request)
    client.logout(keep_data=data.get("keep", True) is not False)
    return JsonResponse({"ok": True, **client.status()})


@api("GET")
def details(request):
    """Pendentes, Conflitos e erros, Histórico."""
    _app_only()
    user = client.local_user()
    uids = specs.UidMap(user)
    pending = []
    for row in SyncOutbox.objects.filter(status__in=("pending", "sent", "blocked")).order_by("id")[:200]:
        obj = client._local(row.model, row.uid, user) if row.action == "upsert" else None
        text = describe(row.model, specs.serialize(obj, uids)) if obj is not None else describe(row.model, row.base)
        pending.append({"model": MODEL_LABELS.get(row.model, row.model), "text": text, "action": row.action,
                        "created": row.base_version is None, "blocked": row.status == "blocked"})
    conflicts = []
    for c in SyncConflict.objects.filter(resolved_at__isnull=True)[:200]:
        local, server = c.local_data or {}, c.server_data or {}
        keys = [k for k in dict.fromkeys(list(local) + list(server)) if local.get(k) != server.get(k)]
        conflicts.append({
            "id": c.pk, "model": c.model, "model_label": MODEL_LABELS.get(c.model, c.model), "reason": c.reason,
            "reason_label": REASONS.get(c.reason, c.reason), "message": c.message, "text": describe(c.model, local or server),
            "fields": keys, "local": local, "server": server, "server_deleted": c.server_deleted,
            "updated_by": c.server_updated_by, "updated_at": c.server_updated_at.isoformat() if c.server_updated_at else None,
            "can_mine": bool(local), "can_retry": c.reason in ("month_locked", "missing_ref", "invalid", "in_use"),
        })
    return JsonResponse({"ok": True, "pending": pending, "conflicts": conflicts, "history": SyncState.get().history or [],
                         "status": client.status()})


@api("POST")
def resolve(request, pk):
    _app_only()
    conflict = SyncConflict.objects.filter(pk=pk, resolved_at__isnull=True).first()
    if conflict is None:
        raise ApiError("Este conflito já foi resolvido.", 404)
    resolution = body(request).get("resolution")
    if resolution not in client.RESOLUTIONS:
        raise ApiError("Escolha o que fazer.", 422)
    local = conflict.local_data
    client.resolve(conflict, resolution)
    return JsonResponse({"ok": True, "model": conflict.model, "local": local if resolution == "manual" else None})


def first_run_redirect(request):
    """The phone's first opening (no choice made, no data yet) goes to the start page."""
    if not settings.APP_MODE:
        return None
    state = SyncState.get()
    if state.mode == "" and not client.local_has_data(request.user):
        return redirect("sync_start")
    return None

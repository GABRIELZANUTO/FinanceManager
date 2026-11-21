"""Security headers and a friendly page for an unmigrated database."""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
# The bundled Django admin still uses a few inline style attributes.
ADMIN_POLICY = POLICY.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            admin = request.path.startswith("/admin/")
            response["Content-Security-Policy"] = ADMIN_POLICY if admin else POLICY
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.DEBUG and request.path.startswith("/" + settings.STATIC_URL.lstrip("/")):
            # Development server: always check for a newer file (scripts import each other by plain
            # names, and a cached old copy would mix versions after an edit).
            response["Cache-Control"] = "no-cache"
        return response


SETUP_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Banco de dados não configurado - Finanças Pessoais</title>
<meta name="robots" content="noindex">
<link rel="stylesheet" href="{css}">
</head><body>
<main class="auth"><div class="auth-card"><div class="card">
<h1>Banco de dados não configurado</h1>
<p>Faltam no banco de dados tabelas de que o app precisa. Pare o servidor, rode este comando na pasta do projeto
e inicie o servidor de novo:</p>
<p><code>python manage.py migrate</code></p>
<p class="muted small">Se você acabou de baixar o projeto, copie também <code>.env.example</code> para <code>.env</code>.
Veja a seção Troubleshooting do README.</p>
</div></div></main></body></html>
"""


class DatabaseReadyMiddleware:
    """Show a plain explanation instead of a stack trace when migrations have not been applied.

    The result is cached once the database is ready, so the normal cost is one attribute read.
    """

    ready = False

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def check_ready(cls):
        from django.db import DatabaseError, connection
        from django.db.migrations.executor import MigrationExecutor

        try:
            executor = MigrationExecutor(connection)
            return not executor.migration_plan(executor.loader.graph.leaf_nodes())
        except DatabaseError:
            return False

    def __call__(self, request):
        cls = type(self)
        if not cls.ready:
            cls.ready = self.check_ready()
            if not cls.ready:
                logger.error("Database not set up: unapplied migrations. Run: python manage.py migrate")
                from django.http import HttpResponse
                from django.templatetags.static import static
                try:
                    css = static("app/css/app.css")
                except ValueError:
                    css = ""
                response = HttpResponse(SETUP_PAGE.format(css=css), status=503, content_type="text/html; charset=utf-8")
                response["Retry-After"] = "60"
                response["Cache-Control"] = "no-store"
                return response
        return self.get_response(request)


def describe_agent(agent):
    """A short name for who is writing, from the User-Agent: "Chrome no Windows", "Firefox no Android"."""
    agent = agent or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                           ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in agent), "")
    system = next((name for key, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                          ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if key in agent), "")
    if browser and system:
        return f"{browser} no {system}"
    return browser or system or "Site"


class ActorMiddleware:
    """Record who is writing (shown as "Alterado por ..."): the phone's name in app mode,
    otherwise the browser and system from the User-Agent."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from app.sync.capture import acting_as

        token = getattr(request, "api_token", None)
        if token is not None:
            name = token.name
        elif settings.APP_MODE:
            name = settings.DEVICE_NAME or "Este celular"
        else:
            name = describe_agent(request.META.get("HTTP_USER_AGENT"))
        with acting_as(name):
            return self.get_response(request)


class SyncLockMiddleware:
    """Phone app: a write from the screens waits while a synchronisation is running (and a
    synchronisation waits for a write in progress), so the two never mix. The screens also show
    a "Sincronizando" overlay that blocks touching anything meanwhile."""

    FREE = ("/api/sync/", "/app/entrar/", "/static/")
    WAIT = 120     # seconds a write waits for a synchronisation to finish

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("GET", "HEAD", "OPTIONS") or request.path.startswith(self.FREE):
            return self.get_response(request)
        from app.sync import client
        if not client._lock.acquire(timeout=self.WAIT):
            return _json_error("Sincronizando os dados. Tente de novo em instantes.", 503, "syncing")
        try:
            return self.get_response(request)
        finally:
            client._lock.release()


def _json_error(message, status, code):
    from django.http import JsonResponse

    return JsonResponse({"ok": False, "message": message, "errors": {}, "code": code}, status=status)


class ApiTokenMiddleware:
    """/api/v1/ with "Authorization: Bearer fp_...": the request acts as the token's owner, with no
    session cookie and no CSRF token (the CSRF check runs in process_view, after this). Without the
    header nothing changes. Comes right after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not request.path.startswith("/api/v1/") or not header.startswith("Bearer "):
            return self.get_response(request)
        if not (settings.DEBUG or settings.APP_MODE or request.is_secure()):
            return _json_error("Use HTTPS para acessar a API.", 403, "insecure")
        from app import tokens
        from app.ratelimit import Throttle

        token = tokens.authenticate(header[7:].strip())
        if token is None:
            return _json_error("Chave de acesso inválida, vencida ou revogada.", 401, "auth")
        throttle = Throttle("api-token", settings.API_TOKEN_RATE, settings.API_TOKEN_WINDOW)
        if throttle.hit(token.pk) > settings.API_TOKEN_RATE:
            response = _json_error("Muitas requisições. Tente de novo em instantes.", 429, "rate_limited")
            response["Retry-After"] = str(settings.API_TOKEN_WINDOW)
            return response
        tokens.touch(token)
        request.user = token.user
        request.api_token = token
        request._dont_enforce_csrf_checks = True
        return self.get_response(request)

"""Django settings for the finance manager.

Everything that differs between machines is read from environment variables
(optionally from a ``.env`` file in the project root, see ``.env.example``).
Defaults are production-safe: DEBUG is off, cookies are secure and the secret
key must be provided.
"""
import os
import sys
from pathlib import Path

import django

from django.core.exceptions import ImproperlyConfigured

from .env import env_bool, env_int, env_list, load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
# .env is this machine's configuration and never leaves it. The server gets .env.producao
# instead (see scripts/montar_discloud.py); it is read only when there is no .env.
load_dotenv(BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else BASE_DIR / ".env.producao")

DEBUG = env_bool("DEBUG", False)
# Android app: the whole system runs on the phone for one person (see expensemanager/localserver.py).
APP_MODE = env_bool("APP_MODE", False)
# The phone's name in app mode (shown as "Alterado por ..."); set by localserver.start.
DEVICE_NAME = os.environ.get("DEVICE_NAME", "")[:60]
# Synchronisation: the site is the "server" (source of truth); the phone app is a "client" that
# keeps an outbox of local changes and downloads what changed on the server.
SYNC_ROLE = "client" if APP_MODE else "server"
# The plan in Profile.plan is checked only when this is on (billing still to be decided).
SYNC_REQUIRE_PLAN = env_bool("SYNC_REQUIRE_PLAN", False)
# Default server for "Entrar na minha conta online" in the phone app.
# The phone app has no .env: its default comes from expensemanager/local_settings.py (not in git,
# see local_settings.example.py), copied into the app by scripts/montar_android.py.
try:
    from .local_settings import SYNC_SERVER_URL as _LOCAL_SYNC_SERVER_URL
except ImportError:
    _LOCAL_SYNC_SERVER_URL = ""
SYNC_SERVER_URL = (os.environ.get("SYNC_SERVER_URL") or _LOCAL_SYNC_SERVER_URL).rstrip("/")
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "django-insecure-development-only-key-not-for-production"
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY is not set. Copy .env.example to .env and set it "
            "(see README), or export SECRET_KEY in the environment."
        )

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [])

# Application behaviour
ALLOW_NEGATIVE_BALANCE = env_bool("ALLOW_NEGATIVE_BALANCE", True)
ALLOW_REGISTRATION = env_bool("ALLOW_REGISTRATION", True)
DEFAULT_CURRENCY_CODE = os.environ.get("DEFAULT_CURRENCY_CODE", "BRL")
DEFAULT_CURRENCY_SYMBOL = os.environ.get("DEFAULT_CURRENCY_SYMBOL", "R$")
# intl = 1,234.56 | indian = 12,34,567.89 | eu = 1.234,56 (Brasil) | space = 1 234,56
DEFAULT_NUMBER_FORMAT = os.environ.get("DEFAULT_NUMBER_FORMAT", "eu")
if DEFAULT_NUMBER_FORMAT not in ("intl", "indian", "eu", "space"):
    raise ImproperlyConfigured("DEFAULT_NUMBER_FORMAT must be one of: intl, indian, eu, space.")
LOGIN_MAX_ATTEMPTS = env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 15 * 60)
# /api/v1/ with a key: requests per key per window (seconds). The cache is per process: fine with one
# gunicorn worker; with more workers use a shared cache.
API_TOKEN_RATE = env_int("API_TOKEN_RATE", 600)
API_TOKEN_WINDOW = env_int("API_TOKEN_WINDOW", 60)
REGISTER_MAX_PER_HOUR = env_int("REGISTER_MAX_PER_HOUR", 10)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "app",
]

MIDDLEWARE = [
    "expensemanager.middleware.DatabaseReadyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "expensemanager.middleware.ApiTokenMiddleware",
    "expensemanager.middleware.ActorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "expensemanager.middleware.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "expensemanager.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "builtins": ["app.templatetags.icons"],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "app.context_processors.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "expensemanager.wsgi.application"
ASGI_APPLICATION = "expensemanager.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH") or BASE_DIR / "db.sqlite3",
        # Wait for the lock instead of failing at once ("database is locked").
        "OPTIONS": {"timeout": 20},
    }
}
if django.VERSION >= (5, 1):
    # Take the write lock when a transaction starts (no deadlock when a read turns into a write)
    # and let readers work while someone writes (WAL).
    DATABASES["default"]["OPTIONS"].update({
        "transaction_mode": "IMMEDIATE",
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
    })

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

if TESTING:
    # Fast hashing keeps the test suite quick; never used outside `manage.py test`.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "pt-br")
TIME_ZONE = os.environ.get("TIME_ZONE", "America/Sao_Paulo")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Production (gunicorn, no web server in front for static files): WhiteNoise serves them.
try:
    import whitenoise  # noqa: F401
except ImportError:
    pass
else:
    MIDDLEWARE.insert(MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
                      "whitenoise.middleware.WhiteNoiseMiddleware")
    # STATIC_MANIFEST=1 (the server, see .env.producao): every file name carries a hash of its
    # content, the imports between scripts included, so a new version is never mixed with an old
    # copy kept by the browser. Off in development and tests, which have no collected files.
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "expensemanager.storage.HashedStaticFiles" if env_bool("STATIC_MANIFEST", False)
                        else "whitenoise.storage.CompressedStaticFilesStorage"},
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Sessions and cookies
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Transport security
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not (DEBUG or TESTING))
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if APP_MODE:
    # Served by the app itself on 127.0.0.1, shown by its WebView: no HTTPS, one user, no sign-up.
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = False
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_PRELOAD = False
    SESSION_COOKIE_AGE = 60 * 60 * 24 * 365
    ALLOW_REGISTRATION = False
    MIDDLEWARE.insert(MIDDLEWARE.index("django.contrib.auth.middleware.AuthenticationMiddleware") + 1,
                      "app.localapp.LocalAppMiddleware")
    MIDDLEWARE.append("expensemanager.middleware.SyncLockMiddleware")
if env_bool("TRUST_X_FORWARDED_PROTO", False):
    # Only enable behind a reverse proxy that always sets this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Upload limits (CSV import)
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

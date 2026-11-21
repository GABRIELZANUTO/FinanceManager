"""/api/v1/: the same views as /api/, for the phone and other programs (key in the
Authorization header). Screen-only helpers stay out. A breaking change to these answers needs
/api/v2/ (the contract test freezes the keys of the read routes)."""
from django.urls import path

from . import api_auth, api_docs
from .sync import views as sync_views
from . import urls as screen_urls

SCREEN_ONLY = {"api_onboarding", "api_sample_data", "api_suggest", "api_tokens", "api_token", "api_sync_status", "api_sync_run",
               "api_sync_local", "api_sync_login", "api_sync_logout", "api_sync_details", "api_sync_resolve"}

urlpatterns = [
    path("auth/token/", api_auth.token, name="v1_auth_token"),
    path("auth/register/", api_auth.register, name="v1_auth_register"),
    path("auth/me/", api_auth.me, name="v1_auth_me"),
    path("me/wipe/", api_auth.me_wipe, name="v1_me_wipe"),
    path("me/", api_auth.me_delete, name="v1_me_delete"),
    path("auth/revoke/", api_auth.revoke, name="v1_auth_revoke"),
    path("auth/tokens/", api_auth.token_list, name="v1_auth_tokens"),
    path("auth/tokens/<int:pk>/", api_auth.token_detail, name="v1_auth_token_detail"),
    path("sync/push/", sync_views.push, name="v1_sync_push"),
    path("sync/pull/", sync_views.pull, name="v1_sync_pull"),
    path("sync/snapshot/", sync_views.snapshot, name="v1_sync_snapshot"),
    path("sync/object/", sync_views.one, name="v1_sync_object"),
    path("openapi.json", api_docs.openapi, name="v1_openapi"),
    path("docs/", api_docs.docs, name="v1_docs"),
]

for pattern in screen_urls.urlpatterns:
    route = str(pattern.pattern)
    if route.startswith("api/") and pattern.name not in SCREEN_ONLY:
        urlpatterns.append(path(route[len("api/"):], pattern.callback, name="v1_" + pattern.name[len("api_"):]))

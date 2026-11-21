from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("app.urls_api_v1")),
    path("", include("app.urls")),
]

if settings.APP_MODE:
    # Android app: the launch entry, and the screens' files served straight from the app's folder.
    from app import localapp

    urlpatterns = [
        path("app/entrar/", localapp.enter, name="app_enter"),
        re_path(r"^static/(?P<path>.+)$", serve, {"document_root": settings.BASE_DIR / "app" / "static"}),
    ] + urlpatterns

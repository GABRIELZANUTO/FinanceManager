from pathlib import Path

from django.conf import settings
from django.templatetags.static import static

from . import services

STATIC_APP = Path(__file__).resolve().parent / "static" / "app"


def asset_version():
    """Changes whenever a stylesheet or script changes, so browsers fetch the new file."""
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_APP.rglob("*") if p.suffix in (".js", ".css"))))
    except (ValueError, OSError):
        return "1"


def shell(request):
    """Values every signed-in page needs: currency settings, category and account choices."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or request.path.startswith("/admin/"):
        return {"asset_version": asset_version()}
    profile = services.get_profile(user)
    categories = list(user.categories.select_related("parent"))
    return {
        "profile": profile,
        "shell_categories": [
            {"id": c.id, "name": c.name, "full_name": c.full_name, "kind": c.kind, "color": c.color, "icon": c.icon,
             "parent": c.parent_id} for c in sorted(categories, key=lambda c: c.full_name.lower())
        ],
        "shell_accounts": list(user.accounts.filter(archived=False).values("id", "name", "type")),
        "shell_default_account": services.default_account(user).pk,
        "money_config": {
            "symbol": profile.currency_symbol,
            "code": profile.currency_code,
            "format": profile.number_format,
        },
        "sprite_url": static("app/icons/sprite.svg"),
        "nav": getattr(request.resolver_match, "url_name", ""),
        "asset_version": asset_version(),
        "app_mode": settings.APP_MODE,
    }

"""API keys for /api/v1/: creation, lookup and revocation.

Format shown to the person once: fp_<prefix>_<secret>. The prefix (12 hex characters) finds the
row; the secret (32 random bytes) is compared through its SHA-256. The secret is never stored
nor logged.
"""
import hashlib
import hmac
import secrets

from django.utils import timezone

from .models import API_SCOPES, ApiToken

VALID_SCOPES = tuple(code for code, _ in API_SCOPES)


def _hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_scopes(scopes):
    """Known scopes, in a fixed order; "read" always included."""
    if isinstance(scopes, str):
        scopes = scopes.replace(",", " ").split()
    wanted = {str(s).strip().lower() for s in (scopes or [])} | {"read"}
    return " ".join(code for code in VALID_SCOPES if code in wanted)


def create(user, name, scopes=("read",), expires_at=None):
    """Returns (token, full key). The key is shown once and cannot be recovered."""
    while True:
        prefix = secrets.token_hex(6)
        if not ApiToken.objects.filter(prefix=prefix).exists():
            break
    secret = secrets.token_urlsafe(32)
    token = ApiToken.objects.create(user=user, name=(name or "Sem nome").strip()[:60] or "Sem nome", prefix=prefix,
                                    key_hash=_hash(secret), scopes=clean_scopes(scopes), expires_at=expires_at)
    return token, f"fp_{prefix}_{secret}"


def authenticate(key):
    """The active token for this key, or None."""
    if not key or not key.startswith("fp_"):
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    token = ApiToken.objects.select_related("user").filter(prefix=parts[1]).first()
    if token is None or not hmac.compare_digest(token.key_hash, _hash(parts[2])):
        return None
    if not token.active or not token.user.is_active:
        return None
    return token


def touch(token):
    """Record the last use, at most once a minute (no write on every request)."""
    now = timezone.now()
    if token.last_used_at is None or (now - token.last_used_at).total_seconds() > 60:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now


def revoke(token):
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])


def token_json(token):
    return {"id": token.pk, "name": token.name, "prefix": token.prefix, "scopes": token.scopes.split(),
            "created_at": token.created_at.isoformat(),
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "last_sync_at": token.last_sync_at.isoformat() if token.last_sync_at else None,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "revoked": token.revoked_at is not None, "active": token.active}

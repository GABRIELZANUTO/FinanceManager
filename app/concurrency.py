"""Edit conflicts (Alteração 3): a write names the version it was based on; if the record changed
in between (another tab, the site, the phone's sync) it is refused with 409 instead of silently
overwriting what the other one saved.

The version comes from the JSON body ("version"), the query string (?version=, for DELETE) or the
If-Match header. Without a version the write goes through as before (old clients); "force": true
overwrites on purpose ("Manter as minhas").
"""


class StaleRecord(Exception):
    """The record changed since the screen loaded it. ``current`` is what is saved now."""

    def __init__(self, obj, current):
        super().__init__("stale")
        self.obj = obj
        self.current = current


def _parse(raw):
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip('"')
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _truthy(value):
    return value is True or str(value).lower() in ("1", "true", "yes", "sim")


def requested(request, data=None):
    """(expected version or None, force) for this write."""
    data = data if data is not None else {}
    getter = getattr(data, "get", None)
    raw = getter("version") if getter else None
    if raw in (None, ""):
        raw = request.GET.get("version")
    if raw in (None, ""):
        raw = request.META.get("HTTP_IF_MATCH")
    force = (getter and _truthy(getter("force"))) or _truthy(request.GET.get("force"))
    return _parse(raw), bool(force)


def current_version(obj):
    """The version saved now, read with the row locked until the end of the transaction
    (Postgres; SQLite already runs one writer at a time). None if the row is gone."""
    return (type(obj)._base_manager.select_for_update().filter(pk=obj.pk)
            .values_list("version", flat=True).first())


def check(request, obj, data=None, current=None):
    """Raise StaleRecord if the write was based on another version. ``current`` builds the JSON of
    the record as saved now (called only on conflict)."""
    expected, force = requested(request, data)
    if expected is None or force:
        return
    saved = current_version(obj)
    if saved is None:
        from .api import ApiError
        raise ApiError("Este registro foi apagado em outro lugar.", 404, code="gone")
    if saved != expected:
        fresh = type(obj)._base_manager.get(pk=obj.pk)
        raise StaleRecord(fresh, current() if current else None)

"""/api/v1/openapi.json and /api/v1/docs/: the API description, served from the app's own files
(no CDN, within the CSP)."""
import json
from functools import lru_cache
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

SPEC = Path(__file__).resolve().parent / "static" / "app" / "api" / "openapi.json"


@lru_cache(maxsize=1)
def load():
    return json.loads(SPEC.read_text(encoding="utf-8"))


@require_GET
def openapi(request):
    return JsonResponse(load(), json_dumps_params={"ensure_ascii": False})


@require_GET
def docs(request):
    spec = load()
    groups = []
    for path, methods in spec.get("paths", {}).items():
        groups.append({"path": path, "ops": [{"method": m.upper(), "summary": op.get("summary", ""),
                                             "public": op.get("security") == []}
                                            for m, op in methods.items()]})
    response = render(request, "app/api_docs.html", {"info": spec.get("info", {}), "groups": groups})
    return response if isinstance(response, HttpResponse) else HttpResponse(response)

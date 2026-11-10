"""The fetch-data tool — one callable surface for every endpoint in the catalog.

The Ollama brain is given exactly one tool (``fetch_oneness_data``). When the
model asks for data, :meth:`ToolBox.execute` resolves the catalog name, fills the
path/query/body from the model's arguments, performs a **real HTTP request**
against the running API and returns the request, the response envelope, the HTTP
status and the latency. Nothing is mocked: the model sees what the node said.

    from core.tools import ToolBox
    tb = ToolBox("http://127.0.0.1:8090", admin_token="…")
    tb.execute("chain.info")
    tb.execute("account.get", {"address": "TOR1…onion"})
    tb.execute("wallets.send", {"name": "founder", "to": "TOR1…", "amount": 1})
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core import api_catalog
from core.config import settings
from core.errors import ValidationError

TOOL_NAME = "fetch_oneness_data"
MAX_TOOL_CHARS = 6000


def _coerce(value: Any, type_name: str) -> Any:
    """Convert a model-supplied value into the type the route expects."""
    if value is None:
        return None
    if type_name == "int":
        return int(str(value).strip())
    if type_name == "float":
        return float(str(value).strip())
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if type_name == "object":
        if isinstance(value, (dict, list)):
            return value
        return json.loads(str(value))
    return value if isinstance(value, str) else str(value)


class ToolBox:
    """Executes catalog endpoints over real HTTP."""

    def __init__(self, base_url: str | None = None, *,
                 display_base: str | None = None,
                 admin_token: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or os.environ.get("ONENESS_INTERNAL_BASE_URL")
                         or settings.get("paths.public_base_url")).rstrip("/")
        # The call itself must go to loopback (an .onion host is unresolvable
        # from inside the node), but the *catalog* has to advertise the URL the
        # browser actually used — otherwise /api/tools and the docs page claim
        # "base https://…" and then print a 127.0.0.1 request, which reads like a
        # broken deployment instead of a loopback round-trip.
        self.display_base = str(display_base or "").rstrip("/") or self.base_url
        self.admin_token = admin_token
        self.timeout = float(timeout)

    @property
    def spec(self) -> dict:
        return api_catalog.tool_spec()

    def describe(self) -> dict:
        return {"tool": TOOL_NAME, "description": self.spec["function"]["description"],
                "base_url": self.display_base, "call_base": self.base_url,
                "endpoints": api_catalog.endpoints(),
                "count": len(api_catalog.ALL)}

    def _echo(self, entry: dict, url: str, body: dict) -> dict:
        """The request as it was sent, plus the URL a visitor recognises."""
        echo = {"method": entry["method"], "url": url, "body": body or None}
        if self.display_base != self.base_url and url.startswith(self.base_url):
            echo["public_url"] = self.display_base + url[len(self.base_url):]
        return echo

    def _build(self, entry: dict, params: dict) -> tuple[str, dict, dict]:
        path, query, body = entry["path"], {}, {}
        known = {param["name"] for param in entry["params"]}
        for param in entry["params"]:
            raw = params.get(param["name"])
            if raw is None or raw == "":
                if param["required"]:
                    raise ValidationError(
                        f"{entry['name']} needs {param['name']!r} "
                        f"({param['in']}, {param['type']})",
                        details={"endpoint": entry["name"],
                                 "missing": param["name"]})
                continue
            value = _coerce(raw, param["type"])
            if param["in"] == "path":
                path = path.replace(f"{{{param['name']}}}",
                                    urllib.parse.quote(str(value), safe=""))
            elif param["in"] == "query":
                query[param["name"]] = value
            else:
                body[param["name"]] = value
        for key, value in (params or {}).items():
            if key not in known and value not in (None, ""):
                query.setdefault(key, value)
        return path, query, body

    def execute(self, endpoint: str, params: dict | None = None, *,
                token: str | None = None, timeout: float | None = None) -> dict:
        entry = api_catalog.by_name(endpoint)
        if entry is None:
            raise ValidationError(f"unknown endpoint {endpoint!r}",
                                  details={"known": api_catalog.names()})
        if params is not None and not isinstance(params, dict):
            raise ValidationError("params must be an object of name -> value")
        path, query, body = self._build(entry, dict(params or {}))
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        payload = json.dumps(body).encode("utf-8") if body else None
        headers = {"accept": "application/json"}
        presented = token or self.admin_token
        if presented:
            headers["x-admin-token"] = presented
        if payload is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers,
                                         method=entry["method"])
        started = time.perf_counter()
        http_status = 0
        try:
            with urllib.request.urlopen(request,
                                        timeout=float(timeout or self.timeout)) as reply:
                http_status = reply.status
                raw = reply.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            http_status = error.code
            raw = error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            envelope = {"ok": False, "data": None, "error": {
                "code": "TOOL_UNREACHABLE",
                "message": f"{self.base_url} is not answering "
                           f"({getattr(error, 'reason', error)}) — start the API "
                           f"with `python -m oneness_tor`"}}
            return {"ok": False, "endpoint": entry["name"], "http_status": 0,
                    "latency_ms": elapsed,
                    "request": self._echo(entry, url, body),
                    "response": envelope, "data": None, "error": envelope["error"]}
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = {"ok": False, "data": raw[:2000], "error": {
                "code": f"HTTP_{http_status}", "message": raw[:400]}}
        if not isinstance(envelope, dict):
            envelope = {"ok": True, "data": envelope, "error": None}
        return {"ok": bool(envelope.get("ok")), "endpoint": entry["name"],
                "http_status": http_status, "latency_ms": elapsed,
                "request": self._echo(entry, url, body),
                "response": envelope, "data": envelope.get("data"),
                "error": envelope.get("error")}


def summarize(result: dict, limit: int = MAX_TOOL_CHARS) -> str:
    """Compact JSON for a tool message — big lists are trimmed, never faked."""
    body = {"ok": result.get("ok"), "endpoint": result.get("endpoint"),
            "http_status": result.get("http_status"),
            "data": result.get("data"), "error": result.get("error")}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    data = body.get("data")
    if isinstance(data, dict):
        body["data"] = {key: value for key, value in data.items()
                        if not isinstance(value, (list, dict))
                        or (isinstance(value, list) and len(value) <= 5)}
        body["truncated"] = True
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return text[:limit - 3] + "..." if len(text) > limit else text

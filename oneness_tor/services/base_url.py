"""The URL this node is *actually* reachable at — resolved per request, never guessed.

Three deployments share one code path:

* a local run — ``request.host_url`` is already correct;
* Render / Docker — TLS terminates in front of the process, so the honest URL
  lives in ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` / ``X-Forwarded-Port``
  (the RFC 7239 ``Forwarded`` header is honoured too); without this the docs
  would advertise ``http://0.0.0.0:10000``;
* a Tor hidden service — the ``Host`` header is ``<id>.onion`` and v3 onions do
  not use https, so the scheme is forced to ``http``.

``ONENESS_PUBLIC_BASE_URL`` wins when an operator pins a domain (a custom
domain in front of Render, or an onion address they publish themselves).
The value is also reported per source so the docs page can say where it came
from instead of pretending there is one canonical hostname.

:func:`remember` is the other half of that answer, and the half a deployment
cannot get wrong: whatever ``Host`` a request arrives with is a host this
process demonstrably serves. Those hosts are kept in ``data/node/self_hosts.json``
so ``oneness_tor.services.node_sync`` can recognise ``origin.url`` as *itself*
even when nobody sets ``ONENESS_PUBLIC_BASE_URL`` and the platform only
advertises ``https://<service>.onrender.com`` while the site answers on a custom
domain. Being wrong there is expensive: a node that does not know its own URL
demotes itself to a read-only replica of itself and refuses to propose blocks
(``409 SYNC_READ_ONLY``).
"""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings

ENV_PIN = "ONENESS_PUBLIC_BASE_URL"
INTERNAL_ENV = "ONENESS_INTERNAL_BASE_URL"

#: Hosts this process has answered for -> when each was first seen. A file, like
#: ``config.fleet.yaml``, so a restart keeps the answer and a test can point the
#: constant at its own copy.
REMEMBERED_PATH = settings.data_dir / "node" / "self_hosts.json"
REMEMBERED_LIMIT = 20
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})


def normalize(value: object) -> str:
    """One canonical ``scheme://host[:port]`` string (no trailing slash)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    scheme = (parts.scheme or "http").lower()
    if host.endswith(".onion"):
        scheme = "http"                       # Tor Browser never uses https here
    netloc = parts.netloc
    if parts.username or parts.password:      # never advertise credentials
        netloc = host + (f":{parts.port}" if parts.port else "")
    base = f"{scheme}://{netloc}"
    path = "/" + parts.path.strip("/") if parts.path.strip("/") else ""
    return (base + path).rstrip("/")


def _forwarded(request) -> dict:
    """First hop of ``X-Forwarded-*`` (Render, nginx, Cloudflare) or ``Forwarded``."""
    headers = getattr(request, "headers", {}) or {}

    def first(name: str) -> str:
        raw = headers.get(name) or ""
        return str(raw).split(",")[0].strip()

    proto = first("x-forwarded-proto").lower()
    host = first("x-forwarded-host")
    port = first("x-forwarded-port")
    if not host:
        forwarded = str(headers.get("forwarded") or "")
        for part in forwarded.split(",")[0].split(";"):
            key, _, value = part.strip().partition("=")
            value = value.strip().strip('"')
            if key.lower() == "host" and not host:
                host = value
            elif key.lower() == "proto" and not proto:
                proto = value.lower()
    return {"proto": proto, "host": host, "port": port}


def _with_port(scheme: str, host: str, port: str) -> str:
    if ":" in host or not port:
        return host
    if (scheme, port) in (("https", "443"), ("http", "80")):
        return host
    return f"{host}:{port}"


def _public_host(value: object) -> str:
    """The DNS host of ``value``, or ``""`` for loopback / a bare word.

    ``http://localhost:8090`` is this machine talking to itself, never a public
    address, and a ``Host`` header without a dot is not a name anybody can reach.
    """
    base = normalize(value)
    if not base:
        return ""
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if not host or host in LOOPBACK_HOSTS or "." not in host:
        return ""
    return host


def known_hosts() -> dict:
    """``{host: first seen}`` — every host this node has been addressed by."""
    try:
        payload = json.loads(Path(REMEMBERED_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("hosts") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        return {str(k): str(v) for k, v in rows.items() if _public_host(k)}
    if isinstance(rows, list):
        return {str(item): "" for item in rows if _public_host(item)}
    return {}


def remember(request) -> str:
    """Record the host this request was addressed to (``""`` when it is not one).

    ``Host``/``X-Forwarded-Host`` is the client's word, not proof — a caller can
    send anything. It is still the only evidence a custom domain in front of a
    Render service ever gives this process, and the value is used for exactly one
    decision (``is the fleet's origin.url me?``); the fleet's token digest stays
    the primary credential. Loopback and bare hosts are ignored, and the file is
    written only when a host is new, so a busy node does not rewrite it per hit.
    """
    forwarded = _forwarded(request)
    host = _public_host(forwarded["host"] or str(getattr(request, "host", "") or ""))
    if not host:
        return ""
    remembered = known_hosts()
    if host in remembered:
        return host
    remembered[host] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = dict(sorted(remembered.items(), key=lambda row: row[1])[-REMEMBERED_LIMIT:])
    path = Path(REMEMBERED_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"hosts": rows}, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        return host              # a read-only disk must not break a request
    return host


def request_base(request) -> str:
    """The base URL for *this* request, in precedence order."""
    pinned = normalize(os.environ.get(ENV_PIN))
    if pinned:
        return pinned
    forwarded = _forwarded(request)
    scheme = forwarded["proto"] or str(getattr(request, "scheme", "http") or "http")
    host = forwarded["host"] or str(getattr(request, "host", "") or "")
    if not host:
        return normalize(getattr(request, "host_url", "") or "")
    host = _with_port(scheme, host, forwarded["port"])
    return normalize(f"{scheme}://{host}")


def onion_base() -> str:
    """The published node onion, when this process published one."""
    try:
        from oneness_tor.services import tor_service
    except Exception:  # pragma: no cover - import cycle safety
        return ""
    try:
        return tor_service.node_url()
    except Exception:  # noqa: BLE001 - the docs page must still render
        return ""


def listen_base() -> str:
    """Where the WSGI server binds (useful only on the machine itself)."""
    host = str(settings.get("api.host") or "127.0.0.1")
    port = os.environ.get("PORT") or settings.get("api.port") or 8090
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return normalize(f"http://{host}:{port}")


def internal_base() -> str:
    """Where *this* process calls itself — loopback, never the public host.

    ``request_base()`` is the *browser-facing* URL. Over a hidden service that
    is ``<id>.onion``, which only resolves inside Tor: an in-process tool call
    has no SOCKS proxy, so using it fails with ``TOOL_UNREACHABLE``
    (``getaddrinfo failed``). ``ONENESS_INTERNAL_BASE_URL`` is set by
    ``python -m oneness_tor`` at boot; nothing else is a safe default.
    """
    explicit = normalize(os.environ.get(INTERNAL_ENV))
    if explicit:
        return explicit
    return listen_base()


def base_urls(request) -> dict:
    """Every URL worth showing, plus which rule produced ``current``."""
    pinned = normalize(os.environ.get(ENV_PIN))
    forwarded = _forwarded(request)
    onion = onion_base()
    if pinned:
        source = f"{ENV_PIN} env var"
    elif forwarded["host"] or forwarded["proto"]:
        source = "X-Forwarded-* headers (reverse proxy)"
    elif str(getattr(request, "host", "")).endswith(".onion"):
        source = "the .onion Host header"
    else:
        source = "the request Host header"
    current = request_base(request)
    return {"current": current, "source": source,
            "request": normalize(getattr(request, "host_url", "") or ""),
            "configured": pinned or None, "onion": onion or None,
            "listen": listen_base()}

"""IPFS metadata pinning (Pinata) and ``ipfs://`` gateway resolution.

Meme-token launch metadata and AI-NFT metadata are **public, permanent
documents**: they are what a wallet, a DEX listing or a marketplace reads when
it only has the token/NFT id. This module pins those documents to IPFS through
the operator's Pinata account and turns ``ipfs://<cid>/…`` into a real HTTPS
gateway URL for ``<img src>`` and JSON fetches.

Configuration (``.env`` at the repo root, which ``core.config.load_dotenv`` loads,
or real environment variables — those always win):

* ``PINATA_JWT`` — preferred, a scoped Pinata JWT,
* ``PINATA_API_KEY`` + ``PINATA_API_SECRET`` — the legacy key/secret pair,
* ``IPFS_GATEWAY`` — the gateway every URI resolves through,
* ``PINATA_GATEWAY_TOKEN`` — the gateway's read token (a private Pinata gateway
  answers ``403`` without it); it may also be part of ``IPFS_GATEWAY`` itself,
* ``ONENESS_IPFS_PIN=0`` — disable pinning (reads keep working).

Honesty rules:

* a pin failure never fails a mint: :func:`pin_json` returns
  ``{"pinned": False, "error": …}`` and the caller keeps its local record,
* :func:`status` never returns the JWT, the key or the secret — only whether
  they are configured and which gateway is in use,
* nothing is invented: an unconfigured node says so instead of pretending a
  document is pinned.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from core.config import settings

STANDARD = "oneness-ipfs-pin/1"
DEFAULT_GATEWAY = "https://ipfs.io/ipfs/"
USER_AGENT = "OneNessTOR/1.0 (+https://onenesstech.ca)"

_LOCK = threading.RLock()
_LAST: dict = {"at": None, "ok": None, "name": None, "cid": None, "error": None}


# --------------------------------------------------------------------- config
def _jwt() -> str:
    return str(settings.get("ipfs.pinata.jwt") or os.environ.get("PINATA_JWT") or "").strip()


def _key() -> str:
    return str(settings.get("ipfs.pinata.api_key")
               or os.environ.get("PINATA_API_KEY") or "").strip()


def _secret() -> str:
    return str(settings.get("ipfs.pinata.secret")
               or os.environ.get("PINATA_API_SECRET")
               or os.environ.get("PINATA_SECRET_API_KEY") or "").strip()


def enabled() -> bool:
    raw = settings.get("ipfs.pinata.enabled")
    return bool(raw) and bool(_jwt() or (_key() and _secret()))


def _timeout() -> float:
    try:
        return max(3.0, float(settings.get("ipfs.pinata.timeout") or 30.0))
    except (TypeError, ValueError):
        return 30.0


def _iso(stamp: float | None = None) -> str:
    return datetime.fromtimestamp(float(stamp or time.time()),
                                  timezone.utc).isoformat(timespec="seconds")


GATEWAY_TOKEN_ENVS = ("PINATA_GATEWAY_TOKEN", "ONENESS_IPFS_GATEWAY_TOKEN")
GATEWAY_TOKEN_PARAMS = ("pinatagatewaytoken", "gatewaytoken")


def gateway_token() -> str:
    """The read token a private gateway (Pinata) needs — ``""`` when unset."""
    value = str(settings.get("ipfs.gateway_token") or "").strip()
    if value:
        return value
    for name in GATEWAY_TOKEN_ENVS:
        found = os.environ.get(name, "").strip()
        if found:
            return found
    return ""


def _gateway() -> tuple[str, str]:
    """``(base ending in /ipfs/, read token)`` — a token inside the URL is peeled off."""
    raw = str(settings.get("ipfs.gateway") or DEFAULT_GATEWAY).strip() or DEFAULT_GATEWAY
    base, _, query = raw.partition("?")
    token = gateway_token()
    for chunk in query.split("&"):
        key, _, value = chunk.partition("=")
        if key.strip().lower() in GATEWAY_TOKEN_PARAMS and value.strip():
            token = token or value.strip()
    base = base.rstrip("/")
    if base.endswith("/ipfs"):
        base = base[: -len("/ipfs")]
    return base + "/ipfs/", token


def gateway() -> str:
    """The configured gateway, normalised to end with ``/ipfs/``."""
    return _gateway()[0]


def _with_token(url: str) -> str:
    """Append the gateway read token once — a private gateway answers 403 without it."""
    token = _gateway()[1]
    if not token or "?" in url:
        return url
    return f"{url}?pinataGatewayToken={token}"


def gateways() -> list[str]:
    """Every gateway worth trying, configured one first, duplicates removed."""
    rows = [gateway()]
    for item in settings.get("nft.ipfs_gateways") or []:
        text = str(item or "").strip()
        if not text:
            continue
        base = text.rstrip("/")
        if base.endswith("/ipfs"):
            base = base[: -len("/ipfs")]
        candidate = base + "/ipfs/"
        if candidate not in rows:
            rows.append(candidate)
    return rows


def resolve(uri: object) -> str:
    """``ipfs://<cid>/x.json`` / ``ar://<id>`` → an HTTPS URL; http(s) passes through."""
    text = str(uri or "").strip()
    if not text:
        return ""
    if text.startswith("ipfs://"):
        return _with_token(gateway() + text[len("ipfs://"):].lstrip("/"))
    if text.startswith("ar://"):
        return "https://arweave.net/" + text[len("ar://"):].lstrip("/")
    return text


def cid_of(uri: object) -> str:
    """The CID inside an ``ipfs://`` / gateway URI (``""`` when it is not one)."""
    text = str(uri or "").strip()
    if text.startswith("ipfs://"):
        return text[len("ipfs://"):].lstrip("/").split("/")[0]
    marker = "/ipfs/"
    if marker in text:
        return text.split(marker, 1)[1].lstrip("/").split("/")[0]
    return ""


# ----------------------------------------------------------------------- HTTP
def _headers(*, json_body: bool = True) -> dict:
    headers = {"accept": "application/json", "user-agent": USER_AGENT}
    if json_body:
        headers["content-type"] = "application/json"
    jwt = _jwt()
    if jwt:
        headers["authorization"] = f"Bearer {jwt}"
    else:
        headers["pinata_api_key"] = _key()
        headers["pinata_secret_api_key"] = _secret()
    return headers


def _post(url: str, body: bytes, headers: dict) -> tuple[dict | None, str | None]:
    """POST ``body``; ``(payload, error)`` — never raises, never logs a credential."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8") or "{}")
        return (payload if isinstance(payload, dict) else {}), None
    except urllib.error.HTTPError as problem:
        detail = ""
        try:
            detail = problem.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - the status alone is still useful
            pass
        return None, f"pinata HTTP {problem.code}: {detail or problem.reason}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as problem:
        return None, f"pinata unreachable: {getattr(problem, 'reason', problem)}"


def _record(*, ok: bool, name: str = "", cid: str = "", error: str | None = None) -> None:
    with _LOCK:
        _LAST.update(at=_iso(), ok=bool(ok), name=name, cid=cid or None,
                     error=(error or None))


# ---------------------------------------------------------------------- pins
def pin_json(document: dict, *, name: str = "metadata.json") -> dict:
    """Pin a JSON document and return where it lives (never raises)."""
    if not isinstance(document, dict):
        return {"pinned": False, "provider": "pinata",
                "error": "a JSON object is required"}
    filename = str(name or "metadata.json")[:120]
    result = {"standard": STANDARD, "provider": "pinata", "name": filename,
              "configured": enabled(), "gateway": gateway(), "pinned": False}
    if not enabled():
        result["error"] = ("no Pinata credential configured — set PINATA_JWT (or "
                           "PINATA_API_KEY + PINATA_API_SECRET) in .env")
        _record(ok=False, name=filename, error=result["error"])
        return result
    body = json.dumps({"pinataContent": document,
                       "pinataMetadata": {"name": filename},
                       "pinataOptions": {"cidVersion": 1}},
                      ensure_ascii=False).encode("utf-8")
    payload, error = _post(str(settings.get("ipfs.pinata.json_url")
                               or "https://api.pinata.cloud/pinning/pinJSONToIPFS"),
                           body, _headers(json_body=True))
    cid = str((payload or {}).get("IpfsHash") or "").strip()
    if error or not cid:
        result["error"] = error or "Pinata returned no IpfsHash"
        _record(ok=False, name=filename, error=result["error"])
        return result
    result.update(pinned=True, cid=cid, uri=f"ipfs://{cid}",
                  url=_with_token(gateway() + cid), pinned_at=_iso(),
                  bytes=len(body))
    _record(ok=True, name=filename, cid=cid)
    return result


def _multipart(filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----oneness" + uuid.uuid4().hex
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"file\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def pin_bytes(data: bytes, *, filename: str = "image.png",
              content_type: str = "") -> dict:
    """Pin a file (image/animation) and return its CID + gateway URL."""
    filename = str(filename or "file")[:120]
    result = {"standard": STANDARD, "provider": "pinata", "name": filename,
              "configured": enabled(), "gateway": gateway(), "pinned": False}
    if not data:
        result["error"] = "empty file"
        return result
    if not enabled():
        result["error"] = ("no Pinata credential configured — set PINATA_JWT (or "
                           "PINATA_API_KEY + PINATA_API_SECRET) in .env")
        _record(ok=False, name=filename, error=result["error"])
        return result
    kind = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body, boundary = _multipart(filename, data, kind)
    headers = _headers(json_body=False)
    headers["content-type"] = boundary
    payload, error = _post(str(settings.get("ipfs.pinata.file_url")
                               or "https://api.pinata.cloud/pinning/pinFileToIPFS"),
                           body, headers)
    cid = str((payload or {}).get("IpfsHash") or "").strip()
    if error or not cid:
        result["error"] = error or "Pinata returned no IpfsHash"
        _record(ok=False, name=filename, error=result["error"])
        return result
    result.update(pinned=True, cid=cid, uri=f"ipfs://{cid}",
                  url=_with_token(gateway() + cid), pinned_at=_iso(),
                  bytes=len(data), content_type=kind)
    _record(ok=True, name=filename, cid=cid)
    return result


def pin_data_url(data_url: object, *, filename: str = "image") -> dict:
    """Pin a ``data:image/…;base64,…`` payload (what the studio UI can produce)."""
    text = str(data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        return {"pinned": False, "provider": "pinata",
                "error": "not a base64 data: URL"}
    header, _, encoded = text.partition(",")
    kind = header[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" not in header:
        return {"pinned": False, "provider": "pinata",
                "error": "data: URL is not base64-encoded"}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return {"pinned": False, "provider": "pinata",
                "error": "broken base64 payload"}
    suffix = mimetypes.guess_extension(kind) or ".bin"
    return pin_bytes(raw, filename=f"{filename}{suffix}", content_type=kind)


# --------------------------------------------------------------------- status
def status() -> dict:
    """Credential presence + the last pin — never a secret, never a guess."""
    with _LOCK:
        last = dict(_LAST)
    return {"standard": STANDARD, "enabled": enabled(),
            "configured": {"jwt": bool(_jwt()), "api_key": bool(_key()),
                           "secret": bool(_secret())},
            "gateway": gateway(), "gateway_token": bool(gateway_token()),
            "gateways": gateways(),
            "last_pin": last, "last_pin_at": last.get("at"),
            "last_error": last.get("error"),
            "note": ("pinned documents are public and permanent: anyone with the "
                     "CID can read them through the gateway")}


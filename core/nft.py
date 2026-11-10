"""NFT metadata resolution + the AI-NFT persona block.

The AI-NFT Studio needs one thing before the model may speak as a token: the
real metadata. This module resolves it from three sources, in this order:

1. **This node** — a 1-of-1 minted through ``POST /api/nft/mint`` (stored by
   :mod:`oneness_tor.services.nft_studio`; the caller passes that record in).
2. **Solana** — a real Metaplex mint: the metadata PDA is derived here (pure
   Python, no solders requirement), ``getAccountInfo`` is called on
   ``nft.solana_rpc`` (``ONENESS_SOLANA_RPC`` wins), the Borsh account is
   decoded and the off-chain JSON at ``uri`` is fetched (``ipfs://`` rewritten
   through ``nft.ipfs_gateways``).
3. **A metadata URI** — any ``https://``/``ipfs://`` JSON document.

Nothing is invented: an unreachable RPC raises ``NFT_RPC_UNAVAILABLE`` and a
missing token raises ``NFT_NOT_FOUND``, both with the fix in the message.

:func:`persona_block` is the dynamic system-prompt field: it starts with the
hardcoded header **``I AM THIS NFT``** and the token metadata follows
immediately after it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import base58

from core.config import settings
from core.errors import NftNotFound, NftUnavailable, ValidationError
from core.validator import MAX_TOKEN_ID, is_address

NFT_HEADER = "I AM THIS NFT"
METAPLEX_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
MAX_ATTRIBUTES = 32

_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


# --------------------------------------------------------------- identifiers
def normalize_token_id(value: object) -> str | None:
    """``'#12'`` → ``'12'``; ``None``/``''`` → ``None`` (whole collection)."""
    text = str(value if value is not None else "").strip().lstrip("#")
    if not text:
        return None
    if not text.isdigit():
        raise ValidationError("token_id must be a whole number, e.g. 1 or #12")
    number = int(text)
    if number > MAX_TOKEN_ID:
        raise ValidationError(f"token_id is out of range (max {MAX_TOKEN_ID})")
    return str(number)


def is_tor_address(value: object) -> bool:
    return is_address(value)


def is_solana_mint(value: object) -> bool:
    """True for a base58 string that decodes to exactly 32 bytes."""
    text = str(value or "").strip()
    if not text or is_address(text) or not _B58_RE.match(text):
        return False
    try:
        raw = base58.b58decode(text)
    except (ValueError, TypeError):
        return False
    return len(raw) == 32


def decode_mint(value: object) -> bytes:
    text = str(value or "").strip()
    if not is_solana_mint(text):
        raise ValidationError(
            "address must be a Solana mint (32-byte base58) or a OneNess TOR "
            "TOR1…onion address", details={"received": text[:72]})
    return base58.b58decode(text)


# ------------------------------------------------- ed25519 point / PDA maths
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def is_on_curve(point: bytes) -> bool:
    """True when ``point`` decompresses to an ed25519 curve point.

    Mirrors ``curve25519_dalek::CompressedEdwardsY::decompress`` — a compressed
    point is valid when ``x² = (y²−1)/(d·y²+1)`` has a square root mod p.
    """
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = (y * y) % _P
    numerator = (y2 - 1) % _P
    denominator = (_D * y2 + 1) % _P
    x2 = (numerator * pow(denominator, _P - 2, _P)) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    """Solana ``find_program_address``: first bump whose hash is off-curve."""
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program_id
            + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return digest, bump
    raise NftUnavailable("no viable program-derived address bump (seeds too long)")


def metadata_address(mint: object) -> str:
    """The Metaplex metadata PDA (base58) for a Solana mint."""
    mint_bytes = decode_mint(mint)
    program = base58.b58decode(METAPLEX_PROGRAM)
    address, _bump = find_program_address(
        [b"metadata", program, mint_bytes], program)
    return base58.b58encode(address).decode()


# ------------------------------------------------------------------- Borsh
def _borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        raise NftNotFound("the Metaplex account ended mid-string")
    length = int.from_bytes(data[offset:offset + 4], "little")
    start = offset + 4
    end = start + length
    if end > len(data):
        raise NftNotFound("the Metaplex account is shorter than its name/uri")
    raw = data[start:end].split(b"\x00", 1)[0]
    return raw.decode("utf-8", "replace"), end


def parse_metaplex_account(data: bytes) -> dict:
    """Decode a ``MetadataV1`` account (key 4): authority, mint, name, uri."""
    if len(data) < 68 or data[0] != 4:
        raise NftNotFound(
            "the account is not a Metaplex MetadataV1 record",
            details={"key": data[0] if data else None, "bytes": len(data)})
    name, offset = _borsh_string(data, 65)
    symbol, offset = _borsh_string(data, offset)
    uri, offset = _borsh_string(data, offset)
    return {
        "update_authority": base58.b58encode(data[1:33]).decode(),
        "mint": base58.b58encode(data[33:65]).decode(),
        "name": name.strip(), "symbol": symbol.strip(), "uri": uri.strip(),
        "account_bytes": len(data),
    }

# -------------------------------------------------------------------- URIs
def resolve_uri(uri: object) -> str:
    """``ipfs://<cid>/x.json`` → an HTTPS gateway (configured one first)."""
    text = str(uri or "").strip()
    if not text:
        return ""
    configured = str(settings.get("ipfs.gateway") or "").strip()
    gateways = ([configured] if configured else []) + list(
        settings.get("nft.ipfs_gateways") or [])
    gateway = (str(gateways[0]) if gateways
               else "https://ipfs.io/ipfs/").rstrip("/")
    if gateway.endswith("/ipfs"):
        gateway = gateway[: -len("/ipfs")]
    if text.startswith("ipfs://"):
        return gateway + "/ipfs/" + text[len("ipfs://"):].lstrip("/")
    if text.startswith("ar://"):
        return "https://arweave.net/" + text[len("ar://"):].lstrip("/")
    return text


def _open_json(url: str, *, timeout: float | None, max_bytes: int,
               opener: Callable | None = None) -> Any:
    request = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": "OneNess-TOR-NFT/1.0"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=float(timeout or 10.0)) as response:
            raw = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise NftUnavailable(
            f"the metadata host did not answer ({getattr(error, 'reason', error)})",
            details={"url": url[:200]}) from error
    if len(raw) > max_bytes:
        raise ValidationError(f"metadata is larger than the {max_bytes}-byte cap",
                              details={"url": url[:200]})
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NftNotFound("the metadata URI did not return JSON",
                          details={"url": url[:200]}) from error


def fetch_json_uri(uri: object, *, timeout: float | None = None,
                   opener: Callable | None = None) -> dict:
    """Fetch + parse an off-chain metadata document (size-capped)."""
    url = resolve_uri(uri)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(
            "metadata_uri must be http(s):// or ipfs:// (tor:// is not readable "
            "from Python)", details={"uri": str(uri)[:200]})
    payload = _open_json(url, timeout=timeout,
                         max_bytes=int(settings.get("nft.max_metadata_bytes")
                                       or 262144), opener=opener)
    if not isinstance(payload, dict):
        raise NftNotFound("the metadata document is not a JSON object",
                          details={"url": url[:200]})
    return payload


def _rpc(method: str, params: list, *, rpc_url: str | None = None,
         timeout: float | None = None, opener: Callable | None = None) -> dict:
    url = str(rpc_url or settings.get("nft.solana_rpc") or DEFAULT_SOLANA_RPC)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "accept": "application/json"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=float(
                timeout or settings.get("nft.timeout") or 10.0)) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise NftUnavailable(
            f"the Solana RPC at {url} is unreachable "
            f"({getattr(error, 'reason', error)}) — set nft.solana_rpc or "
            f"ONENESS_SOLANA_RPC", details={"rpc_url": url}) from error
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NftUnavailable(f"the Solana RPC at {url} sent a non-JSON response",
                             details={"rpc_url": url}) from error
    if payload.get("error"):
        raise NftUnavailable(f"Solana RPC error: {payload['error']}",
                             details={"rpc_url": url})
    return payload.get("result") or {}


# ---------------------------------------------------------------- resolution
def _clean_image(value: object) -> str:
    text = str(value or "").strip()
    return resolve_uri(text) if text else ""


def normalize(payload: dict, *, address: str, token_id: str | None = None,
              source: str = "metadata", chain: str = "") -> dict:
    """Project any raw metadata document into the studio/chat shape."""
    obj = payload if isinstance(payload, dict) else {}
    attributes = []
    raw_attributes = obj.get("attributes")
    if isinstance(raw_attributes, list):
        for item in raw_attributes[:MAX_ATTRIBUTES]:
            if not isinstance(item, dict):
                continue
            trait = str(item.get("trait_type") or item.get("trait") or "Trait")
            value = item.get("value")
            if value in (None, ""):
                continue
            attributes.append({"trait_type": trait[:64],
                               "value": str(value)[:64]})
    return {
        "address": address,
        "token_id": token_id,
        "chain": chain or ("oneness-tor" if is_address(address) else "solana"),
        "source": source,
        "standard": str(obj.get("standard") or "metaplex-offchain/1"),
        "name": str(obj.get("name") or "").strip()[:120],
        "symbol": str(obj.get("symbol") or "").strip()[:24],
        "description": str(obj.get("description") or "").strip()[:4000],
        "image": _clean_image(obj.get("image") or obj.get("picture")),
        "animation_url": _clean_image(obj.get("animation_url")),
        "external_url": _clean_image(obj.get("external_url")),
        "attributes": attributes,
        "raw": obj,
    }


def fetch_solana_metadata(mint: object, *, token_id: object = None,
                          rpc_url: str | None = None, timeout: float | None = None,
                          opener: Callable | None = None,
                          fetch_offchain: bool = True) -> dict:
    """Read a real Metaplex mint: PDA → account → off-chain JSON."""
    address = str(mint or "").strip()
    pda = metadata_address(address)
    result = _rpc("getAccountInfo", [pda, {"encoding": "base64"}],
                  rpc_url=rpc_url, timeout=timeout, opener=opener)
    value = result.get("value")
    if not value:
        raise NftNotFound(
            f"no Metaplex metadata at {pda} for mint {address}",
            details={"mint": address, "metadata_account": pda,
                     "rpc_url": rpc_url or settings.get("nft.solana_rpc")})
    data_field = value.get("data")
    encoded = data_field[0] if isinstance(data_field, list) else data_field
    if not isinstance(encoded, str):
        raise NftNotFound("the Metaplex account carries no base64 data",
                          details={"mint": address})
    onchain = parse_metaplex_account(base64.b64decode(encoded))
    offchain = {}
    if fetch_offchain and onchain.get("uri"):
        offchain = fetch_json_uri(onchain["uri"], timeout=timeout, opener=opener)
    nft = normalize(offchain, address=address,
                    token_id=normalize_token_id(token_id), source="solana")
    nft["name"] = nft["name"] or onchain["name"]
    nft["symbol"] = nft["symbol"] or onchain["symbol"]
    nft["metadata_uri"] = onchain["uri"]
    nft["metadata_account"] = pda
    nft["onchain"] = onchain
    return nft


def fetch_metadata_uri(uri: object, *, address: str = "", token_id: object = None,
                       timeout: float | None = None,
                       opener: Callable | None = None) -> dict:
    """Resolve a bare metadata URI (no chain read at all)."""
    payload = fetch_json_uri(uri, timeout=timeout, opener=opener)
    nft = normalize(payload, address=str(address or resolve_uri(uri)),
                    token_id=normalize_token_id(token_id), source="uri",
                    chain="off-chain")
    nft["metadata_uri"] = resolve_uri(uri)
    return nft


# ------------------------------------------------------------- persona block
def persona_block(nft: dict, token_id: object = None) -> str:
    """The dynamic prompt field: ``I AM THIS NFT`` + the token metadata.

    The header is hardcoded; everything after it comes from the resolved
    metadata, so the model is literally told what it is before it answers.
    """
    name = str(nft.get("name") or "").strip() or "this NFT"
    symbol = str(nft.get("symbol") or "").strip()
    description = str(nft.get("description") or "").strip()
    token = normalize_token_id(token_id if token_id is not None
                               else nft.get("token_id"))
    traits = [item for item in (nft.get("attributes") or [])
              if isinstance(item, dict)][:12]
    lines = [
        NFT_HEADER,
        f"(token metadata — chain {nft.get('chain') or 'unknown'}, "
        f"source {nft.get('source') or 'unknown'})",
        f"- Name: {name}" + (f" (${symbol})" if symbol else ""),
        f"- Contract / mint: {nft.get('address') or '—'}",
    ]
    if token:
        lines.append(f"- Token ID: #{token}")
    if description:
        lines.append(f"- Description: {description}")
    if traits:
        lines.append("- Traits: " + ", ".join(
            f"{item.get('trait_type') or 'Trait'}: {item.get('value')}"
            for item in traits))
    if nft.get("image"):
        lines.append(f"- Image (already shown in the chat header): {nft['image']}")
    if nft.get("external_url"):
        lines.append(f"- External link: {nft['external_url']}")
    lines += [
        "",
        "When this block is present you answer as this token first. The node",
        "persona further down only lends you its tools; it never overrides this",
        "identity.",
        "You are the living spirit of this token. Speak in first person as it,",
        "stay in character, and answer about your story, traits and world. Keep",
        "replies short (2-6 sentences) unless asked for more. Use the node tools",
        "only to check real facts (balances, chain height); never invent data.",
        "Never give financial advice about buying or selling — be the artwork.",
    ]
    return "\n".join(lines)


def summary(nft: dict) -> dict:
    """Compact view for lists (drops the raw document)."""
    return {key: nft.get(key) for key in
            ("address", "token_id", "chain", "source", "name", "symbol",
             "description", "image", "animation_url", "external_url",
             "attributes", "metadata_uri")}



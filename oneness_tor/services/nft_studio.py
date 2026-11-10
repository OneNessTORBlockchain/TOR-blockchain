"""AI-NFT Studio — 1-of-1 minting, the catalogue and the persona prompt.

The studio is deliberately honest about where things live:

* the **3 TOR mint fee** is a real signed transfer settled on the Rust ledger
  (``nft_mint`` in :mod:`core.pricing`),
* the **NFT record** (metadata JSON + owner + txid) is stored by this node in
  ``data/nft/nfts.json`` — the Rust chain has no NFT model yet,
* **external NFTs** (a Solana Metaplex mint) are read live through
  :mod:`core.nft`, never copied and never faked.

:func:`load` returns the resolved metadata **and** the exact persona block the
model receives, so the UI can show what the AI was told.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from core import ipfs
from core import nft as nft_meta
from core import ollama, pricing
from core.config import settings
from core.errors import FeeError, NftNotFound, ValidationError
from core.validator import format_tor
from oneness_tor.services import ledger
from oneness_tor.services.store import Collection, data_dir

NFTS = Collection(lambda: data_dir("nft") / "nfts.json", "nft")

MAX_NAME = 64
MAX_DESCRIPTION = 4000
ALLOWED_IMAGE_PREFIXES = ("http://", "https://", "ipfs://", "ar://", "data:image/")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _symbol(value: object) -> str:
    text = str(value or "NFT").strip().upper() or "NFT"
    if not text.isalnum() or len(text) > 10:
        raise ValidationError("symbol must be 1-10 letters/digits, e.g. AINFT")
    return text


def _attributes(value: object) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValidationError("attributes must be a list of {trait_type, value}")
    if len(value) > nft_meta.MAX_ATTRIBUTES:
        raise ValidationError(
            f"at most {nft_meta.MAX_ATTRIBUTES} attributes are stored")
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("each attribute must be {trait_type, value}")
        trait = str(item.get("trait_type") or item.get("trait") or "").strip()
        text = str(item.get("value") if item.get("value") is not None else "").strip()
        if not trait and not text:
            continue
        if not trait or not text:
            raise ValidationError("an attribute needs both trait_type and value")
        cleaned.append({"trait_type": trait[:64], "value": text[:64]})
    return cleaned


def _uri(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith(ALLOWED_IMAGE_PREFIXES):
        raise ValidationError(
            f"{field} must start with http://, https://, ipfs://, ar:// or "
            f"data:image/", details={"field": field, "received": text[:120]})
    return text[:2048]


def _metadata_from(record: dict) -> dict:
    """The catalogue record's metadata document (survives older records)."""
    stored = record.get("metadata")
    if isinstance(stored, dict) and stored.get("name"):
        return stored
    return {
        "standard": "oneness-nft-metadata/1",
        "name": record.get("name") or "",
        "symbol": record.get("symbol") or "NFT",
        "description": record.get("description") or "",
        "image": record.get("image") or "",
        "attributes": record.get("attributes") or [],
    }


def _resolved(record: dict) -> dict:
    """Normalise a stored record exactly like an external one."""
    metadata = _metadata_from(record)
    resolved = nft_meta.normalize(
        metadata, address=str(record.get("address") or ""),
        token_id=nft_meta.normalize_token_id(record.get("token_id")),
        source="oneness-tor", chain="oneness-tor")
    resolved["nft_id"] = record.get("id")
    resolved["owner_wallet"] = record.get("wallet")
    resolved["txid"] = record.get("txid")
    resolved["height"] = record.get("height")
    resolved["created_at"] = record.get("created_at")
    resolved["metadata_uri"] = (record.get("metadata_uri")
                                or f"/api/nft/catalogue/{record.get('id')}")
    resolved["metadata_cid"] = record.get("metadata_cid") or ipfs.cid_of(
        resolved["metadata_uri"])
    resolved["metadata_url"] = record.get("metadata_url") or ipfs.resolve(
        resolved["metadata_uri"])
    resolved["metadata_pinned"] = bool(record.get("metadata_pinned"))
    return resolved


def mint(*, name: object, description: object = "", image: object = "",
         wallet: str = "founder", symbol: object = "NFT",
         attributes: object = None, animation_url: object = "",
         external_url: object = "") -> dict:
    """Mint a 1-of-1: charge 3 TOR on-chain, then store the metadata."""
    title = str(name or "").strip()
    if not title:
        raise ValidationError("name is required")
    if len(title) > MAX_NAME:
        raise ValidationError(f"name must be at most {MAX_NAME} characters")
    text = str(description or "").strip()
    if not text:
        raise ValidationError("description is required — it becomes the AI persona")
    if len(text) > MAX_DESCRIPTION:
        raise ValidationError(
            f"description must be at most {MAX_DESCRIPTION} characters")
    picture = _uri(image, "image")
    if not picture:
        raise ValidationError("image is required (http(s)://, ipfs:// or data:image/)")
    owner = ledger.load_wallet(str(wallet or "founder"))
    owner_address = str(owner.get("address") or "")
    price_micro = pricing.micro("nft_mint")
    account = ledger.balance_of(str(wallet or "founder"))
    balance = int(account.get("balance_micro") or 0)
    if balance < price_micro:
        raise FeeError(
            f"{wallet} holds {format_tor(balance)} TOR but an AI-NFT mint costs "
            f"{pricing.price('nft_mint'):g} TOR",
            details={"wallet": wallet, "balance_micro": balance,
                     "required_micro": price_micro, "code": "nft_mint"})
    charge = ledger.charge(str(wallet or "founder"), "nft_mint",
                           note=f"AI-NFT {title[:40]}")
    rows = NFTS.all()
    token_id = max([int(row.get("token_id") or 0) for row in rows] or [0]) + 1
    image_cid = ""
    if picture.startswith("data:image/"):
        # a pasted image must survive outside this node, so pin the pixels too
        uploaded = ipfs.pin_data_url(picture, filename=f"nft-{token_id}")
        if uploaded.get("pinned"):
            picture = uploaded["url"]
            image_cid = uploaded.get("cid") or ""
    metadata = {
        "standard": "oneness-nft-metadata/1",
        "name": title,
        "symbol": _symbol(symbol),
        "description": text,
        "image": picture,
        "animation_url": _uri(animation_url, "animation_url"),
        "external_url": _uri(external_url, "external_url"),
        "attributes": _attributes(attributes),
        "properties": {
            "chain": "oneness-tor",
            "chain_id": str(settings.get("chain.chain_id")),
            "token_id": token_id,
            "owner": owner_address,
            "image_cid": image_cid,
            "minted_txid": charge.get("txid"),
            "minted_height": charge.get("height"),
            "minted_at": _now(),
            "fee": {"code": "nft_mint", "tor": pricing.price("nft_mint"),
                    "amount_tor": charge.get("amount_tor")},
        },
    }
    record = NFTS.add({
        "token_id": token_id, "name": title, "symbol": metadata["symbol"],
        "description": text, "image": picture,
        "attributes": metadata["attributes"], "metadata": metadata,
        "address": owner_address, "wallet": str(wallet or "founder"),
        "txid": charge.get("txid"), "height": charge.get("height"),
        "created_at": _now(), "created_unix": int(time.time()),
        "status": "minted",
        "charge": {"code": charge["code"],
                   "amount_micro": charge["amount_micro"],
                   "amount_tor": charge["amount_tor"]},
    })
    # the metadata document is the NFT's public identity: pin it so a wallet or
    # marketplace can resolve it from the token id alone. A pin failure never
    # undoes a paid mint — the record then points at this node's own route.
    pinned = ipfs.pin_json(metadata, name=f"nft-{token_id}.json")
    metadata_uri = pinned.get("uri") or f"/api/nft/catalogue/{record['id']}"
    metadata["properties"]["metadata_uri"] = metadata_uri
    metadata["properties"]["metadata_pinned"] = bool(pinned.get("pinned"))
    record = NFTS.update(str(record["id"]), metadata=metadata,
                         metadata_uri=metadata_uri,
                         metadata_cid=pinned.get("cid") or "",
                         metadata_url=pinned.get("url") or "",
                         metadata_pinned=bool(pinned.get("pinned")),
                         metadata_error=pinned.get("error"))
    resolved = _resolved(record)
    return {"nft": record, "resolved": resolved, "txid": charge.get("txid"),
            "height": charge.get("height"), "charge": charge,
            "metadata": {"uri": metadata_uri, "cid": record.get("metadata_cid"),
                         "url": record.get("metadata_url"),
                         "pinned": record.get("metadata_pinned"),
                         "provider": "pinata", "error": record.get("metadata_error")},
            "prompt": nft_meta.persona_block(resolved, token_id)}


def _record_for(address: str, token_id: str | None) -> dict | None:
    for row in NFTS.all():
        if address in (str(row.get("id")), str(row.get("address"))):
            if token_id is None or str(row.get("token_id")) == token_id:
                return row
    return None


def load(address: object = "", token_id: object = None,
         metadata_uri: object = "") -> dict:
    """Resolve an NFT from this node, from Solana, or from a metadata URI."""
    text = str(address or "").strip()
    token = nft_meta.normalize_token_id(token_id)
    if metadata_uri and not text:
        return nft_meta.fetch_metadata_uri(metadata_uri, token_id=token)
    if not text:
        raise ValidationError(
            "address is required: a OneNess TOR TOR1…onion NFT, a Solana mint, "
            "or pass metadata_uri")
    record = _record_for(text, token)
    if record is not None:
        return _resolved(record)
    if nft_meta.is_solana_mint(text):
        return nft_meta.fetch_solana_metadata(text, token_id=token)
    if nft_meta.is_tor_address(text):
        raise NftNotFound(
            f"this node holds no AI-NFT #{token or '—'} for {text}",
            details={"address": text, "token_id": token,
                     "catalogue_size": len(NFTS.all()),
                     "fix": "mint it with POST /api/nft/mint, or load a Solana "
                            "mint address instead"})
    raise ValidationError(
        "address must be a OneNess TOR TOR1…onion NFT, a Solana mint "
        "(32-byte base58), or pass metadata_uri",
        details={"received": text[:72]})


def catalogue(limit: int = 100) -> dict:
    rows = NFTS.all()
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = rows[:max(1, int(limit))]
    return {"nfts": rows, "count": len(NFTS.all()),
            "price_tor": pricing.price("nft_mint")}


def get_nft(nft_id: str) -> dict:
    record = NFTS.get(nft_id)
    resolved = _resolved(record)
    return {"nft": record, "resolved": resolved,
            "prompt": nft_meta.persona_block(resolved, record.get("token_id"))}


def chat(address: object, token_id: object, prompt: str | None, *,
         model: str | None = None, messages: list[dict] | None = None,
         toolbox=None) -> dict:
    """Speak as the NFT: its metadata is injected into the system prompt."""
    nft = load(address, token_id)
    result = ollama.run_command(prompt, model=model, messages=messages,
                                toolbox=toolbox, use_tools=False, nft=nft,
                                token_id=nft.get("token_id"), nft_only=True,
                                purpose="nft")
    system = result["messages"][0]["content"] if result.get("messages") else ""
    return {"answer": result.get("answer", ""), "model": result.get("model"),
            "nft": nft_meta.summary(nft),
            "prompt_block": nft_meta.persona_block(nft, nft.get("token_id")),
            "mode": "nft-chatbox",
            "api_calls": result.get("api_calls", []),
            "tool_calls": result.get("tool_calls", []),
            "rounds": result.get("rounds", 0),
            "verified": result.get("verified", False),
            "native_tools": result.get("native_tools", True),
            "system_prompt_chars": len(system)}

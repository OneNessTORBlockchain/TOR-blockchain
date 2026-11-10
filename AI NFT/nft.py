"""Solana 1-of-1 NFT minting + metadata — the wallet-side port of the `sol mint` bot.

The tested bot (``mint bot/sol mint``) minted NFTs from a private payer key by
sequencing the same on-chain pipeline this module rebuilds with the wallet's
solders stack:

    system CreateAccount (rent-exempt 82-byte mint)
      -> SPL InitializeMint (0 decimals, mint+freeze authority = owner)
      -> SPL Associated Token Account
      -> SPL MintTo (exactly 1 token)
      -> Metaplex CreateMetadataAccountV3 (name/symbol/uri, creators)

Off-chain side the bot uploaded the image and the metadata JSON to
catbox.moe; ``upload_catbox`` (+ failover hosts: configured Pinata first,
then anonymous 0x0.st / litterbox when catbox is down) and
``build_metadata_json`` keep that flow, and
``nft_bundle`` reads any mint back (PDA name/symbol/uri + the off-chain JSON:
description, image, attributes) for the AI-NFT chatbox.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time

import requests
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.config import METADATA_PROGRAM_ID
from app.solana import metadata as meta_mod
from app.solana import tx as tx_mod
from app.solana.spl import (
    MINT_ACCOUNT_SPACE,
    SYSTEM_PROGRAM_ID,
    TOKEN_ACCOUNT_SPACE,
    TOKEN_PROGRAM_ID,
    create_account_ix,
    create_ata_idempotent_ix,
    derive_ata,
    initialize_mint_ix,
    mint_to_ix,
    system_transfer_ix,
)

METADATA_PROGRAM = Pubkey.from_string(METADATA_PROGRAM_ID)

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"      # permanent, anonymous
ZEROD_UPLOAD_URL = "https://0x0.st/"        # anonymous, ≥30 days while hot
LITTERBOX_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"  # 72h
PINATA_UPLOAD_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"  # your own pin
GATEWAY_IPFS = "https://ipfs.io/ipfs/"
GATEWAY_PINATA = "https://gateway.pinata.cloud/ipfs/"
GATEWAY_ARWEAVE = "https://arweave.net/"

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BASE64 = MAX_UPLOAD_BYTES + (MAX_UPLOAD_BYTES + 2) // 3
MAX_ATTRIBUTES = 32
METADATA_APPROX_SPACE = 700  # rent preflight (real account grows later)
SIG_FEE_LAMPORTS = 15_000  # 2-3 signature fees of headroom over the rents


class MintError(RuntimeError):
    """User-presentable mint / NFT-load failure."""


class UploadError(MintError):
    """Off-chain upload (image / metadata JSON) failure."""


# ---- instruction encoding (Metaplex Token Metadata) --------------------------

def _borsh_str(value: str, limit: int) -> bytes:
    """borsh string: u32 LE length + utf-8 bytes (truncated to the ABI cap)."""
    raw = str(value or "").strip()[:limit].encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _creators_borsh(creators) -> bytes:
    """Option<Vec<Creator { address, verified, share }>> — None encodes as 0x00."""
    if not creators:
        return b"\x00"
    out = b"\x01" + struct.pack("<I", len(creators))
    for address, verified, share in creators:
        out += (
            bytes(address)
            + (b"\x01" if verified else b"\x00")
            + struct.pack("<B", max(0, min(100, int(share))))
        )
    return out


def create_metadata_accounts_v3_ix(
    *,
    mint: Pubkey,
    mint_authority: Pubkey,
    payer: Pubkey,
    update_authority: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    seller_fee_bps: int = 500,
    creators=None,
    is_mutable: bool = True,
) -> Instruction:
    """Metaplex CreateMetadataAccountV3 (instruction tag 33, DataV2 args).

    Layout (borsh): DataV2 { name, symbol, uri, seller_fee_bps, creators?,
    collections?, uses? } -> is_mutable -> collection_details?. Accounts:
    metadata (mut), mint, mint_authority (signer), payer (signer, mut),
    update_authority (signer), system program.
    """
    metadata_acct = Pubkey.find_program_address(
        [b"metadata", bytes(METADATA_PROGRAM), bytes(mint)], METADATA_PROGRAM
    )[0]
    data = (
        struct.pack("<B", 33)                     # CreateMetadataAccountV3
        + _borsh_str(name, 32)                    # Metaplex MAX_NAME_LENGTH
        + _borsh_str(symbol, 10)                  # MAX_SYMBOL_LENGTH
        + _borsh_str(uri, 200)                    # MAX_URI_LENGTH
        + struct.pack("<H", max(0, min(10_000, int(seller_fee_bps))))
        + _creators_borsh(creators)
        + b"\x00"                                 # collections: none
        + b"\x00"                                 # uses: none
        + (b"\x01" if is_mutable else b"\x00")
        + b"\x00"                                 # collection_details: none
    )
    return Instruction(
        METADATA_PROGRAM,
        data,
        [
            AccountMeta(metadata_acct, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(mint_authority, True, False),
            AccountMeta(payer, True, True),
            AccountMeta(update_authority, True, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        ],
    )


# ---- off-chain: image + metadata JSON upload (catbox, as the bot did) --------
# A dead host must never brick a launch: uploads happen BEFORE any transaction,
# so nothing is ever stranded mid-mint. Failover order is by permanence:
# catbox (permanent) → Pinata when creds exist (permanent, own pin) →
# 0x0.st (≥30 days, longer while fetched) → litterbox (72h, last resort).
# Every downgrade prints so nothing silently loses permanence.

def _upload_attempts(data: bytes, filename: str, content_type: str) -> list:
    """Failover chain; each entry is (url, headers, files-dict, kind)."""
    attempts = [
        (CATBOX_UPLOAD_URL, {},
         {"reqtype": (None, "fileupload"),
          "fileToUpload": (filename, data, content_type)}, "plain"),
    ]
    jwt = os.environ.get("PINATA_JWT", "").strip()
    key = os.environ.get("PINATA_API_KEY", "").strip()
    secret = os.environ.get("PINATA_SECRET_API_KEY", "").strip()
    if jwt or (key and secret):
        headers = ({"Authorization": f"Bearer {jwt}"} if jwt
                   else {"pinata_api_key": key, "pinata_secret_api_key": secret})
        attempts.append((PINATA_UPLOAD_URL, headers,
                         {"file": (filename, data, content_type)}, "pinata"))
    attempts += [
        (ZEROD_UPLOAD_URL, {},
         {"file": (filename, data, content_type)}, "plain"),
        (LITTERBOX_UPLOAD_URL, {},
         {"reqtype": (None, "fileupload"),
          "time": (None, "72h"),
          "fileToUpload": (filename, data, content_type)}, "plain"),
    ]
    return attempts


def upload_catbox(data: bytes, filename: str, content_type: str, timeout: float = 120.0) -> str:
    """Anonymous image/metadata hosting with failover; returns the hosted URL.

    Hosts tried in order (first valid https:// answer wins):
      1. catbox.moe                          — permanent, anonymous (canonical)
      2. Pinata (PINATA_JWT, or PINATA_API_KEY + PINATA_SECRET_API_KEY pair)
                                             — permanent, your own pin
      3. 0x0.st                              — anonymous, ≥30 days while hot
      4. litterbox.catbox.moe (72h)          — anonymous, last resort
    """
    if not data:
        raise UploadError("nothing to upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"{filename} is {len(data)} bytes — max is {MAX_UPLOAD_BYTES}")
    connect_to = (min(float(timeout), 30.0), float(timeout))
    last = ""
    for url, headers, fields, kind in _upload_attempts(data, filename, content_type):
        try:
            resp = requests.post(url, files=fields, headers=headers or None,
                                 timeout=connect_to)
        except requests.RequestException as exc:
            last = f"{url} unreachable: {exc}"
            print(f"[upload] {last}", flush=True)
            continue
        if kind == "pinata":
            out = ""
            try:
                ipfs_hash = json.loads(resp.text or "{}")["IpfsHash"]
            except (ValueError, KeyError):
                ipfs_hash = ""
            if resp.status_code == 200 and ipfs_hash:
                gw = os.environ.get("IPFS_GATEWAY", "").strip().rstrip("/")
                out = (gw + "/ipfs/" if gw else GATEWAY_PINATA) + ipfs_hash
                return out
            last = f"{url} answered {resp.status_code}: {str(resp.text or '')[:120]}"
        else:
            out = (resp.text or "").strip()
            if resp.status_code == 200 and out.startswith("https://") \
                    and not any(c.isspace() for c in out):
                return out
            last = f"{url} answered {resp.status_code}: {out[:120]}"
        print(f"[upload] {last}", flush=True)
    raise UploadError(f"image/metadata upload failed ({last})")


def build_metadata_json(
    name: str,
    symbol: str,
    description: str,
    image_url: str,
    external_url: str,
    attributes: list,
    seller_fee_bps: int = 500,
) -> bytes:
    """Standard Metaplex off-chain metadata file (the bot's metadata.json shape)."""
    payload = {
        "name": name,
        "symbol": symbol,
        "description": description,
        "image": image_url,
        "external_url": external_url,
        "seller_fee_basis_points": int(seller_fee_bps),
        "attributes": list(attributes or []),
        "properties": {
            "category": "image",
            "files": [{"uri": image_url, "type": "image"}],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_data_image(data_url: str) -> tuple[str, bytes]:
    """Parse a data:image/...;base64 payload into (mime, raw bytes)."""
    head, _, b64 = str(data_url or "").partition(";base64,")
    if not head.startswith("data:image/") or not b64:
        raise MintError("image must be a PNG/JPG/WEBP/GIF file or an https:// image URL")
    if len(b64) > MAX_IMAGE_BASE64:
        raise MintError(f"image too large — {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB max")
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:
        raise MintError("image is not valid base64 data") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise MintError(f"image too large — {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB max")
    mime = head[5:].split(";")[0].strip().lower()
    if mime not in ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/svg+xml"):
        raise MintError(f"unsupported image type {mime or '(unknown)'}")
    return mime, raw


# ---- reading NFTs back: gateways + off-chain JSON ------------------------------

def resolve_uri(uri: str) -> str | None:
    """Map ipfs:// and ar:// URIs onto public https gateways; https passes through."""
    raw = (uri or "").strip()
    if not raw:
        return None
    if raw.startswith("ipfs://"):
        rest = raw[7:].lstrip("/")
        if rest.startswith("ipfs/"):
            rest = rest[5:]
        return GATEWAY_IPFS + rest
    if raw.startswith("ar://"):
        return GATEWAY_ARWEAVE + raw[5:]
    if raw.startswith(("https://", "http://")):
        return raw
    return None


def fetch_uri_json(uri: str, timeout: float = 8.0) -> dict | None:
    """Download a NFT/metadata JSON file with the app's strict limits
    (HTTPS-only, no redirects, 8s timeout, 1 MiB cap, JSON object only)."""
    if not uri or not uri.startswith("https://"):
        return None
    try:
        resp = requests.get(
            uri,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": "OneNess-Wallet/1.0"},
        )
        if resp.status_code != 200:
            return None
        content = resp.raw.read(1_000_001, decode_content=True)
        if len(content) > 1_000_000:
            return None
        obj = json.loads(content.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None

# ---- the minting bot: one transaction, fully on-chain ------------------------

def mint_nft(
    rpc,
    owner_keypair,
    *,
    name: str,
    symbol: str,
    uri: str,
    seller_fee_bps: int = 500,
    confirm: bool = True,
    fee_wallet: str | None = None,
    fee_lamports: int = 0,
) -> dict:
    """Mint one 1-of-1 NFT owned by the payer — the `sol mint` bot pipeline
    (create mint → init → ATA → mint-to → Metaplex metadata) in one tx.

    When ``fee_lamports`` > 0, a flat mint fee is transferred to ``fee_wallet``
    inside the SAME transaction (atomic — the mint cannot land without it),
    the balance guard includes it, and the result reports the fee for the
    fee_ledger row the caller records."""
    owner = owner_keypair.pubkey()
    mint_kp = Keypair()                     # fresh mint account per NFT
    mint = mint_kp.pubkey()
    ata = derive_ata(owner, mint)
    metadata_acct = Pubkey.find_program_address(
        [b"metadata", bytes(METADATA_PROGRAM), bytes(mint)], METADATA_PROGRAM
    )[0]

    fee_pub: Pubkey | None = None
    if fee_lamports:
        if fee_lamports < 0 or not fee_wallet:
            raise MintError("the mint fee needs a positive lamport amount and a fee wallet")
        try:
            fee_pub = Pubkey.from_string(fee_wallet)
        except Exception as exc:
            raise MintError("the configured NFT mint fee wallet address is invalid") from exc

    rent_mint = rpc.get_rent_lamports(MINT_ACCOUNT_SPACE)
    rent_token = rpc.get_rent_lamports(TOKEN_ACCOUNT_SPACE)
    rent_meta = rpc.get_rent_lamports(METADATA_APPROX_SPACE)
    balance = rpc.get_balance(str(owner))
    needed = rent_mint + rent_token + rent_meta + SIG_FEE_LAMPORTS
    if fee_pub is not None:
        needed += fee_lamports
    if balance < needed:
        mint_fee_txt = (
            f" + the {fee_lamports / 1e9:.4f} SOL mint fee"
            if fee_pub is not None
            else ""
        )
        raise MintError(
            f"wallet balance {balance / 1e9:.4f} SOL is below what minting needs: "
            f"~{needed / 1e9:.4f} SOL (mint rent + token account rent + metadata "
            f"rent + fees{mint_fee_txt})"
        )

    instructions = [
        create_account_ix(owner, mint, rent_mint, MINT_ACCOUNT_SPACE, TOKEN_PROGRAM_ID),
        initialize_mint_ix(mint, 0, owner, owner),
        create_ata_idempotent_ix(owner, owner, mint)[0],
        mint_to_ix(mint, ata, owner, 1),
        create_metadata_accounts_v3_ix(
            mint=mint,
            mint_authority=owner,
            payer=owner,
            update_authority=owner,
            name=name,
            symbol=symbol,
            uri=uri,
            seller_fee_bps=seller_fee_bps,
            creators=[(owner, True, 100)],
            is_mutable=True,
        ),
    ]
    if fee_pub is not None:
        instructions.append(system_transfer_ix(owner, fee_pub, fee_lamports))
    tx = tx_mod.build_and_sign(rpc, instructions, owner_keypair, extra_signers=[mint_kp])
    signature = (
        tx_mod.send_and_confirm(rpc, tx) if confirm else rpc.send_transaction(bytes(tx))
    )
    out = {
        "signature": signature,
        "mint": str(mint),
        "token_account": str(ata),
        "metadata_account": str(metadata_acct),
        "name": name,
        "symbol": symbol,
        "metadata_uri": uri,
        "explorer_url": f"https://explorer.solana.com/address/{mint}",
        "solscan_url": f"https://solscan.io/token/{mint}",
    }
    if fee_pub is not None:
        out["fee_raw"] = fee_lamports
        out["fee_wallet"] = str(fee_pub)
    return out

# @@PART3@@
# ---- the AI-NFT chatbox side: bundle a mint + build the system prompt --------

_BUNDLE_CACHE: dict = {}
_BUNDLE_CACHE_TTL = 600.0
_BUNDLE_LOCK = threading.Lock()


def nft_bundle(rpc, mint_str: str, timeout: float = 8.0) -> dict:
    """Everything the AI-NFT chatbox needs, best-effort with the app's usual
    limits: on-chain Metaplex PDA (name/symbol/uri) then the off-chain JSON
    (description, image, attributes). Cached per mint (10 min TTL)."""
    mint_str = (mint_str or "").strip()
    try:
        Pubkey.from_string(mint_str)
    except Exception as exc:
        raise MintError(f"'{mint_str or '(empty)'}' is not a valid Solana address") from exc

    with _BUNDLE_LOCK:
        hit = _BUNDLE_CACHE.get(mint_str)
    if hit is not None and time.monotonic() - hit[0] < _BUNDLE_CACHE_TTL:
        return dict(hit[1])

    info = {
        "mint": mint_str,
        "name": "",
        "symbol": "",
        "decimals": None,
        "supply": None,
        "is_nft": False,
        "metadata_url": "",
        "image": "",
        "description": "",
        "attributes": [],
        "external_url": "",
        "animation_url": "",
        "source": "none",
    }

    acc = rpc.get_account_info(mint_str)
    if acc is None:
        raise MintError(f"mint {mint_str} does not exist on this network")
    if str(acc.get("owner", "")) != str(TOKEN_PROGRAM_ID):
        raise MintError(
            f"{mint_str} exists but is not an SPL token mint (owner "
            f"{str(acc.get('owner', 'none'))[:12]}…) — the AI-NFT chatbox loads "
            "SPL token mints (NFT mint addresses)"
        )
    try:
        data = acc["data"]
        if len(data) >= 45:
            info["decimals"] = int(data[44])
            info["supply"] = struct.unpack("<Q", data[36:44])[0]
    except MintError:
        raise
    except Exception as exc:
        raise MintError(f"mint {mint_str} lookup failed: {exc}") from exc
    if not (info["supply"] or 0) >= 1:
        raise MintError(f"mint {mint_str} has no supply — nothing loaded, nothing to chat with")
    # A true 1-of-1: 0 decimals, supply exactly 1 (informational for the UI).
    info["is_nft"] = info["decimals"] == 0 and info["supply"] == 1

    onchain = meta_mod.fetch_onchain_metadata(rpc, mint_str)
    if onchain:
        info["name"] = onchain["name"].strip()
        info["symbol"] = onchain["symbol"].strip()
        info["source"] = "metaplex"
        resolved = resolve_uri(onchain["uri"])
        if resolved and resolved.startswith("https://"):
            info["metadata_url"] = resolved
            obj = fetch_uri_json(resolved, timeout=timeout)
            if obj:
                info["source"] = "metaplex+json"
                info["description"] = str(obj.get("description") or "")
                image = resolve_uri(str(obj.get("image") or obj.get("picture") or ""))
                info["image"] = image or str(obj.get("image") or "")
                info["animation_url"] = resolve_uri(str(obj.get("animation_url") or "")) or ""
                ext = resolve_uri(str(obj.get("external_url") or ""))
                info["external_url"] = ext or ""
                attrs = obj.get("attributes")
                if isinstance(attrs, list):
                    clean = []
                    for a in attrs[:MAX_ATTRIBUTES]:
                        if isinstance(a, dict) and (a.get("trait_type") or a.get("value")):
                            clean.append({
                                "trait_type": str(a.get("trait_type") or "Trait")[:64],
                                "value": str(a.get("value") or "")[:64],
                            })
                    info["attributes"] = clean

    if not info["name"]:
        # Non-NFT tokens (no Metaplex PDA): fall back to Jupiter's index so the
        # chatbox still works for any SPL token mint.
        try:
            results = meta_mod.search_tokens(mint_str, limit=1)
            if results and results[0]["mint"] == mint_str:
                info["name"] = results[0]["name"]
                info["symbol"] = results[0]["symbol"] or info["symbol"]
                info["source"] = "jupiter"
        except Exception:
            pass

    with _BUNDLE_LOCK:
        _BUNDLE_CACHE[mint_str] = (time.monotonic(), dict(info))
    return info


def build_nft_system_prompt(nft: dict, token_id: str | None = None) -> str:
    """The AI-NFT persona prompt — the metadata description is injected next
    to "You are" as the dynamic identity field, exactly like the original app."""
    name = (nft.get("name") or "").strip() or "this NFT"
    symbol = (nft.get("symbol") or "").strip()
    description = (nft.get("description") or "").strip()
    identity = description or (f"{name} (${symbol})" if symbol else name)

    lines = [f"You are {identity}.", ""]
    lines += [
        "You are the living, on-chain spirit of this Solana NFT, chatting inside",
        "the OneNess wallet's AI-NFT chatbox. Stay fully in character: you embody",
        "the artwork and its creator's intent. Answer questions about yourself,",
        "your story, your traits and your world warmly and vividly. Keep replies",
        "short (2-6 sentences) unless the human asks for more. You are not a",
        "generic assistant — you are this NFT.",
        "",
        "Identity card (from the NFT's on-chain + off-chain metadata):",
        f"- Name: {name}" + (f" (${symbol})" if symbol else ""),
        f"- Mint address: {nft.get('mint', '')}",
    ]
    if token_id:
        lines.append(f"- Token ID / edition #: {token_id}")
    if description and description not in identity:
        lines.append(f"- Description: {description}")
    traits = [a for a in (nft.get("attributes") or []) if isinstance(a, dict)][:12]
    if traits:
        lines.append(
            "- Traits: "
            + ", ".join(
                f"{a.get('trait_type') or 'Trait'}: {a.get('value')}" for a in traits
            )
        )
    if nft.get("image"):
        lines.append(f"- Image (already displayed in the chatbox header): {nft['image']}")
    if nft.get("external_url"):
        lines.append(f"- External link: {nft['external_url']}")
    lines += [
        "",
        "Never give financial advice about buying or selling — be the artwork.",
    ]
    return "\n".join(lines)
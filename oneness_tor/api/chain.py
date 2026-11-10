"""Chain endpoints — a faithful, envelope-preserving window onto the Rust node."""
from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from core import coin, ledger_host, ollama, pricing
from core.config import settings
from core.errors import ChainUnavailable, ValidationError
from core.node_client import node
from core.validator import validate_address
from oneness_tor.services import (base_url, node_sync, privacy, tor_service,
                                  unwrap)
from oneness_tor.services.auth import (is_admin_request, require_admin,
                                       require_chain_admin, token_from_request)

bp = Blueprint("chain_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _limit(default: int = 50) -> int:
    raw = request.args.get("limit", default)
    try:
        return max(1, min(500, int(raw)))
    except (TypeError, ValueError):
        return default


@bp.get("/health")
def health():
    reachable = node.is_reachable()
    height, chain_id = None, None
    if reachable:
        info = node.info()
        if info.get("ok"):
            height = info["data"].get("height")
            chain_id = info["data"].get("chain_id")
    runtime = ledger_host.status()
    onion = tor_service.node_status()
    return envelope({"status": "ok" if reachable else "degraded",
                     "service": "oneness-tor-api", "version": "1.0.0",
                     "height": height, "chain_id": chain_id,
                     "node_reachable": reachable, "node_url": node.base_url,
                     # what this process did about the ledger, and what to do if
                     # it is down — a health check that hides the reason is a lie
                     "node_runtime": runtime,
                     "node_hint": None if reachable else runtime.get("hint"),
                     # the onion this deployment is browsed at (never probed here,
                     # so health stays fast) — a Render service that is public but
                     # has no onion says so instead of looking finished
                     "onion": onion.get("url"),
                     "tor": {"published": onion.get("published"),
                             "key_set": onion.get("key_set"),
                             "auto_start": onion.get("auto_start"),
                             "control_port": int(settings.get("tor.control_port")
                                                 or 9051)},
                     # configuration facts only (no probe, so health stays fast):
                     # a hosted brain with a key is the way a small box answers
                     "ollama": {"api_url": ollama.base_url(),
                                "remote": not ollama.is_local(),
                                "api_key": bool(ollama.api_key()),
                                "model": ollama.configured_model()},
                     "coin": coin.as_dict()})


@bp.get("/coin")
def get_coin():
    """Always answers: node metadata when live, the pinned constants otherwise."""
    try:
        reply = node.coin()
        if reply.get("ok"):
            return envelope(reply["data"], source="rust-node")
    except ChainUnavailable:
        pass
    return envelope(coin.metadata(), source="python-twin", node_reachable=False)


@bp.get("/pricing")
def get_pricing():
    """The price table: the node's own copy, with this panel's prices on top.

    ``torchain`` ships the LOCKED table in Rust and answers first, but the admin
    panel reprices in Python (``data/admin/pricing.json``). Overlaying here keeps
    one number everywhere: the page, the quote and the charge.
    """
    try:
        reply = node.pricing()
        if reply.get("ok"):
            data = reply["data"] if isinstance(reply.get("data"), dict) else {}
            if isinstance(data.get("rows"), list):
                data = {**data, "rows": pricing.apply(data["rows"])}
            return envelope(data, source="rust-node")
    except ChainUnavailable:
        pass
    return envelope({"rows": pricing.table(), "coin": coin.as_dict(),
                     "native_coin_id": "TOR111111111111111111111111111111onion",
                     "fees": {"base_layer_tx_fee_micro": 0,
                              "base_layer_tx_fee_tor": 0.0,
                              "note": ("the chain charges no transaction fee; "
                                       "rows are the node/platform fee schedule")},
                     "node_reachable": False}, source="python-twin")


@bp.get("/chain/info")
def chain_info():
    return envelope(unwrap(node.info()))


@bp.get("/chain/verify")
def chain_verify():
    return envelope(unwrap(node.verify()))


@bp.get("/chain/sync")
def chain_sync():
    info = unwrap(node.info())
    peers = settings_peers()
    replication = node_sync.status()
    onion = tor_service.node_url()
    return envelope({
        "mode": "single-node-poa",
        "role": replication["role"],
        "origin": replication["url"] or None,
        "replication": replication,
        "peers": peers,
        "local_height": info.get("height"),
        "head_hash": info.get("head_hash"),
        "genesis_hash": info.get("genesis_hash"),
        "total_supply": info.get("total_supply"),
        "endpoints": {
            "base_url": base_url.request_base(request),
            "onion": onion or None,
            "snapshot": "/api/chain/sync/snapshot",
            "pull": "/api/chain/sync/pull",
            "blocks": "/api/chain/blocks",
            "block": "/api/chain/block/{ref}",
            "export": "/api/admin/sync/export (Rust node, admin)",
            "import": "/api/admin/sync/import (Rust node, admin)",
        },
        "catch_up": {"endpoint": "/api/chain/blocks", "from_param": "from",
                     "to_param": "to", "max_blocks_per_call": 500},
        "submit_endpoint": "/api/chain/tx",
        "note": ("one proposer, verified replicas: this node proposes blocks; a "
                 "replica pulls GET /api/chain/sync/snapshot, re-hashes every "
                 "block and the state root, then imports it into its own Rust "
                 "ledger with POST /api/admin/sync/import. Multi-proposer gossip "
                 "is not implemented in torchain yet")})


@bp.get("/chain/sync/snapshot")
def chain_sync_snapshot():
    """The whole verifiable ledger snapshot, for a replica to pull (admin)."""
    token = require_chain_admin(request)
    return envelope(node_sync.snapshot(token))


@bp.post("/chain/sync/pull")
def chain_sync_pull():
    """Replica: pull + verify the origin's snapshot, then import it here."""
    token = require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(node_sync.pull(
        force=bool(payload.get("force")),
        origin_url=payload.get("origin") or None,
        local_token=token,
        origin_token=payload.get("origin_token") or None))


@bp.get("/chain/sync/audit")
def chain_sync_audit():
    """Re-hash every block the node serves, walk prev_hash links, probe peers."""
    return envelope(node_sync.audit())


@bp.get("/chain/sync/push")
def chain_sync_push_status():
    """The push wiring: targets, interval, where the snapshot is saved, last run."""
    require_admin(request)
    return envelope(node_sync.push_status())


@bp.post("/chain/sync/push")
def chain_sync_push():
    """Origin: export + save + POST this ledger's snapshot to every target (admin)."""
    token = require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(node_sync.push(
        targets=payload.get("targets") or None,
        save=payload.get("save") if isinstance(payload.get("save"), bool) else None,
        force=bool(payload.get("force")), token=token))


@bp.post("/chain/sync/accept")
def chain_sync_accept():
    """Replica: accept a snapshot pushed by the origin — verify it, then import.

    Same door a pull uses, same hashes verified: the body is
    ``{"snapshot": {...}, "force": false}`` (a bare snapshot is accepted too).
    """
    token = require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    document = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) \
        else payload
    return envelope(node_sync.accept(
        document, force=bool(payload.get("force")), local_token=token,
        source=str(payload.get("origin") or "pushed snapshot")))


def settings_peers() -> list[dict]:
    from core.config import settings
    raw = settings.get("chain.peers") or []
    peers = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            peers.append({"url": item, "reachable": False})
        elif isinstance(item, dict):
            peers.append({**item, "reachable": bool(item.get("reachable", False))})
    return peers


@bp.get("/chain/blocks")
def chain_blocks():
    start = request.args.get("from", type=int) or 0
    end = request.args.get("to", type=int)
    data = unwrap(node.blocks(start, end))
    blocks = data if isinstance(data, list) else data.get("blocks", [])
    return envelope({"blocks": blocks, "from": start,
                     "to": end if end is not None else (blocks[-1]["height"]
                                                        if blocks else start),
                     "count": len(blocks)})


@bp.get("/chain/block/<ref>")
def chain_block(ref: str):
    return envelope(unwrap(node.block(ref)))


@bp.get("/chain/tx/<txid>")
def chain_tx(txid: str):
    """One record. A wallet that chose ANONYMOUS shows the label, not its address.

    The owner (wallet session or chain admin token) still reads the unmasked
    record — it is their own address, and the operator already has the ledger on
    disk, so pretending otherwise would be theatre.
    """
    token = token_from_request(request)
    reply = node.tx(txid, token=token)
    if reply.get("ok"):
        record = reply.get("data") or {}
        if not is_admin_request(request):
            return envelope(privacy.public_view(record))
    return envelope(unwrap(reply))


@bp.get("/chain/events")
def chain_events():
    data = unwrap(node.events(_limit()))
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        data = {**data, "events": privacy.public_view_all(data["events"])}
    return envelope(data)


@bp.get("/public/records")
def public_records():
    data = unwrap(node.public_records(_limit()))
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        data = {**data, "records": privacy.public_view_all(data["records"])}
    return envelope(data)


@bp.post("/private/records")
def private_records():
    """Records an older release sealed when it still had a 30-day window.

    The window is gone: nothing is sealed any more. This admin-only read stays so
    that a chain written by an older node can still be audited in full.
    """
    token = require_chain_admin(request)
    return envelope(unwrap(node.private_records(token=token, limit=_limit())))


@bp.get("/account/<address>")
def account(address: str):
    """A public balance read: every address's balances are public ledger data."""
    validate_address(address, "address")
    return envelope(unwrap(node.account(address)))


@bp.get("/account/<address>/nfts")
def account_nfts(address: str):
    """That address's unique editions — the wallet's NFT HOLDINGS pane, public.

    Same rule as ``/api/account/<address>/holdings``: balances are public ledger
    data, and a wallet that holds nothing answers an empty list rather than a 404,
    because the pane has to open for a brand-new address.
    """
    validate_address(address, "address")
    return envelope(unwrap(node.account_nfts(address)))


# ------------------------------------------------- NFT ownership & transfers
@bp.get("/chain/nft")
def chain_nft_catalogue():
    """Every unique edition on the chain, with the holder of its whole supply."""
    offset = max(0, int(request.args.get("offset", 0, type=int) or 0))
    return envelope(unwrap(node.nfts(_limit(200), offset)))


@bp.get("/chain/nft/history")
def chain_nft_history():
    """Every NFT mint and transfer, newest first — public, and **masked**.

    An anonymous transfer masks its sender here exactly as it does on
    ``GET /api/chain/tx/<txid>``: ``privacy.public_view`` stays the only place that
    rule is written (§19.10), and ``counterparty`` is recomputed from the masked
    fields so this new door cannot become the side door §19.10 closed. A mint's
    ``external_ref`` is the metadata URI, not a privacy choice, so a mint is never
    anonymous — but the rule decides that, not this comment.
    """
    address = (request.args.get("address") or "").strip() or None
    symbol = ((request.args.get("symbol") or request.args.get("token")
               or "").strip() or None)
    data = unwrap(node.nft_history(address, symbol, _limit(200)))
    records = data.get("records")
    if isinstance(records, list):
        admin = is_admin_request(request)
        rows = []
        for row in records:
            if not isinstance(row, dict):
                continue
            view = dict(row) if admin else privacy.public_view(row)
            direction = str(view.get("direction") or "")
            # recomputed from the *masked* fields: masking `from` alone would
            # leave the address sitting in the neighbour field
            if direction in ("out", "self"):
                view["counterparty"] = str(view.get("to") or "")
            elif direction == "in":
                view["counterparty"] = str(view.get("from") or "")
            rows.append(view)
        data["records"] = rows
    return envelope(data)


@bp.get("/chain/nft/<symbol>")
def chain_nft(symbol: str):
    """Ownership lookup for one edition: holder now, creator, metadata, last move."""
    data = unwrap(node.nft(symbol))
    if not is_admin_request(request) and isinstance(data, dict):
        last = data.get("last_transfer")
        if isinstance(last, dict) and last:
            view = privacy.public_view(last)
            data["last_transfer"] = {**last, "from": view["from"]}
        mint = data.get("mint")
        if isinstance(mint, dict) and mint:
            # a mint's external_ref is the metadata document, so this reads
            # "public" unless the ledger says otherwise
            view = privacy.public_view({
                "from": mint.get("from"),
                "external_ref": mint.get("metadata_uri") or ""})
            data["mint"] = {**mint, "from": view["from"]}
    return envelope(data)


@bp.get("/tokens")
def tokens():
    """Every token on the ledger, each row carrying its logo and mint time.

    The extra fields come from public metadata (`/api/tokens/images`), never from
    the ledger: the chain keeps the ticker, the supply, the owner and the mint
    time, and the picture is resolved the same way every other page resolves it.
    """
    from oneness_tor.services import token_media

    rows = unwrap(node.tokens())
    if isinstance(rows, dict):
        token_media.decorate([row for row in rows.values()
                              if isinstance(row, dict)])
    return envelope(rows)


@bp.get("/chain/nft/tx/<txid>")
def chain_nft_receipt(txid: str):
    """The transfer receipt: block, confirmations, owner now, destination holds.

    The receipt carries the sender's address inside ``transfer.from``, so the
    public door masks it with the same rule as every other record; the admin door
    reads the ledger as it is.
    """
    token = token_from_request(request)
    reply = node.nft_receipt(txid, token=token)
    if reply.get("ok") and not is_admin_request(request):
        data = reply.get("data") or {}
        transfer = data.get("transfer") if isinstance(data.get("transfer"), dict) else {}
        if transfer:
            view = privacy.public_view({
                "from": transfer.get("from"),
                "to": transfer.get("to"),
                "visibility": transfer.get("visibility"),
                "external_ref": transfer.get("external_ref"),
                "kind": data.get("kind"), "txid": data.get("txid")})
            data = {**data,
                    "transfer": {**transfer, "from": view["from"]},
                    "privacy": view["privacy"], "anonymous": view["anonymous"],
                    "masked_fields": view["masked_fields"]}
        return envelope(data)
    return envelope(unwrap(reply))


@bp.post("/chain/nft/transfer")
def submit_nft_transfer():
    """Submit a signed ``nft_transfer``; the node owns every rule, this door owns none.

    Ownership, the signature, the nonce and the supply check all live in
    ``chain.rs`` (``tx_nft_transfer``). This route exists so a wallet has a
    documented door with the *shape* checked up front: the kind must be
    ``nft_transfer``, so a caller can never mistake it for the coin sender.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("a signed transaction JSON object is required")
    if str(payload.get("kind") or "").strip().lower() != "nft_transfer":
        raise ValidationError(
            "this door only moves a unique edition: kind must be nft_transfer, got "
            f"{payload.get('kind')!r} — send every other kind to POST /api/chain/tx")
    node_sync.require_writable()
    data = unwrap(node.nft_transfer(payload))
    receipt = data.get("receipt") if isinstance(data, dict) else None
    if (isinstance(receipt, dict) and not is_admin_request(request)
            and isinstance(receipt.get("transfer"), dict)):
        transfer = receipt["transfer"]
        view = privacy.public_view({
            "from": transfer.get("from"), "to": transfer.get("to"),
            "visibility": transfer.get("visibility"),
            "external_ref": transfer.get("external_ref"),
            "kind": receipt.get("kind"), "txid": receipt.get("txid")})
        data = {**data, "receipt": {**receipt,
                                    "transfer": {**transfer, "from": view["from"]},
                                    "privacy": view["privacy"],
                                    "anonymous": view["anonymous"]}}
    return envelope(data)


@bp.get("/pools")
def pools():
    return envelope(unwrap(node.pools()))


@bp.post("/chain/tx")
def submit_tx():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("a signed transaction JSON object is required")
    node_sync.require_writable()
    return envelope(unwrap(node.submit_tx(payload)))

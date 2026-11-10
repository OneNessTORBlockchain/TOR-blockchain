"""Node synchronization — verify the chain, and replicate it honestly.

``GET /api/chain/sync`` reports the wiring; this module *verifies* and *moves* it:

1. fetch the whole block chain from the Rust node over HTTP,
2. re-compute every block digest with the exact payload the ledger hashes
   (``serde_json::to_string`` of ``{height, prev_hash, timestamp, proposer,
   state_root, txids}`` — keys sorted, compact — then sha3-256),
3. walk the ``prev_hash`` links, compare block 0 with ``genesis_hash`` and the
   newest block with ``head_hash``, and check the block count against ``height``,
4. probe every peer in ``chain.peers`` and report how far ahead it is,
5. **replicate**: a replica pulls the origin's verifiable ledger snapshot
   (``GET /api/chain/sync/snapshot`` → the origin's Rust node
   ``GET /api/admin/sync/export``), re-checks every hash and the state root in
   Python, then hands it to the local Rust node's
   ``POST /api/admin/sync/import``.
6. **push**: the origin does the same in the other direction on a timer — it
   exports its own snapshot, keeps a copy in ``data/snapshots`` and POSTs it to
   every ``sync.push.targets`` entry (a Render node, a node 2, an onion node),
   which verifies it again with ``POST /api/chain/sync/accept`` before touching
   its ledger. Pushing the *verified* snapshot is what makes the mirroring
   automatic without inventing gossip: a target can never install a ledger it
   did not re-hash, and it is never rolled backwards.

The verdict is honest: ``in-sync``, ``drift`` or ``node-down``. Nothing is ever
reported green because the process is up — the hashes have to agree. There is
still exactly one proposer (single-node PoA); replicas are verified read-only
mirrors, which is what lets a Render node, a local node and an onion node show
the same tokens without inventing a consensus protocol.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from core.config import ROOT, settings
from core.errors import AppError, ChainUnavailable, ValidationError
from core.node_client import node
from oneness_tor.services import security_module

PAGE = 500
MAX_PAGES = 40
DIGEST_FIELDS = ("height", "prev_hash", "timestamp", "proposer", "state_root",
                 "txids")
SNAPSHOT_STANDARD = "oneness-tor-ledger-snapshot/1"
SNAPSHOT_KEEP = 5

_LOCK = threading.Lock()
_LAST: dict = {"at": None, "ok": None, "reason": "no pull attempted yet"}
_PUSH_LAST: dict = {"at": None, "ok": None, "reason": "no push attempted yet",
                    "targets": []}

# The committed fleet wiring (who proposes, who mirrors) — see fleet().
FLEET_PATH = ROOT / "config.fleet.yaml"
_FLEET_CACHE: dict = {"mtime": None, "data": {}}


def block_digest(block: dict) -> str:
    """The ledger's block hash: sha3-256 over the canonical JSON payload."""
    payload = {key: block.get(key) for key in DIGEST_FIELDS}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha3_256(text.encode("utf-8")).hexdigest()


def state_root_of(state: dict) -> str:
    """``chain.rs``'s ``state_root_of`` — the only way to prove a snapshot."""
    lines: list[str] = []
    accounts = state.get("accounts") or {}
    for address in sorted(accounts):
        account = accounts.get(address) or {}
        tokens = account.get("tokens") or {}
        rendered = ",".join(f"{symbol}={int(tokens[symbol] or 0)}"
                            for symbol in sorted(tokens))
        lines.append(f"a|{address}|{int(account.get('balance') or 0)}"
                     f"|{int(account.get('nonce') or 0)}|{rendered}")
    for symbol in sorted(state.get("tokens") or {}):
        token = (state.get("tokens") or {}).get(symbol) or {}
        lines.append(f"t|{symbol}|{int(token.get('supply') or 0)}"
                     f"|{int(token.get('decimals') or 0)}|{token.get('owner') or ''}")
    for symbol in sorted(state.get("pools") or {}):
        pool = (state.get("pools") or {}).get(symbol) or {}
        line = (f"p|{symbol}|{int(pool.get('tor_reserve') or 0)}"
                f"|{int(pool.get('token_reserve') or 0)}"
                f"|{int(pool.get('fee_bps') or 0)}")
        # ``chain.rs::state_root_of`` was extended after 122 pools were already
        # sealed: only a pool that carries a venue/creator/manager appends those
        # three fields, so the pre-venue state still hashes to the root its blocks
        # recorded. This mirror has to stay exact — a replica proves the state
        # against the head block's state_root before importing a byte.
        venue = str(pool.get("venue") or "")
        creator = str(pool.get("creator") or "")
        manager = str(pool.get("manager") or "")
        if venue or creator or manager:
            line += f"|{venue}|{creator}|{manager}"
        lines.append(line)
    lines.append(f"s|{int(state.get('total_supply') or 0)}"
                 f"|{int(state.get('height') or 0)}")
    return hashlib.sha3_256("\n".join(lines).encode("utf-8")).hexdigest()


def verify_snapshot(snapshot: dict) -> dict:
    """Re-hash a snapshot before any byte of it is allowed to touch this node."""
    if not isinstance(snapshot, dict):
        raise ValidationError("a ledger snapshot must be a JSON object")
    state = snapshot.get("state")
    if not isinstance(state, dict):
        raise ValidationError("snapshot.state is required")
    raw_blocks = snapshot.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ValidationError("snapshot.blocks must be a non-empty array")

    blocks: list[dict] = []
    for index, item in enumerate(raw_blocks):
        block = (item.get("block")
                 if isinstance(item, dict) and isinstance(item.get("block"), dict)
                 else item)
        if not isinstance(block, dict):
            raise ValidationError(f"snapshot.blocks[{index}] is not a block")
        if int(block.get("height") or 0) != index:
            raise ValidationError(
                f"snapshot.blocks[{index}] has height {block.get('height')} "
                f"(expected {index})")
        blocks.append(block)

    seen: dict[str, tuple[int, str]] = {}
    for record in state.get("records") or []:
        if isinstance(record, dict) and record.get("txid"):
            seen[str(record["txid"])] = (int(record.get("height") or 0),
                                         str(record.get("block_hash") or ""))

    previous = "0" * 64
    for block in blocks:
        height = int(block.get("height") or 0)
        if str(block.get("prev_hash")) != previous:
            raise ValidationError(f"block {height} does not link to its predecessor")
        if block_digest(block) != str(block.get("hash")):
            raise ValidationError(f"block {height} stored hash does not re-hash")
        for txid in block.get("txids") or []:
            placed = seen.get(str(txid))
            if placed is None:
                raise ValidationError(
                    f"block {height} lists {txid} but state.records has no "
                    f"such record")
            if placed != (height, str(block.get("hash"))):
                raise ValidationError(
                    f"block {height} lists {txid} but the state places it elsewhere")
        previous = str(block.get("hash"))

    head = blocks[-1]
    if str(blocks[0].get("hash")) != str(state.get("genesis_hash")):
        raise ValidationError("block 0 hash is not state.genesis_hash")
    if previous != str(state.get("head_hash")):
        raise ValidationError("the newest block hash is not state.head_hash")
    if int(state.get("height") or 0) != int(head.get("height") or 0):
        raise ValidationError(
            f"state.height is {state.get('height')} but the newest block is "
            f"{head.get('height')}")
    if state_root_of(state) != str(head.get("state_root")):
        raise ValidationError(
            "state does not hash to the head block's state_root — the snapshot "
            "is not the ledger it claims to be")
    if int(state.get("total_supply") or 0) <= 0:
        raise ValidationError("state.total_supply is zero")
    # The rules the blocks cannot see: every record's privacy marker is checked
    # against the signed policy here, so a snapshot that hashes perfectly but
    # carries a contradictory or malformed marker never reaches the ledger.
    privacy_counts = security_module.verify_records(
        state.get("records"), policy=security_module.require()["policy"])
    return {"state": state, "blocks": blocks,
            "height": int(head.get("height") or 0),
            "head_hash": str(head.get("hash")),
            "genesis_hash": str(state.get("genesis_hash") or ""),
            "tokens": len(state.get("tokens") or {}),
            "accounts": len(state.get("accounts") or {}),
            "records": len(state.get("records") or []),
            "transactions": len(seen), "privacy": privacy_counts}


def _fetch(from_height: int, to_height: int) -> list[dict]:
    reply = node.blocks(from_height, to_height)
    data = reply.get("data") if isinstance(reply, dict) else None
    rows = data.get("blocks") if isinstance(data, dict) else data
    return [row for row in (rows or []) if isinstance(row, dict)]


def peers(local_height: int | None = None) -> list[dict]:
    """Probe every configured peer's ``/api/chain/info`` (real HTTP, short timeout)."""
    raw = settings.get("chain.peers") or []
    rows: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        url = str((item.get("url") if isinstance(item, dict) else item) or "").strip()
        if not url:
            continue
        entry: dict = {"url": url, "reachable": False}
        try:
            request = urllib.request.Request(
                url.rstrip("/") + "/api/chain/info",
                headers={"accept": "application/json"})
            with urllib.request.urlopen(request, timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            data = payload.get("data") if isinstance(payload, dict) else {}
            data = data if isinstance(data, dict) else {}
            entry.update(reachable=True, chain_id=data.get("chain_id"),
                         height=data.get("height"),
                         head_hash=data.get("head_hash"))
            if local_height is not None and data.get("height") is not None:
                entry["ahead_by"] = max(0, int(data["height"]) - int(local_height))
                entry["behind_by"] = max(0, int(local_height) - int(data["height"]))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as problem:
            entry["error"] = str(getattr(problem, "reason", problem))[:200]
        rows.append(entry)
    return rows


def audit() -> dict:
    """Re-hash the served chain and report the real verdict (never raises)."""
    checked = int(time.time())
    try:
        info = (node.info() or {}).get("data") or {}
    except ChainUnavailable as problem:
        return {"verdict": "node-down", "node_reachable": False, "height": None,
                "blocks_checked": 0, "pages": 0,
                "errors": [problem.message],
                "error": {"code": problem.code, "message": problem.message},
                "node_url": node.base_url, "peers": peers(),
                "replication": status(),
                "checked_at": checked,
                "note": "start `torchain serve` — nothing can be verified without it"}
    height = int(info.get("height") or 0)
    blocks: list[dict] = []
    pages, cursor = 0, 0
    while cursor <= height and pages < MAX_PAGES:
        rows = _fetch(cursor, min(cursor + PAGE - 1, height))
        if not rows:
            break
        blocks.extend(rows)
        cursor = int(rows[-1].get("height") or cursor) + 1
        pages += 1
    blocks.sort(key=lambda row: int(row.get("height") or 0))

    errors: list[str] = []
    previous = "0" * 64
    for block in blocks:
        level = block.get("height")
        if str(block.get("prev_hash")) != previous:
            errors.append(f"height {level}: prev_hash does not link to the previous block")
        recomputed = block_digest(block)
        if recomputed != str(block.get("hash")):
            errors.append(
                f"height {level}: the stored hash does not re-hash "
                f"(stored {str(block.get('hash'))[:12]}…, recomputed {recomputed[:12]}…)")
        previous = str(block.get("hash"))
    if not blocks:
        errors.append("the node returned no blocks at all")
    else:
        if str(blocks[0].get("hash")) != str(info.get("genesis_hash")):
            errors.append("block 0 hash is not info.genesis_hash")
        if previous != str(info.get("head_hash")):
            errors.append("the newest block hash is not info.head_hash")
        if len(blocks) != height + 1:
            errors.append(f"fetched {len(blocks)} block(s) but the node reports "
                          f"height {height} (expected {height + 1})")
    peer_rows = peers(local_height=height)
    ahead = [row["url"] for row in peer_rows if int(row.get("ahead_by") or 0) > 0]
    return {"verdict": "drift" if errors else "in-sync", "node_reachable": True,
            "height": height, "head_hash": info.get("head_hash"),
            "genesis_hash": info.get("genesis_hash"),
            "blocks_checked": len(blocks), "pages": pages,
            "truncated": pages >= MAX_PAGES and cursor <= height,
            "errors": errors, "peers": peer_rows, "peers_ahead": ahead,
            "replication": status(),
            "catch_up": {"endpoint": "/api/chain/blocks", "from_param": "from",
                         "to_param": "to", "max_blocks_per_call": PAGE},
            "node_url": node.base_url, "checked_at": checked,
            "note": ("single-proposer proof-of-authority: this node re-hashes every "
                     "block it serves. A replica pulls a verified snapshot "
                     "(/api/chain/sync/snapshot → /api/admin/sync/import); there is "
                     "no multi-proposer gossip in torchain yet")}



# ------------------------------------------------------------------ replication
def _explicit(dotted: str, env: str):
    """The value a human actually set (the env var, or the YAML file), else None.

    ``settings.get`` cannot answer that — it falls back to the documented default.
    The fleet file is a *fallback*, so it must only fill in what nobody chose.
    """
    block, _, key = dotted.partition(".")
    value = os.environ.get(env)
    if value in (None, ""):
        section = settings.raw.get(block) if isinstance(settings.raw, dict) else None
        value = section.get(key) if isinstance(section, dict) else None
    return value if value not in (None, "") else None


def _interval() -> int:
    value = _explicit("sync.interval_seconds", "ONENESS_SYNC_INTERVAL")
    if value is None:
        value = fleet()["interval_seconds"]
    try:
        return max(15, int(value if value is not None else 60))
    except (TypeError, ValueError):
        return 60


def _auto() -> bool:
    value = _explicit("sync.auto", "ONENESS_SYNC_AUTO")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is not None:
        return bool(value)
    from_fleet = fleet()["auto"]
    return True if from_fleet is None else from_fleet


def _timeout() -> float:
    try:
        return max(5.0, float(settings.get("sync.timeout") or 30.0))
    except (TypeError, ValueError):
        return 30.0


def _proxy() -> str:
    return str(settings.get("sync.proxy") or os.environ.get("ONENESS_SYNC_PROXY")
               or "").strip()


def _sha3(text: str) -> str:
    return hashlib.sha3_256(text.encode("utf-8")).hexdigest()


def _host(url: object) -> str:
    """The lowercase host of a URL (``""`` when there is none)."""
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        return (urllib.parse.urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def self_hosts() -> dict[str, str]:
    """The hosts that are *this* deployment, and where each one was read from.

    One repository serves every node, so ``origin.url`` in the committed fleet
    file is also *this* node's public address on the node that proposes. Three
    sources can say which public host belongs to this process, and all of them
    require no code change per node:

    * ``ONENESS_PUBLIC_BASE_URL`` / ``paths.public_base_url`` — a pinned custom
      domain (``https://www.torblockchain.fun``);
    * ``RENDER_EXTERNAL_URL`` / ``RENDER_EXTERNAL_HOSTNAME`` — what Render itself
      injects into every web service;
    * a ``Host`` this process has actually answered for
      (:func:`oneness_tor.services.base_url.known_hosts`, written by
      ``base_url.remember`` on every request) — a custom domain in front of a
      Render service appears nowhere in the environment, but it does appear in
      the requests, and a host that reaches this process *is* this process.

    Loopback is ignored: ``127.0.0.1`` is never somebody's origin URL.
    """
    found: dict[str, str] = {}
    candidates = (
        (os.environ.get("ONENESS_PUBLIC_BASE_URL"), "ONENESS_PUBLIC_BASE_URL"),
        (settings.get("paths.public_base_url"), "paths.public_base_url"),
        (os.environ.get("RENDER_EXTERNAL_URL"), "RENDER_EXTERNAL_URL"),
        (os.environ.get("RENDER_EXTERNAL_HOSTNAME"), "RENDER_EXTERNAL_HOSTNAME"),
    )
    for value, source in candidates:
        host = _host(value)
        if host and host not in LOOPBACK_HOSTS and host not in found:
            found[host] = source
    from oneness_tor.services import base_url      # late: no import cycle
    for host in base_url.known_hosts():
        if host not in found:
            found[host] = "an observed request Host"
    return found


def is_self(url: object) -> str:
    """Why ``url`` is this very node (``""`` when it is somebody else).

    A node that pulled from its own public URL would at best import nothing and
    at worst demote itself to a read-only replica of itself and refuse to mint.
    """
    host = _host(url)
    if not host:
        return ""
    return self_hosts().get(host, "")


def fleet() -> dict:
    """The committed fleet wiring (``config.fleet.yaml``): one origin, many mirrors.

    Every deployment shares one repository, so "who proposes, who mirrors" belongs
    in the repository too: an empty ``ONENESS_SYNC_ORIGIN`` (or a stale dashboard
    value) left a node with ``role: origin`` and an origin of its own, which looks
    healthy until two nodes report different heights.

    The file supplies *defaults*, never an override — with one exception that
    cannot be left to a default: a node that does **not** hold the origin's chain
    credential (its own ``api.admin_token`` does not hash to
    ``origin.admin_token_sha256``) is a replica, even when ``ONENESS_NODE_ROLE``
    still says ``origin``. The node holding that token is the origin, and it never
    pulls from its own public URL. The same rule seen from the other side: a node
    whose own public host (``ONENESS_PUBLIC_BASE_URL`` / ``RENDER_EXTERNAL_URL``)
    *is* ``origin.url`` is the origin too, and also never pulls from itself — see
    :func:`self_hosts`.

    Returns ``{configured, origin_url, origin_token, origin_admin_token_sha256,
    genesis_hash, interval_seconds, auto, is_origin_node, self_by}``. A missing or
    unreadable file is simply "no fleet": a node must boot from env alone too.
    """
    empty = {"configured": False, "origin_url": "", "origin_token": "",
             "origin_admin_token_sha256": "", "genesis_hash": "",
             "interval_seconds": None, "auto": None, "is_origin_node": False,
             "self_by": None}
    try:
        stamp = FLEET_PATH.stat().st_mtime
    except OSError:
        return empty
    if _FLEET_CACHE.get("mtime") == stamp and _FLEET_CACHE.get("data"):
        data = dict(_FLEET_CACHE["data"])
    else:
        data = _read_fleet()
        if data is None:
            return empty
        _FLEET_CACHE.update(mtime=stamp, data=data)
        data = dict(data)
    # never cached: this is the credential check, and a process may load another
    # token between two calls (tests do; a restart always does)
    own = str(settings.admin_token or "").strip()
    digest = str(data.get("origin_admin_token_sha256") or "")
    by = ""
    if digest and own and _sha3(own) == digest:
        by = "admin token"
    elif data.get("origin_url"):
        by = is_self(data["origin_url"])
    data["is_origin_node"] = bool(data.get("origin_url") and by)
    data["self_by"] = by or None
    return data


def _read_fleet() -> dict | None:
    """Parse ``config.fleet.yaml`` (``None`` = unreadable, which means no fleet)."""
    try:
        loaded = yaml.safe_load(FLEET_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    origin_block = loaded.get("origin") if isinstance(loaded.get("origin"), dict) else {}
    replicas = loaded.get("replicas") if isinstance(loaded.get("replicas"), dict) else {}
    url = str(origin_block.get("url") or "").strip().rstrip("/")
    token = str(origin_block.get("token") or "").strip()
    digest = str(origin_block.get("admin_token_sha256") or "").strip().lower()
    interval = replicas.get("interval_seconds")
    if interval not in (None, ""):
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = None
    else:
        interval = None
    auto = replicas.get("auto")
    if isinstance(auto, str):
        auto = auto.strip().lower() in ("1", "true", "yes", "on")
    elif auto is not None:
        auto = bool(auto)
    return {"configured": bool(url), "origin_url": url, "origin_token": token,
            "origin_admin_token_sha256": digest,
            "genesis_hash": str(origin_block.get("genesis_hash") or "").strip(),
            "interval_seconds": interval, "auto": auto, "is_origin_node": False}


def origin() -> dict:
    """Who proposes the chain this node should mirror (``""`` = this node)."""
    conf = fleet()
    url = str(settings.get("sync.origin_url") or "").strip().rstrip("/")
    source = "config" if url else ""
    if not url and conf["configured"]:
        url, source = conf["origin_url"], "fleet"
    role = str(settings.get("sync.role") or "").strip().lower()
    if role not in ("origin", "replica"):
        role = "replica" if url else "origin"
    mine = is_self(url)
    if conf["is_origin_node"] or mine:
        # this node holds the origin's chain credential — or its own public URL
        # *is* the origin's (the committed fleet file names it for everybody, so
        # without this check the proposer would demote itself to a read-only
        # replica of itself and refuse to mint). It proposes; it never pulls.
        url, source, role = "", "", "origin"
    elif url and conf["configured"] and role == "origin":
        # a stale ONENESS_NODE_ROLE=origin on a mirror is exactly how a node ends
        # up "syncing" with itself and never catching up
        role = "replica"
    return {"url": url, "role": role, "configured": bool(url), "source": source,
            "origin_token_set": bool(
                str(settings.get("sync.origin_token") or "").strip()
                or conf["origin_token"])}


def is_replica() -> bool:
    """True when this node follows another proposer (and must not mint)."""
    conf = origin()
    return bool(conf["configured"]) and conf["role"] == "replica"


def require_writable() -> None:
    """Replicas are read-only: a local block would be erased by the next pull."""
    if not is_replica():
        return
    raise AppError(
        f"this node is a verified replica of {origin()['url']} — new blocks are "
        f"only proposed there. Create/mint/send on the origin, then pull here "
        f"(POST /api/chain/sync/pull). Set sync.role: origin to make this node "
        f"the proposer instead.",
        code="SYNC_READ_ONLY", http_status=409,
        details={"role": "replica", "origin": origin()["url"]})


def _pysocks_available() -> bool:
    try:
        import socks  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    return True


def status() -> dict:
    """The replication wiring, without touching the network."""
    conf = origin()
    wiring = fleet()
    return {**conf, "auto": _auto(),
            "interval_seconds": _interval(), "timeout": _timeout(),
            "proxy": _proxy() or None,
            "onion_proxy_available": _pysocks_available(),
            "fleet": {"file": FLEET_PATH.name, "configured": bool(wiring["configured"]),
                      "origin_is_this_node": bool(wiring["is_origin_node"]),
                      "origin_is_this_node_by": wiring.get("self_by"),
                      "self_hosts": sorted(self_hosts()),
                      "genesis_hash": wiring["genesis_hash"] or None,
                      "note": ("the committed fleet file supplies the origin when "
                               "no env/config value is set, and keeps a node that "
                               "does not hold the origin token a read-only replica")},
            "last_pull": dict(_LAST),
            "push": push_status(),
            "snapshot_standard": SNAPSHOT_STANDARD,
            "endpoints": {"export": "/api/admin/sync/export (Rust node)",
                          "snapshot": "/api/chain/sync/snapshot (origin API)",
                          "import": "/api/admin/sync/import (Rust node)",
                          "pull": "POST /api/chain/sync/pull",
                          "push": "POST /api/chain/sync/push (origin)",
                          "accept": "POST /api/chain/sync/accept (replica)"},
            "note": ("one proposer, verified replicas: a replica pulls the origin "
                     "snapshot (or accepts one pushed to it), re-hashes it, then "
                     "replaces its own state.json + blocks.jsonl atomically. It "
                     "keeps its own admin token and stays read-only for new blocks.")}


def snapshot(token: str | None = None) -> dict:
    """This node's ledger snapshot, straight from the Rust node (admin only).

    The export is stamped with this node's :func:`security_module.envelope` — the
    policy it implements and the issuer's signature over that policy — so a peer
    can decide, offline, whether this node's rules are the ones it trusts. The
    stamp is added here rather than in the Rust node because the Rust node has no
    keys and no policy: it is the ledger, not the operator.
    """
    reply = node.sync_export(token=token or settings.admin_token)
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise AppError(error.get("message") or "the Rust node refused the export",
                       code=error.get("code") or "SYNC_EXPORT_FAILED",
                       http_status=502)
    data = reply.get("data") or {}
    if data.get("standard") != SNAPSHOT_STANDARD:
        raise AppError(
            f"the Rust node returned {data.get('standard')!r}, expected "
            f"{SNAPSHOT_STANDARD} — rebuild rust/torchain",
            code="SYNC_EXPORT_VERSION", http_status=502)
    data[security_module.ATTESTATION_FIELD] = security_module.envelope()
    return data


def require_protocol(document: object) -> dict:
    """The consensus gate: a peer with different privacy rules cannot sync here.

    Called before every import (pull and push-accept) *and* before this node ships
    its own snapshot, so a node whose security module is broken cannot spread a
    ledger either. What it proves is provenance plus agreement — that the sender's
    policy is signed by a key this node pins and hashes to a ruleset this node
    already trusts. What it cannot prove is which code the sender is running; that
    is why :func:`verify_snapshot` still re-checks every record's privacy marker.
    """
    if not security_module.require_peer_attestation():
        return {"required": False,
                "reason": ("ONENESS_SECURITY_REQUIRE_ATTESTATION=0 — this node "
                           "accepts unattested peers and says so")}
    if not isinstance(document, dict):
        raise ValidationError("a ledger snapshot must be a JSON object",
                              code="SYNC_PROTOCOL_MISMATCH")
    attestation = document.get(security_module.ATTESTATION_FIELD)
    if attestation is None and security_module.allow_unattested():
        return {"required": False, "attested": False,
                "reason": ("ONENESS_SECURITY_ALLOW_UNATTESTED=1 — a snapshot "
                           "without a policy was accepted during a rollout"),
                "warning": ("this node did not verify the sender's privacy "
                            "rules; unset the flag once every node is updated")}
    verified = security_module.verify_attestation(attestation)
    return {"required": True, "attested": True, **verified}



def _socks_request(method: str, url: str, headers: dict, body: bytes | None,
                   timeout: float, proxy: str) -> bytes:
    """One HTTP/1.1 request through Tor's SOCKS5 port (PySocks is optional).

    ``GET`` is what a pull needs; ``POST`` is what a push needs, and a replica
    that only exists as a hidden service has no other route. ``Connection:
    close`` keeps it to a single response with no chunked-body parsing.
    """
    try:
        import socks
    except ImportError as problem:      # pragma: no cover - depends on the host
        raise AppError(
            "an .onion origin or target needs the Tor SOCKS proxy: "
            "`pip install pysocks` and set ONENESS_SYNC_PROXY=socks5://127.0.0.1:9050",
            code="SYNC_TOR_PROXY_MISSING", http_status=503) from problem

    parts = urllib.parse.urlsplit(url)
    proxy_parts = urllib.parse.urlsplit(
        proxy if "://" in proxy else f"socks5://{proxy}")
    handle = socks.socksocket()
    handle.set_proxy(socks.SOCKS5, proxy_parts.hostname or "127.0.0.1",
                     proxy_parts.port or 9050)
    handle.settimeout(timeout)
    handle.connect((parts.hostname, parts.port or 80))
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    lines = [f"{method.upper()} {path} HTTP/1.1", f"Host: {parts.netloc}",
             "Connection: close", "Accept-Encoding: identity"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    handle.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    if body:
        handle.sendall(body)
    chunks: list[bytes] = []
    while True:
        chunk = handle.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    handle.close()
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    try:
        code = int(status_line.split(" ")[1])
    except (IndexError, ValueError) as problem:
        raise AppError(f"the onion origin returned a malformed response: "
                       f"{status_line[:120]}", code="SYNC_ORIGIN_ERROR",
                       http_status=502) from problem
    if code >= 400:
        raise AppError(f"the onion origin answered HTTP {code}: "
                       f"{payload[:200].decode('utf-8', 'replace')}",
                       code="SYNC_ORIGIN_ERROR", http_status=502)
    return payload


def _socks_get(url: str, headers: dict, timeout: float, proxy: str) -> bytes:
    """A GET through Tor's SOCKS5 port (the pull path)."""
    return _socks_request("GET", url, headers, None, timeout, proxy)


def _read(url: str, headers: dict, timeout: float) -> bytes:
    proxy = _proxy()
    host = urllib.parse.urlsplit(url).hostname or ""
    if proxy.startswith("socks") or host.endswith(".onion"):
        fallback = f"socks5://127.0.0.1:{settings.get('tor.socks_port') or 9050}"
        return _socks_get(url, headers, timeout, proxy or fallback)
    handlers = ([urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
                if proxy else [])
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """POST a JSON body (a pushed snapshot) and return the parsed reply."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sent = {**headers, "content-type": "application/json",
            "content-length": str(len(raw))}
    proxy = _proxy()
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        if proxy.startswith("socks") or host.endswith(".onion"):
            fallback = f"socks5://127.0.0.1:{settings.get('tor.socks_port') or 9050}"
            body = _socks_request("POST", url, sent, raw, timeout, proxy or fallback)
        else:
            handlers = ([urllib.request.ProxyHandler({"http": proxy,
                                                      "https": proxy})]
                         if proxy else [])
            opener = urllib.request.build_opener(*handlers)
            request = urllib.request.Request(url, data=raw, headers=sent,
                                             method="POST")
            with opener.open(request, timeout=timeout) as response:
                body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise AppError(f"{url} answered HTTP {error.code}: {detail}",
                       code="SYNC_PUSH_REJECTED", http_status=502) from error
    except AppError:
        raise
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as problem:
        raise AppError(f"{url} is not reachable "
                       f"({getattr(problem, 'reason', problem)})",
                       code="SYNC_PUSH_UNREACHABLE", http_status=502) from problem
    try:
        reply = json.loads(body.decode("utf-8", "replace"))
    except ValueError as problem:
        raise AppError(f"{url} did not return JSON",
                       code="SYNC_PUSH_REJECTED", http_status=502) from problem
    if not isinstance(reply, dict):
        raise AppError(f"{url} returned {type(reply).__name__}, not an envelope",
                       code="SYNC_PUSH_REJECTED", http_status=502)
    return reply


def _origin_json(base: str, path: str, token: str) -> dict:
    url = f"{base}{path}"
    headers = {"accept": "application/json"}
    if token:
        headers["x-admin-token"] = token
    try:
        raw = _read(url, headers, _timeout())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise AppError(f"the origin answered HTTP {error.code}: {detail}",
                       code="SYNC_ORIGIN_ERROR", http_status=502) from error
    except AppError:
        raise
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as problem:
        raise AppError(
            f"the origin {base} is not reachable "
            f"({getattr(problem, 'reason', problem)})",
            code="SYNC_ORIGIN_UNREACHABLE", http_status=502) from problem
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as problem:
        raise AppError(f"the origin {base} did not return JSON",
                       code="SYNC_ORIGIN_ERROR", http_status=502) from problem
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = (payload or {}).get("error") or {}
        raise AppError(error.get("message") or "the origin did not return a snapshot",
                       code=error.get("code") or "SYNC_ORIGIN_ERROR",
                       http_status=502)
    return payload.get("data") or {}


def _origin_token(explicit: str | None = None) -> str:
    return (str(explicit or "").strip()
            or str(settings.get("sync.origin_token") or "").strip()
            or fleet()["origin_token"]
            or str(settings.get("api.admin_token") or ""))


def _import_tokens(explicit: str | None = None) -> list:
    """The tokens a local import may have to try, most specific first.

    ``ledger_host.write_chain()`` deliberately keeps a *deployment's own* admin
    token out of a seeded ledger: when this container has no real
    ``api.admin_token`` (empty or the ``change-me`` default) the imported
    ``state.json`` keeps the ``admin_token_sha256`` the snapshot carried — the
    origin's. The local import then has to authenticate with the origin's token,
    the one the committed fleet file already holds; sending ``change-me``
    instead is exactly how a seeded replica ends up with "admin token is
    invalid" and never leaves its seed height.
    """
    candidates = (str(explicit or "").strip(),
                  str(settings.get("api.admin_token") or "").strip(),
                  _origin_token())
    ordered: list = []
    for token in candidates:
        if token and token not in ordered:
            ordered.append(token)
    return ordered or [""]


def _import_snapshot(remote: dict, local_token: str | None = None) -> dict:
    """Import the snapshot into the local node, using the token it accepts."""
    tokens = _import_tokens(local_token)
    reply = node.sync_import(remote, token=tokens[0])
    for token in tokens[1:]:
        if reply.get("ok"):
            break
        reply = node.sync_import(remote, token=token)
    return reply


def _local_info() -> dict:
    try:
        reply = node.info()
    except ChainUnavailable:
        return {}
    return (reply.get("data") or {}) if reply.get("ok") else {}



def _adopt(document: dict, verified: dict, *, source: str, role: str,
           force: bool = False, local_token: str | None = None,
           started: float | None = None) -> dict:
    """Import a snapshot that has already been re-hashed here (pull or push).

    Refuses (unless ``force``) to move backwards or to re-import the height it
    already has, so a stale — or hostile — peer can never roll this ledger back.
    The document handed to the Rust node is the *verified* one: the same bytes a
    replica just hashed, never a re-serialisation of a subset.
    """
    local = _local_info()
    local_height = int(local.get("height") or 0)
    report = {"origin": source, "role": role,
              "origin_height": verified["height"],
              "origin_head_hash": verified["head_hash"],
              "origin_tokens": verified["tokens"],
              "local_height_before": local_height,
              "verified": {key: verified[key] for key in
                           ("height", "head_hash", "genesis_hash", "tokens",
                            "accounts", "records", "transactions")}}
    if not force and verified["height"] < local_height:
        report.update(skipped=True, imported=False,
                      reason=("the origin is behind this node — refusing to "
                              "roll the local ledger back"))
    elif not force and verified["height"] == local_height:
        same = str(verified["head_hash"]) == str(local.get("head_hash"))
        report.update(
            skipped=True, imported=False, same_head=same,
            reason=("already at the origin's height" if same else
                    "same height but a different head — run with "
                    "force:true to adopt the origin's branch"))
    else:
        reply = _import_snapshot(document, local_token)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise AppError(
                error.get("message") or "the local node refused the snapshot",
                code=error.get("code") or "SYNC_IMPORT_FAILED",
                http_status=502)
        data = reply.get("data") or {}
        after = _local_info()
        # a freshly imported ledger must be re-read, never served from cache
        try:
            from oneness_tor.services import public_ledger

            public_ledger.reset()
        except Exception:  # noqa: BLE001 - a cache reset must never fail a pull
            pass
        report.update(
            skipped=False, imported=True,
            height=int(data.get("height") or verified["height"]),
            head_hash=data.get("head_hash"),
            state_root=data.get("state_root"),
            blocks=data.get("blocks"), tokens=data.get("tokens"),
            accounts=data.get("accounts"), records=data.get("records"),
            local_height_after=int(after.get("height") or 0),
            elapsed_ms=round((time.time() - (started or time.time())) * 1000, 1),
            reason="snapshot imported and verified")
    return report


def pull(*, force: bool = False, origin_url: str | None = None,
         local_token: str | None = None, origin_token: str | None = None) -> dict:
    """Pull the origin's verified ledger snapshot into this node.

    Refuses (unless ``force``) to move backwards or to re-import the height it
    already has, so a stale or hostile origin can never roll this ledger back.
    """
    conf = origin()
    base = str(origin_url or conf["url"] or "").strip().rstrip("/")
    if not base:
        raise ValidationError(
            "no sync origin configured — set sync.origin_url (or "
            "ONENESS_SYNC_ORIGIN) to the public base URL of the proposing node")
    if not _LOCK.acquire(blocking=False):
        raise ValidationError("a sync pull is already running")
    started = time.time()
    try:
        remote = _origin_json(base, "/api/chain/sync/snapshot",
                              _origin_token(origin_token))
        gate = require_protocol(remote)
        verified = verify_snapshot(remote)
        report = _adopt(remote, verified, source=base, role=conf["role"],
                        force=force, local_token=local_token, started=started)
        report["protocol"] = gate
        _LAST.update({"at": int(time.time()), "ok": True,
                      "reason": report["reason"], "detail": report})
        return report
    except Exception as problem:  # noqa: BLE001 - recorded, then re-raised
        _LAST.update({"at": int(time.time()), "ok": False,
                      "reason": getattr(problem, "message", str(problem))})
        raise
    finally:
        _LOCK.release()


def accept(document: object, *, force: bool = False,
           local_token: str | None = None, source: str = "") -> dict:
    """Replica side of a push: verify the snapshot someone sent, then import it.

    A pushed snapshot gets exactly the treatment a pulled one gets — every block
    re-hashed, every ``prev_hash`` walked, the state root recomputed — so an
    origin that is compromised mid-flight cannot install anything it could not
    prove. ``force`` is the same door pull has, and it is never the default.
    """
    gate = require_protocol(document)
    verified = verify_snapshot(document)
    started = time.time()
    held = False
    if not _LOCK.acquire(blocking=False):
        raise ValidationError("a sync pull or push is already running here")
    held = True
    try:
        report = _adopt(document, verified, source=source or "pushed snapshot",  # type: ignore[arg-type]
                        role="replica", force=force, local_token=local_token,
                        started=started)
        report["protocol"] = gate
        _LAST.update({"at": int(time.time()), "ok": True,
                      "reason": report["reason"], "detail": report})
        return report
    except Exception as problem:  # noqa: BLE001 - recorded, then re-raised
        _LAST.update({"at": int(time.time()), "ok": False,
                      "reason": getattr(problem, "message", str(problem))})
        raise
    finally:
        if held:
            _LOCK.release()


def start_scheduler(interval: int | None = None):
    """Pull from the origin on a timer (no-op when no origin is configured)."""
    conf = origin()
    if not conf["configured"] or not _auto():
        return None
    seconds = max(15, int(interval or _interval()))

    def loop() -> None:
        import logging

        logger = logging.getLogger("oneness_tor.sync")
        while True:
            try:
                report = pull()
                if report.get("imported"):
                    logger.info("sync: imported height %s from %s",
                                report.get("height"), report.get("origin"))
                else:
                    logger.debug("sync: %s", report.get("reason"))
            except Exception:  # noqa: BLE001 - a timer must not kill the process
                logger.warning("sync pull from %s failed", conf["url"],
                               exc_info=True)
            time.sleep(seconds)

    thread = threading.Thread(target=loop, name="oneness-sync", daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------------------- push leg
def _target_row(item: object) -> dict | None:
    """One ``sync.push.targets`` entry — ``{url, token, label}`` or ``"url|token"``."""
    if isinstance(item, dict):
        url = str(item.get("url") or "").strip().rstrip("/")
        token = str(item.get("token") or "").strip()
        label = str(item.get("label") or url).strip()
    elif isinstance(item, str):
        text = str(item).strip()
        url, _, token = text.partition("|")
        url, token = url.strip().rstrip("/"), token.strip()
        label = url
    else:
        return None
    if not url:
        return None
    return {"url": url, "token": token, "label": label}


def push_targets() -> list[dict]:
    """Every replica this node pushes its snapshot to (``sync.push.targets``)."""
    raw = settings.get("sync.push.targets") or []
    items = raw if isinstance(raw, list) else [raw]
    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        row = _target_row(item)
        if row and row["url"] not in seen:
            seen.add(row["url"])
            rows.append(row)
    return rows


def push_enabled() -> bool:
    """Push is on when ``sync.push.enabled`` says so, or when targets exist."""
    explicit = _explicit("sync.push.enabled", "ONENESS_SYNC_PUSH")
    if explicit is not None:
        if isinstance(explicit, str):
            return explicit.strip().lower() in ("1", "true", "yes", "on")
        return bool(explicit)
    return bool(push_targets())


def _push_interval() -> int:
    value = _explicit("sync.push.interval_seconds", "ONENESS_SYNC_PUSH_INTERVAL")
    if value in (None, "", 0, "0"):
        return _interval()
    try:
        return max(15, int(value))
    except (TypeError, ValueError):
        return _interval()


def _keep() -> int:
    try:
        return max(1, int(settings.get("sync.push.keep") or SNAPSHOT_KEEP))
    except (TypeError, ValueError):
        return SNAPSHOT_KEEP


def save_enabled() -> bool:
    value = settings.get("sync.push.save")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return True if value is None else bool(value)


def snapshot_dir() -> Path:
    """``data/snapshots`` — the copy of the ledger a push leaves behind."""
    from oneness_tor.services.store import data_dir

    return data_dir("snapshots")


def save_snapshot(document: dict, *, keep: int | None = None) -> Path:
    """Write ``latest.json`` + ``chain-<height>.json`` and prune the old copies.

    Keeping the file on disk is what makes the automation worth having: the
    newest verified ledger is always there to re-seed a wiped Render container
    (``ONENESS_NODE_SEED``), to hand to a new node, or to keep as a backup —
    without anybody exporting anything by hand.
    """
    folder = snapshot_dir()
    height = int((document or {}).get("height") or 0)
    latest = folder / "latest.json"
    named = folder / f"chain-{height:08d}.json"
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    for path in (named, latest):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    keep = max(1, int(keep or _keep()))
    copies = sorted(folder.glob("chain-*.json"))
    for stale in copies[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass
    return latest


def push_status() -> dict:
    """The push wiring, without touching the network."""
    conf = origin()
    return {"enabled": push_enabled(),
            "role": conf["role"],
            "interval_seconds": _push_interval(),
            "save": save_enabled(),
            "save_dir": str(snapshot_dir()),
            "keep": _keep(),
            "targets": [{"url": row["url"], "label": row["label"],
                         "token_set": bool(row["token"])}
                        for row in push_targets()],
            "last_push": dict(_PUSH_LAST),
            "endpoint": "POST /api/chain/sync/push",
            "accept": "POST /api/chain/sync/accept (on each target)",
            "note": ("the origin exports its verified snapshot, keeps a copy in "
                     "data/snapshots and POSTs it to every target on a timer; "
                     "each target re-hashes it before importing, so a push can "
                     "never install a ledger nobody proved. A replica never "
                     "pushes — it only pulls or accepts")}


def push(*, targets: object = None, save: bool | None = None,
         force: bool = False, token: str | None = None) -> dict:
    """Origin → replicas: export this ledger's snapshot and POST it to each target.

    Only the proposer may push (a replica's ledger is somebody else's truth, and
    pushing it back would fight the origin). Every target re-verifies what it
    receives, so one node failing is a row in the report — never a lie about the
    others.
    """
    if is_replica():
        raise AppError(
            f"this node is a replica of {origin()['url']} — only the proposer "
            f"pushes snapshots. Push from the origin, or pull here instead "
            f"(POST /api/chain/sync/pull).",
            code="SYNC_READ_ONLY", http_status=409,
            details={"role": "replica", "origin": origin()["url"]})
    rows: list[dict] = []
    if isinstance(targets, str):
        targets = [targets]
    for item in (targets if isinstance(targets, list) else []):
        row = _target_row(item)
        if row:
            rows.append(row)
    rows = rows or push_targets()
    if not rows:
        raise ValidationError(
            "no push targets — set sync.push.targets (or "
            "ONENESS_SYNC_PUSH_TARGETS=https://node2.onrender.com,https://node3…) "
            "to the public base URL of every replica",
            details={"endpoint": "POST /api/chain/sync/push",
                     "config": "sync.push.targets"})
    if not _LOCK.acquire(blocking=False):
        raise ValidationError("a sync pull or push is already running")
    started = time.time()
    try:
        document = snapshot()                     # this node's Rust ledger
        gate = require_protocol(document)         # our own rules must verify too
        verified = verify_snapshot(document)      # never ship an unproven ledger
        saved = (save_snapshot(document)
                 if (save if save is not None else save_enabled()) else None)
        default_token = str(token or "").strip() or _origin_token()
        results: list[dict] = []
        for row in rows:
            target_token = row["token"] or default_token
            headers = {"accept": "application/json"}
            if target_token:
                headers["x-admin-token"] = target_token
            try:
                reply = _post_json(
                    f"{row['url']}/api/chain/sync/accept", headers,
                    {"snapshot": document, "origin": row["label"],
                     "force": bool(force)}, _timeout())
                data = reply.get("data") or {}
                results.append({
                    "url": row["url"], "label": row["label"],
                    "ok": bool(reply.get("ok")),
                    "imported": bool(data.get("imported")),
                    "skipped": bool(data.get("skipped")),
                    "height": data.get("height"),
                    "reason": data.get("reason")
                    or ((reply.get("error") or {}).get("message")),
                })
            except AppError as problem:
                results.append({"url": row["url"], "label": row["label"],
                                "ok": False, "imported": False, "skipped": False,
                                "error": problem.code, "reason": problem.message})
        pushed = [row for row in results if row["ok"]]
        failed = [row for row in results if not row["ok"]]
        report = {"role": "origin", "origin_height": verified["height"],
                  "origin_head_hash": verified["head_hash"],
                  "protocol": gate,
                  "targets": results, "pushed": len(pushed),
                  "failed": [row["url"] for row in failed],
                  "saved": str(saved) if saved else None,
                  "checked_at": int(time.time()),
                  "elapsed_ms": round((time.time() - started) * 1000, 1),
                  "reason": (f"pushed height {verified['height']} to "
                             f"{len(pushed)}/{len(results)} target(s)"
                             if pushed else
                             "no target accepted the snapshot — see targets"),
                  "note": ("each target re-hashed the snapshot and imported it "
                           "before answering; a failure is reported per target, "
                           "never smoothed over")}
        _PUSH_LAST.update({"at": report["checked_at"], "ok": not failed,
                           "reason": report["reason"],
                           "height": verified["height"], "targets": results})
        return report
    except Exception as problem:  # noqa: BLE001 - recorded, then re-raised
        _PUSH_LAST.update({"at": int(time.time()), "ok": False,
                           "reason": getattr(problem, "message", str(problem)),
                           "targets": []})
        raise
    finally:
        _LOCK.release()



def start_pusher(interval: int | None = None):
    """Push the snapshot to every target on a timer (origin only)."""
    if is_replica() or not push_enabled() or not push_targets():
        return None
    seconds = max(15, int(interval or _push_interval()))

    def loop() -> None:
        import logging

        logger = logging.getLogger("oneness_tor.sync")
        while True:
            try:
                report = push()
                if report.get("failed"):
                    logger.warning("sync push: %s/%s target(s) failed: %s",
                                   len(report["failed"]), len(report["targets"]),
                                   ", ".join(report["failed"]))
                else:
                    logger.info("sync push: height %s to %s target(s)",
                                report.get("origin_height"),
                                report.get("pushed"))
            except Exception:  # noqa: BLE001 - a timer must not kill the process
                logger.warning("sync push failed", exc_info=True)
            time.sleep(seconds)

    thread = threading.Thread(target=loop, name="oneness-sync-push", daemon=True)
    thread.start()
    return thread


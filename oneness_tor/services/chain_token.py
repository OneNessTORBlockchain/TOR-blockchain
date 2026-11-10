"""The chain admin token this deployment presents to its own Rust node.

``torchain init`` prints a token exactly **once**, and the node keeps only
``sha3-256(token)`` in ``data/chain/state.json`` — so no page can look it up, and
an operator should never have to paste it into a panel just to seal the archive
or move the fee collector. This module owns that one credential:

* an explicit ``api.admin_token`` / ``ONENESS_ADMIN_TOKEN`` always wins — that is
  the operator's own token (and the one ``torchain init`` printed);
* a **replica** presents the *origin's* token instead: its ledger is seeded from
  the origin's snapshot and keeps that ``admin_token_sha256``, so the origin's
  credential is the one it accepts (and the one the origin pushes with);
* otherwise the node creates its own token on first boot and keeps it in
  ``data/admin/chain_token`` (mode 0600, git-ignored like the rest of ``data/``);
* :func:`ensure` runs *before* the ledger is spawned and writes that token's
  sha3 into ``data/chain/state.json``. ``admin_token_sha256`` is node-local — it
  is not part of ``state_root`` — so adopting a token never changes the chain a
  replica verifies, and a seeded container keeps answering to its own token;
* :func:`usable` only returns a token the ledger's own hash agrees with, so a
  stale file can never be presented as if it worked.

    from oneness_tor.services import chain_token
    held = chain_token.ensure()     # {"token": …, "created": True, "adopted": True}
    chain_token.usable()            # the token to send as x-admin-token
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from core.config import settings
from oneness_tor.services.store import data_dir

#: The value ``config.example.yaml`` ships: a placeholder, never a credential.
DEFAULT_TOKEN = "change-me"
ENV = "ONENESS_ADMIN_TOKEN"
FILENAME = "chain_token"


# ------------------------------------------------------------------- files
def store_path() -> Path:
    """``data/admin/chain_token`` — the token this deployment generated."""
    return data_dir("admin") / FILENAME


def state_path() -> Path:
    """The ledger's ``state.json`` (where ``admin_token_sha256`` lives)."""
    return settings.chain_dir / "state.json"


def sha3(token: object) -> str:
    return hashlib.sha3_256(str(token or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ reading
def explicit() -> str:
    """The operator's own token (``ONENESS_ADMIN_TOKEN`` / ``api.admin_token``)."""
    text = str(settings.get("api.admin_token") or "").strip()
    return "" if not text or text == DEFAULT_TOKEN else text


def stored() -> str:
    try:
        return store_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _is_replica() -> bool:
    """True when this node mirrors another (``config.fleet.yaml`` or a sync origin)."""
    from oneness_tor.services import node_sync      # late: node_sync reads us

    try:
        return bool(node_sync.is_replica())
    except Exception:       # noqa: BLE001 - an unreadable fleet file means "no mirror"
        return False


def replica_token() -> str:
    """The chain credential a *replica* must present: the origin's own.

    A replica's ledger is seeded from the origin's snapshot, and
    ``ledger_host.write_chain`` never overwrites ``admin_token_sha256`` without an
    explicit token — so the Rust node here accepts the **origin's** token, which
    is also what the origin pushes with. Reusing it keeps pull/accept working and
    makes chain-level actions work on a mirror without inventing a second secret
    (and without making the mirror look like the proposer).
    """
    if not _is_replica():
        return ""
    from oneness_tor.services import node_sync

    try:
        token = str(node_sync._origin_token() or "").strip()
    except Exception:       # noqa: BLE001 - "no origin token" is a normal answer
        return ""
    return "" if token == DEFAULT_TOKEN else token


def current() -> str:
    """Explicit token, else the replica's origin token, else the stored one."""
    return explicit() or replica_token() or stored()


def source() -> str | None:
    """Where ``current()`` came from: ``env``, ``origin``, ``file`` or nothing."""
    if explicit():
        return "env"
    if replica_token():
        return "origin"
    return "file" if stored() else None


def ledger_hash() -> str | None:
    """``admin_token_sha256`` from the ledger, or ``None`` before genesis exists."""
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    digest = str(state.get("admin_token_sha256") or "").strip()
    return digest.lower() or None


def matches() -> bool:
    """True when the ledger's hash is this deployment's token."""
    expected = ledger_hash()
    held = current()
    return bool(expected and held and sha3(held) == expected)


def usable() -> str | None:
    """The token to present to the ledger — ``None`` when it would be rejected."""
    held = current()
    if not held:
        return None
    expected = ledger_hash()
    if not expected:
        return held            # pre-genesis: the configured token is accepted
    return held if sha3(held) == expected else None


# ------------------------------------------------------------------- writing
def _persist(token: str) -> Path:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)          # operator-only; data/ is git-ignored
    except OSError:
        pass
    return path


def _forget_stored() -> bool:
    """Drop a token this node generated before it became a mirror."""
    try:
        store_path().unlink()
        return True
    except OSError:
        return False


def _node_running() -> bool:
    """True when a ledger already answers — its token then lives in memory."""
    from core.node_client import NodeClient      # late: keep imports light

    try:
        return bool(NodeClient(settings.node_api_url, timeout=1.5).is_reachable())
    except Exception:       # noqa: BLE001 - "unreachable" is the answer we want
        return False


def adopt(token: str, *, force: bool = False) -> bool:
    """Write ``sha3(token)`` into the ledger's ``state.json``.

    Only legitimate while the ledger is *at rest*: a node that is already
    serving has its hash in memory, and rewriting the file underneath it would
    desynchronise the two. Returns ``True`` when the file changed.
    """
    token = str(token or "").strip()
    path = state_path()
    if not token or not path.is_file():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(state, dict):
        return False
    digest = sha3(token)
    if str(state.get("admin_token_sha256") or "").lower() == digest:
        return False
    if not force and _node_running():
        return False
    state["admin_token_sha256"] = digest
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)
    return True


def ensure() -> dict:
    """Create the deployment's token when missing and hand it to its own ledger.

    Runs in the start command *before* the node is spawned, so the first request
    that needs a chain-level action already has a working credential. A replica
    never mints one: its ledger answers to the origin's token, and a second
    secret would lock the origin's push/pull out. The token itself is returned for
    callers that must use it now — never print it.
    """
    token = current()
    created = False
    if _is_replica() and not explicit() and stored():
        # a mirror keeps no local token: the stale one would tell its ledger to
        # answer to something the origin does not push with
        _forget_stored()
        token = current()
    if not token and not _is_replica():
        token = secrets.token_urlsafe(32)
        _persist(token)
        created = True
    adopted = adopt(token) if token else False
    return {"token": token, "created": created, "adopted": adopted,
            "path": str(store_path()), "source": source(), "digest": sha3(token)}


def status() -> dict:
    """What /api/health and the CLI may say out loud — never the token itself."""
    return {"configured": bool(current()), "source": source(),
            "path": str(store_path()), "usable": usable() is not None,
            "ledger_hash": ledger_hash(), "replica": _is_replica(), "env": ENV,
            "hint": ("set ONENESS_ADMIN_TOKEN to pin your own chain token "
                     "(the one `torchain init` printed); otherwise a node creates "
                     "data/admin/chain_token and a replica uses the origin's")}

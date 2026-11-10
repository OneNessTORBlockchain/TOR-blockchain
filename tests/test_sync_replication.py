"""Replication must be provable: a replica only adopts re-hashed snapshots."""
from __future__ import annotations

import hashlib

import pytest

from core.errors import AppError, ValidationError
from oneness_tor.services import node_sync, security_module

FOUNDER = "TOR1" + "a" * 30 + "onion"


def _record(txid: str, height: int) -> dict:
    return {"txid": txid, "kind": "genesis" if height == 0 else "transfer",
            "from": "genesis" if height == 0 else FOUNDER, "to": FOUNDER,
            "amount": 1, "token": None, "fee": 0, "note": "", "external_ref": "",
            "signature": "", "seq": height + 1, "timestamp": height + 1,
            "height": height, "block_hash": "", "visibility": "public"}


def build_snapshot(blocks: int = 2) -> dict:
    """A coherent ledger: linked blocks, matching state root, one tx per block."""
    state = {"chain_id": "072316", "height": 0, "head_hash": "",
             "genesis_hash": "", "total_supply": 50_000_000 * 10 ** 8,
             "founder": FOUNDER, "fee_receiver": FOUNDER,
             "admin_token_sha256": "hash", "created_at": 1, "seq": 1,
             "accounts": {FOUNDER: {"address": FOUNDER, "pubkey_base58": "",
                                    "balance": 50_000_000 * 10 ** 8, "nonce": 0,
                                    "tokens": {}}},
             "tokens": {}, "pools": {}, "records": []}
    ledger: list[dict] = []
    previous = "0" * 64
    for height in range(blocks):
        record = _record(f"tx{height}", height)
        state["records"].append(record)
        state["height"] = height
        if height == 1:
            state["tokens"] = {"BTC": {"symbol": "BTC", "name": "Bitcoin",
                                       "supply": 1_000_000, "decimals": 0,
                                       "owner": FOUNDER, "created_at": 2}}
        block = {"height": height, "prev_hash": previous, "timestamp": height + 1,
                 "proposer": FOUNDER, "state_root": node_sync.state_root_of(state),
                 "txids": [record["txid"]], "hash": ""}
        block["hash"] = node_sync.block_digest(block)
        record["block_hash"] = block["hash"]
        previous = block["hash"]
        ledger.append({"block": block, "txs": [record]})
    state["head_hash"] = previous
    state["genesis_hash"] = ledger[0]["block"]["hash"]
    return {"standard": node_sync.SNAPSHOT_STANDARD, "chain_id": "072316",
            "height": blocks - 1, "head_hash": state["head_hash"],
            "genesis_hash": state["genesis_hash"], "state": state,
            "blocks": ledger,
            # a real export is stamped with the sender's signed policy
            "protocol": security_module.envelope()}


def test_a_coherent_snapshot_verifies():
    report = node_sync.verify_snapshot(build_snapshot())
    assert report["height"] == 1
    assert report["tokens"] == 1
    assert report["transactions"] == 2
    assert report["head_hash"] == report["blocks"][-1]["hash"]


def test_a_venue_pool_is_hashed_the_way_chain_rs_hashes_it():
    """``chain.rs`` appends |venue|creator|manager only to a venue-extended pool."""
    state = {"accounts": {}, "tokens": {},
             "pools": {"BTC": {"tor_reserve": 10, "token_reserve": 20, "fee_bps": 30},
                       "tordex:BTC": {"tor_reserve": 1, "token_reserve": 2,
                                      "fee_bps": 25, "venue": "tordex",
                                      "creator": FOUNDER, "manager": ""}},
             "total_supply": 5, "height": 7}
    expected = hashlib.sha3_256(
        "\n".join(["p|BTC|10|20|30",
                   f"p|tordex:BTC|1|2|25|tordex|{FOUNDER}|",
                   "s|5|7"]).encode("utf-8")).hexdigest()
    assert node_sync.state_root_of(state) == expected

    # a legacy pool that carries none of the three fields keeps the short line
    legacy = dict(state, pools={"BTC": state["pools"]["BTC"]})
    assert node_sync.state_root_of(legacy) == hashlib.sha3_256(
        "\n".join(["p|BTC|10|20|30", "s|5|7"]).encode("utf-8")).hexdigest()


def test_a_tampered_block_is_refused():
    snapshot = build_snapshot()
    snapshot["blocks"][1]["block"]["timestamp"] += 1
    with pytest.raises(ValidationError, match="does not re-hash"):
        node_sync.verify_snapshot(snapshot)


def test_a_broken_link_is_refused():
    snapshot = build_snapshot()
    snapshot["blocks"][1]["block"]["prev_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="does not link"):
        node_sync.verify_snapshot(snapshot)


def test_state_that_does_not_match_the_head_root_is_refused():
    snapshot = build_snapshot()
    snapshot["state"]["tokens"]["BTC"]["supply"] += 1
    with pytest.raises(ValidationError, match="state_root"):
        node_sync.verify_snapshot(snapshot)


def test_a_block_whose_tx_is_missing_from_the_state_is_refused():
    snapshot = build_snapshot()
    snapshot["state"]["records"] = [snapshot["state"]["records"][0]]
    with pytest.raises(ValidationError, match="no such record"):
        node_sync.verify_snapshot(snapshot)


def test_a_different_genesis_is_refused():
    snapshot = build_snapshot()
    snapshot["state"]["genesis_hash"] = "1" * 64
    with pytest.raises(ValidationError, match="genesis_hash"):
        node_sync.verify_snapshot(snapshot)


def test_pull_imports_only_when_the_origin_is_ahead(monkeypatch):
    snapshot = build_snapshot()
    imported: dict = {}

    def fake_import(payload, token=None):
        imported.update(payload=payload, token=token)
        return {"ok": True, "data": {"height": 1, "head_hash": "b" * 64,
                                     "blocks": 2, "tokens": 1, "accounts": 1,
                                     "records": 2}}

    monkeypatch.setattr(node_sync, "_origin_json", lambda *a, **k: snapshot)
    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 0, "head_hash": "a" * 64})
    monkeypatch.setattr(node_sync.node, "sync_import", fake_import)

    report = node_sync.pull(origin_url="https://origin.example", local_token="t")
    assert report["imported"] is True
    assert report["height"] == 1
    assert report["local_height_before"] == 0
    assert imported["payload"] is snapshot and imported["token"] == "t"


def test_pull_never_rolls_a_ledger_back(monkeypatch):
    snapshot = build_snapshot()
    monkeypatch.setattr(node_sync, "_origin_json", lambda *a, **k: snapshot)
    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 9, "head_hash": "a" * 64})
    monkeypatch.setattr(node_sync.node, "sync_import",
                        lambda *a, **k: pytest.fail("must not import"))

    report = node_sync.pull(origin_url="https://origin.example")
    assert report["skipped"] is True
    assert "behind" in report["reason"]


def test_pull_is_a_no_op_at_the_same_height(monkeypatch):
    snapshot = build_snapshot()
    head = snapshot["state"]["head_hash"]
    monkeypatch.setattr(node_sync, "_origin_json", lambda *a, **k: snapshot)
    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 1, "head_hash": head})
    monkeypatch.setattr(node_sync.node, "sync_import",
                        lambda *a, **k: pytest.fail("must not import"))

    report = node_sync.pull(origin_url="https://origin.example")
    assert report["skipped"] is True and report["same_head"] is True


def test_pull_requires_an_origin(monkeypatch):
    monkeypatch.setattr(node_sync, "origin",
                        lambda: {"url": "", "role": "origin", "configured": False})
    with pytest.raises(ValidationError, match="no sync origin"):
        node_sync.pull()


def test_a_replica_refuses_to_propose(monkeypatch):
    monkeypatch.setattr(node_sync, "origin",
                        lambda: {"url": "https://origin.example", "role": "replica",
                                 "configured": True, "origin_token_set": True})
    assert node_sync.is_replica() is True
    with pytest.raises(AppError) as raised:
        node_sync.require_writable()
    assert raised.value.code == "SYNC_READ_ONLY"


def test_the_origin_is_writable(monkeypatch):
    monkeypatch.setattr(node_sync, "origin",
                        lambda: {"url": "", "role": "origin", "configured": False,
                                 "origin_token_set": False})
    assert node_sync.is_replica() is False
    node_sync.require_writable()      # must not raise


# ---------------------------------------------------------------- fleet wiring
ORIGIN_TOKEN = "0123456789abcdef0123456789abcdef"


def _committed_fleet(tmp_path, monkeypatch, *, url: str = "https://origin.example",
                     token: str = ORIGIN_TOKEN, interval: int = 45):
    """A committed fleet file, read fresh, with every sync env var silenced."""
    path = tmp_path / "config.fleet.yaml"
    path.write_text(
        "# the committed fleet wiring — one origin, many mirrors\n"
        "origin:\n"
        f"  url: {url}\n"
        f"  token: {token}\n"
        f"  admin_token_sha256: {node_sync._sha3(token)}\n"
        f"  genesis_hash: {'f' * 64}\n"
        "replicas:\n"
        "  auto: true\n"
        f"  interval_seconds: {interval}\n", encoding="utf-8")
    monkeypatch.setattr(node_sync, "FLEET_PATH", path)
    monkeypatch.setattr(node_sync, "_FLEET_CACHE", {"mtime": None, "data": {}})
    for name in ("ONENESS_SYNC_ORIGIN", "ONENESS_SYNC_TOKEN",
                 "ONENESS_SYNC_INTERVAL", "ONENESS_NODE_ROLE"):
        monkeypatch.setenv(name, "")
    return path


def test_the_fleet_file_wires_a_mirror_that_no_env_var_mentions(tmp_path, monkeypatch):
    """The bug this file exists for: a replica left with no origin syncs with itself."""
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "a-replica-keeps-its-own-token")

    conf = node_sync.origin()
    assert conf["role"] == "replica" and conf["configured"] is True
    assert conf["url"] == "https://origin.example" and conf["source"] == "fleet"
    assert node_sync.is_replica() is True
    assert node_sync._origin_token() == ORIGIN_TOKEN     # never the local token
    assert node_sync.status()["fleet"]["origin_is_this_node"] is False


def test_the_node_holding_the_origin_token_never_pulls_from_itself(tmp_path, monkeypatch):
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", ORIGIN_TOKEN)

    conf = node_sync.origin()
    assert conf == {"url": "", "role": "origin", "configured": False, "source": "",
                    "origin_token_set": True}
    assert node_sync.is_replica() is False
    assert node_sync.start_scheduler() is None           # nothing to mirror


def test_an_explicit_origin_still_wins_over_the_fleet_file(tmp_path, monkeypatch):
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "a-replica-keeps-its-own-token")
    monkeypatch.setenv("ONENESS_SYNC_ORIGIN", "https://pinned.example")
    monkeypatch.setenv("ONENESS_SYNC_TOKEN", "explicit-token")

    conf = node_sync.origin()
    assert conf["url"] == "https://pinned.example" and conf["source"] == "config"
    assert conf["role"] == "replica"
    assert node_sync._origin_token() == "explicit-token"


def test_the_fleet_interval_only_fills_a_silence(tmp_path, monkeypatch):
    _committed_fleet(tmp_path, monkeypatch, interval=45)
    assert node_sync._interval() == 60                   # config.yaml says 60
    monkeypatch.setattr(node_sync.settings, "raw", {})   # nobody said anything
    assert node_sync._interval() == 45
    assert node_sync._auto() is True


def test_no_fleet_file_is_simply_no_fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(node_sync, "FLEET_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(node_sync, "_FLEET_CACHE", {"mtime": None, "data": {}})
    monkeypatch.setenv("ONENESS_SYNC_ORIGIN", "")
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "any-token")

    wiring = node_sync.fleet()
    assert wiring["configured"] is False and wiring["is_origin_node"] is False
    assert node_sync.origin()["role"] == "origin"
    assert node_sync.origin()["url"] == ""


# --------------------------------- the origin whose URL is this very node
def test_the_origin_that_is_this_nodes_own_domain_never_pulls_from_itself(
        tmp_path, monkeypatch):
    """The committed origin.url is also this node's address on the origin itself."""
    _committed_fleet(tmp_path, monkeypatch, url="https://www.torblockchain.fun")
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "the-origin-keeps-its-own-token")
    monkeypatch.setenv("ONENESS_PUBLIC_BASE_URL", "https://www.torblockchain.fun")

    conf = node_sync.origin()
    assert conf == {"url": "", "role": "origin", "configured": False, "source": "",
                    "origin_token_set": True}
    assert node_sync.is_replica() is False
    node_sync.require_writable()          # the proposer must still mint
    fleet = node_sync.status()["fleet"]
    assert fleet["origin_is_this_node"] is True
    assert fleet["origin_is_this_node_by"] == "ONENESS_PUBLIC_BASE_URL"
    assert "www.torblockchain.fun" in fleet["self_hosts"]


def test_a_render_service_recognises_the_origin_it_is_serving(tmp_path, monkeypatch):
    """Render injects RENDER_EXTERNAL_URL: no dashboard click needed to be sure."""
    _committed_fleet(tmp_path, monkeypatch, url="https://oneness-tor.onrender.com")
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "a-fresh-boot-token")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://oneness-tor.onrender.com")

    assert node_sync.origin()["role"] == "origin"
    assert node_sync.status()["fleet"]["origin_is_this_node_by"] == "RENDER_EXTERNAL_URL"


def test_a_custom_domain_the_node_answers_for_makes_it_the_origin(tmp_path, monkeypatch):
    """The live wiring: Render advertises onrender.com, the site is a custom domain.

    ``RENDER_EXTERNAL_HOSTNAME`` is ``onenesstorchain.onrender.com`` and the fleet
    origin is ``https://www.torblockchain.fun`` — the same service, two names. The
    node only learns the second one from the requests it serves, and without it
    ``origin()`` demoted the proposer to a read-only replica of itself: every
    mint/send answered ``409 SYNC_READ_ONLY`` on a chain that looked healthy.
    """
    from oneness_tor.services import base_url

    _committed_fleet(tmp_path, monkeypatch, url="https://www.torblockchain.fun")
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "the-origin-keeps-its-own-token")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "onenesstorchain.onrender.com")
    remembered = tmp_path / "self_hosts.json"
    monkeypatch.setattr(base_url, "REMEMBERED_PATH", remembered)

    # before the first request on the custom domain arrives: a replica of itself
    assert node_sync.origin()["role"] == "replica"
    with pytest.raises(AppError):
        node_sync.require_writable()

    # one request addressed to www.torblockchain.fun is all it takes
    class _Request:
        headers = {"x-forwarded-proto": "https",
                   "x-forwarded-host": "www.torblockchain.fun"}
        host = "127.0.0.1:8090"
        scheme = "http"
        host_url = "http://127.0.0.1:8090/"

    assert base_url.remember(_Request()) == "www.torblockchain.fun"

    conf = node_sync.origin()
    assert conf == {"url": "", "role": "origin", "configured": False, "source": "",
                    "origin_token_set": True}
    node_sync.require_writable()          # the proposer mints again
    fleet = node_sync.status()["fleet"]
    assert fleet["origin_is_this_node"] is True
    assert fleet["origin_is_this_node_by"] == "an observed request Host"
    assert "www.torblockchain.fun" in fleet["self_hosts"]


def test_a_mirror_on_another_host_still_mirrors(tmp_path, monkeypatch):
    """Self-detection must not quietly turn every node into a proposer."""
    _committed_fleet(tmp_path, monkeypatch, url="https://www.torblockchain.fun")
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "node2s-own-token")
    monkeypatch.setenv("ONENESS_PUBLIC_BASE_URL", "https://oneness-tor-node2.onrender.com")

    conf = node_sync.origin()
    assert conf["url"] == "https://www.torblockchain.fun"
    assert conf["role"] == "replica" and conf["source"] == "fleet"
    assert node_sync.status()["fleet"]["origin_is_this_node"] is False
    with pytest.raises(AppError) as caught:
        node_sync.require_writable()
    assert caught.value.code == "SYNC_READ_ONLY"


def test_an_explicit_origin_that_names_this_node_is_a_self_reference(
        tmp_path, monkeypatch):
    """ONENESS_SYNC_ORIGIN pointing at yourself is the same self-race, caught too."""
    monkeypatch.setattr(node_sync, "FLEET_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(node_sync, "_FLEET_CACHE", {"mtime": None, "data": {}})
    monkeypatch.setenv("ONENESS_SYNC_ORIGIN", "https://www.torblockchain.fun/")
    monkeypatch.setenv("ONENESS_PUBLIC_BASE_URL", "https://www.torblockchain.fun")

    conf = node_sync.origin()
    assert conf["role"] == "origin" and conf["url"] == ""

    monkeypatch.setenv("ONENESS_PUBLIC_BASE_URL", "https://somewhere-else.example")
    conf = node_sync.origin()
    assert conf["url"] == "https://www.torblockchain.fun"   # somebody else's origin
    assert conf["configured"] is True and conf["source"] == "config"


# ------------------------------------- the token a seeded node really accepts
def _stub_pull(monkeypatch, snapshot, *, accepts, attempts: list):
    """A local node that only answers to ``accepts``, and no local height yet."""

    def fake_import(payload, token=None):
        attempts.append(token)
        if token != accepts:
            return {"ok": False, "error": {"code": "FORBIDDEN",
                                           "message": "admin token is invalid"}}
        return {"ok": True, "data": {"height": 1, "head_hash": "b" * 64, "blocks": 2,
                                     "tokens": 1, "accounts": 1, "records": 2}}

    monkeypatch.setattr(node_sync, "_origin_json", lambda *a, **k: snapshot)
    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 0, "head_hash": "a" * 64})
    monkeypatch.setattr(node_sync.node, "sync_import", fake_import)


def test_a_seeded_replica_imports_with_the_token_its_ledger_really_has(tmp_path,
                                                                       monkeypatch):
    """Nodes 2 and 3 stuck at the seed height: a container with no admin token of
    its own keeps the snapshot's ``admin_token_sha256`` — the origin's — so the
    local import must authenticate with the origin token, never ``change-me``.
    """
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "change-me")   # a seeded container
    attempts: list = []
    _stub_pull(monkeypatch, build_snapshot(), accepts=ORIGIN_TOKEN, attempts=attempts)

    report = node_sync.pull(origin_url="https://origin.example")
    assert report["imported"] is True and report["height"] == 1
    assert attempts == ["change-me", ORIGIN_TOKEN]
    assert node_sync._import_tokens()[0] == "change-me"      # its own token first


def test_a_node_with_its_own_admin_token_spends_it_first(tmp_path, monkeypatch):
    """A deployment that really owns a token imports on the first try."""
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "own-token")
    attempts: list = []
    _stub_pull(monkeypatch, build_snapshot(), accepts="own-token", attempts=attempts)

    report = node_sync.pull(origin_url="https://origin.example")
    assert report["imported"] is True
    assert attempts == ["own-token"]                         # the fleet token is spare


def test_a_pull_no_token_can_import_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A wrong credential is a loud failure in ``/api/chain/sync``, not silence."""
    _committed_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "change-me")
    attempts: list = []
    _stub_pull(monkeypatch, build_snapshot(), accepts="nobody-has-this",
               attempts=attempts)

    with pytest.raises(AppError, match="admin token is invalid"):
        node_sync.pull(origin_url="https://origin.example")
    assert attempts == ["change-me", ORIGIN_TOKEN]
    assert node_sync.status()["last_pull"]["ok"] is False
    assert node_sync.status()["last_pull"]["reason"] == "admin token is invalid"



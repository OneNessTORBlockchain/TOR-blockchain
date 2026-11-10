"""The wallet's read panes, tested against the real node over HTTP.

`GET /api/account/{address}/holdings` and `.../history` are what the TOR
Anonymous Wallet renders in **TOKEN HOLDINGS**, **NFT HOLDINGS** and
**TRANSACTION HISTORY**. These tests boot `torchain serve` and assert on the
JSON a browser would receive — not on a private function's return value — so a
route that stops being mounted, or a field a UI depends on that disappears, fails
here.

They are skipped when the binary has not been built yet:

    cd rust/torchain; cargo build --release
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = [
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain",
]
BINARY = next((path for path in CANDIDATES if path.exists()), None)
ADMIN = "TEST-ADMIN-TOKEN"

pytestmark = pytest.mark.skipif(BINARY is None, reason="build rust/torchain first")


def run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([str(BINARY), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"{args} -> {result.returncode}\n{result.stderr}"
    return result


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def get(url: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    if token:
        request.add_header("x-admin-token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        return error.code, json.loads(body)


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """A real chain: a generated founder, two user wallets, one unique asset."""
    root = tmp_path_factory.mktemp("wallet-views")
    directory = root / "chain"
    wallets = {}
    for name in ("founder", "alice", "bob"):
        path = root / f"{name}.json"
        run("wallet", "new", "--out", str(path), "--hide-mnemonic")
        wallets[name] = json.loads(path.read_text(encoding="utf-8"))
        wallets[name]["path"] = str(path)
    run("init", "--founder", wallets["founder"]["path"], "--dir", str(directory),
        "--admin-token", ADMIN)
    # TOR moves, then a 1-supply token (this chain's unique edition) is minted and
    # handed to alice — the two things the holdings panes must render.
    run("transfer", "--key", wallets["founder"]["path"], "--to",
        wallets["alice"]["address"], "--amount", "10", "--dir", str(directory))
    run("token-create", "--key", wallets["founder"]["path"], "--amount", "1",
        "--token", "ONION1", "--note", "Onion #1", "--ref",
        "ipfs://bafy-test-onion-1/metadata.json", "--dir", str(directory))
    run("transfer", "--key", wallets["founder"]["path"], "--to",
        wallets["alice"]["address"], "--amount", "1", "--token", "ONION1",
        "--dir", str(directory))
    run("transfer", "--key", wallets["alice"]["path"], "--to",
        wallets["bob"]["address"], "--amount", "2.5", "--dir", str(directory))

    port = free_port()
    process = subprocess.Popen(
        [str(BINARY), "serve", "--dir", str(directory), "--bind", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            status, body = get(f"{base}/health")
            if status == 200 and body.get("ok"):
                break
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    else:  # pragma: no cover - the node never came up
        process.terminate()
        pytest.fail("torchain serve did not answer on /health within 20s")
    try:
        yield {"base": base, "wallets": wallets, "dir": directory}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def test_holdings_render_tor_tokens_and_unique_editions(chain):
    """TOKEN HOLDINGS + NFT HOLDINGS, in one read, with the token table joined."""
    base = chain["base"]
    alice = chain["wallets"]["alice"]["address"]
    status, body = get(f"{base}/api/account/{alice}/holdings")
    assert status == 200 and body["ok"] is True, body
    data = body["data"]
    assert data["address"] == alice
    # alice received 10 TOR and sent 2.5 to bob before this read
    assert data["tor"]["balance_micro"] == 750_000_000
    assert data["tor"]["balance"] == 7.5
    # a 1-supply asset is a unique edition, not a fungible token
    assert data["token_count"] == 0 and data["tokens"] == []
    assert data["nft_count"] == 1
    nft = data["nfts"][0]
    assert nft["symbol"] == "ONION1"
    assert nft["name"] == "Onion #1"          # the creation record's note
    assert nft["supply"] == 1 and nft["nft"] is True
    assert nft["standard"] == "oneness-tor-nft/1"
    # the metadata URI the creator published, carried on external_ref
    assert nft["metadata_uri"] == "ipfs://bafy-test-onion-1/metadata.json"
    assert nft["balance"] == 1 and nft["balance_raw"] == 1
    assert data["height"] >= 1


def test_history_is_address_scoped_with_direction_and_confirmations(chain):
    """TRANSACTION HISTORY: only this address's records, newest first."""
    base = chain["base"]
    alice = chain["wallets"]["alice"]["address"]
    bob = chain["wallets"]["bob"]["address"]
    status, body = get(f"{base}/api/account/{alice}/history")
    assert status == 200 and body["ok"] is True, body
    data = body["data"]
    assert data["address"] == alice
    assert data["count"] >= 3, data["count"]
    newest = data["records"][0]
    assert newest["direction"] == "out"
    assert newest["counterparty"] == bob
    assert newest["symbol"] == "TOR" and newest["amount"] == 2.5
    assert newest["confirmed"] is True and newest["confirmations"] >= 1
    assert newest["status"] == "confirmed"
    assert newest["detail"] == f"/api/chain/tx/{newest['txid']}"
    assert data["totals"]["tor_in"] == 10.0
    assert data["totals"]["tor_out"] == 2.5
    assert data["totals"]["tokens"][0]["symbol"] == "ONION1"
    assert data["totals"]["tokens"][0]["in_raw"] == 1
    # the history never leaks a third party: every row names alice on one side
    for record in data["records"]:
        assert alice in (record["from"], record["to"]), record

    status, body = get(f"{base}/api/account/{bob}/history")
    received = body["data"]["records"][0]
    assert received["direction"] == "in" and received["counterparty"] == alice
    assert received["amount"] == 2.5


def test_history_paginates_but_totals_describe_every_record(chain):
    base = chain["base"]
    alice = chain["wallets"]["alice"]["address"]
    _, first = get(f"{base}/api/account/{alice}/history?limit=1")
    _, second = get(f"{base}/api/account/{alice}/history?limit=1&offset=1")
    assert first["data"]["limit"] == 1 and len(first["data"]["records"]) == 1
    assert len(second["data"]["records"]) == 1
    assert first["data"]["records"][0]["txid"] != second["data"]["records"][0]["txid"]
    # the page moves, the totals do not: they describe every match
    assert first["data"]["totals"] == second["data"]["totals"]
    assert first["data"]["count"] == second["data"]["count"]
    assert first["data"]["private_included"] is False


def test_history_refuses_bad_input_and_answers_empty_for_a_stranger(chain):
    base = chain["base"]
    alice = chain["wallets"]["alice"]["address"]
    status, body = get(f"{base}/api/account/{alice}/nonsense")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
    status, body = get(f"{base}/api/account/NOTANADDRESS/history")
    assert status == 422 and body["error"]["code"] == "VALIDATION_ERROR"
    status, body = get(f"{base}/api/account/NOTANADDRESS/holdings")
    assert status == 422 and body["error"]["code"] == "VALIDATION_ERROR"
    stranger = "TOR1" + "a" * 40 + "onion"
    status, body = get(f"{base}/api/account/{stranger}/history")
    assert status == 200 and body["data"]["count"] == 0
    assert body["data"]["records"] == []
    assert body["data"]["totals"]["tor_in"] == 0.0
    # a wallet that has just been generated must open its pop-up, not read a 404
    status, body = get(f"{base}/api/account/{stranger}/holdings")
    assert status == 200
    fresh = body["data"]
    assert fresh["exists"] is False and fresh["address"] == stranger
    assert fresh["tor"]["balance_micro"] == 0 and fresh["tor"]["balance"] == 0.0
    assert fresh["tokens"] == [] and fresh["nfts"] == []
    assert fresh["role"] == "unfunded"

"""End-to-end tests for the Rust ledger CLI.

They are skipped when the binary has not been built yet:

    cd rust/torchain; cargo build
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
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
FOUNDER = ROOT / "data" / "wallets" / "founder.json"

pytestmark = pytest.mark.skipif(BINARY is None, reason="build rust/torchain first")


def run(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run([str(BINARY), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    assert result.returncode == expect, f"{args} -> {result.returncode}\n{result.stderr}"
    return result


def state_of(chain_dir: Path) -> dict:
    return json.loads((chain_dir / "state.json").read_text(encoding="utf-8"))


def test_genesis_mints_the_full_supply_to_the_founder(tmp_path):
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain),
        "--admin-token", "TEST-TOKEN")
    state = state_of(chain)
    assert state["total_supply"] == 5_000_000_000_000_000
    assert state["accounts"][state["founder"]]["balance"] == state["total_supply"]
    assert state["height"] == 0
    assert state["genesis_hash"] == state["head_hash"]
    assert len(state["records"]) == 1 and state["records"][0]["kind"] == "genesis"


def test_transfer_is_fee_free_on_the_base_layer(tmp_path):
    """The chain charges nothing; nodes/platforms add their own fees."""
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain),
        "--admin-token", "T")
    wallet_path = tmp_path / "user.json"
    run("wallet", "new", "--out", str(wallet_path), "--hide-mnemonic")
    user = json.loads(wallet_path.read_text(encoding="utf-8"))
    run("transfer", "--key", str(FOUNDER), "--to", user["address"],
        "--amount", "1.5", "--dir", str(chain))

    state = state_of(chain)
    founder = state["accounts"][state["founder"]]
    # exactly the amount leaves the founder: no protocol/transaction fee
    assert founder["balance"] == state["total_supply"] - 150_000_000
    assert founder["nonce"] == 1
    assert state["accounts"][user["address"]]["balance"] == 150_000_000
    transfer = next(record for record in state["records"]
                    if record["kind"] == "transfer")
    assert transfer["fee"] == 0
    assert not any(record["kind"] == "fee" for record in state["records"])
    assert sum(account["balance"] for account in state["accounts"].values()) == state["total_supply"]

    report = json.loads(run("verify", "--dir", str(chain)).stdout)
    assert report["ok"] is True and report["errors"] == []


def test_swap_moves_value_through_the_pool_account(tmp_path):
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain), "--admin-token", "T")
    run("token-create", "--key", str(FOUNDER), "--token", "MEME",
        "--amount", "1000000", "--dir", str(chain))
    run("pool-create", "--key", str(FOUNDER), "--token", "MEME",
        "--amount", "500000", "--tor", "100", "--dir", str(chain))
    run("swap", "--key", str(FOUNDER), "--token", "MEME", "--amount", "5",
        "--direction", "buy", "--dir", str(chain))
    run("swap", "--key", str(FOUNDER), "--token", "MEME", "--amount", "1000",
        "--direction", "sell", "--dir", str(chain))

    state = state_of(chain)
    pool = state["pools"]["MEME"]
    assert pool["tor_reserve"] > 100 * 10 ** 8          # bought 5 TOR in
    assert pool["token_reserve"] < 500_000
    assert json.loads(run("verify", "--dir", str(chain)).stdout)["ok"] is True


def test_records_are_permanent_and_never_sealed(tmp_path):
    """The 30-day window is gone: records stay public forever, no archive."""
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain),
        "--admin-token", "SEAL")
    wallet_path = tmp_path / "user.json"
    run("wallet", "new", "--out", str(wallet_path), "--hide-mnemonic")
    user = json.loads(wallet_path.read_text(encoding="utf-8"))
    run("transfer", "--key", str(FOUNDER), "--to", user["address"],
        "--amount", "2", "--dir", str(chain))

    listed = run("public", "--dir", str(chain)).stdout
    assert listed.startswith("2 permanent record")
    assert "genesis" in listed and "transfer" in listed
    # neither the record nor the state carries an expiry clock any more
    record = state_of(chain)["records"][-1]
    assert "public_until" not in record
    assert "public_ttl_days" not in state_of(chain)
    # the archive command is gone with the window
    assert run("archive", "--dir", str(chain), "--admin-token", "SEAL",
               expect=1).returncode == 1
    assert not (chain / "private" / "records.jsonl").exists()
    assert run("private", "--dir", str(chain), "--admin-token", "WRONG",
               expect=1).returncode == 1


def test_coin_command_publishes_the_pinned_logo():
    from core import coin

    document = json.loads(run("coin").stdout)
    assert document["name"] == coin.COIN_NAME
    assert document["symbol"] == coin.COIN_SYMBOL
    assert document["decimals"] == 8
    assert document["image"] == document["logo_url"] == coin.LOGO_URL
    assert document["logo_ipfs"] == f"ipfs://{coin.LOGO_CID}"
    assert document["properties"]["logo_cid"] == coin.LOGO_CID


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.25)
    return False




def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_http_public_ledger_publishes_balances_and_totals(tmp_path):
    """Balances and totals are public: no header, no token, no session."""
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain),
        "--admin-token", "PUBLIC")
    wallet_path = tmp_path / "user.json"
    run("wallet", "new", "--out", str(wallet_path), "--hide-mnemonic")
    user = json.loads(wallet_path.read_text(encoding="utf-8"))
    run("transfer", "--key", str(FOUNDER), "--to", user["address"],
        "--amount", "3", "--dir", str(chain))

    port = _free_port()
    process = subprocess.Popen(
        [str(BINARY), "serve", "--bind", f"127.0.0.1:{port}", "--dir", str(chain)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health")
        base = f"http://127.0.0.1:{port}"
        doc = _get_json(f"{base}/api/public/ledger?limit=1")["data"]
        assert doc["standard"] == "oneness-tor-public-ledger/1"
        assert doc["accounts_total"] == 2 and len(doc["accounts"]) == 1
        assert doc["totals"]["accounts"] == 2
        assert doc["totals"]["balance_micro"] == doc["total_supply_micro"]

        page = _get_json(f"{base}/api/public/ledger?limit=1&offset=1")["data"]
        assert page["offset"] == 1 and len(page["accounts"]) == 1
        addresses = {doc["accounts"][0]["address"], page["accounts"][0]["address"]}
        assert addresses == {state_of(chain)["founder"], user["address"]}
        row = next(row for row in doc["accounts"] + page["accounts"]
                   if row["address"] == user["address"])
        assert row["balance_micro"] == 3 * 10 ** 8
        assert row["token_balances"] == {}

        # the single-address read needs no credential either
        account = _get_json(f"{base}/api/account/{user['address']}")["data"]
        assert account["balance_micro"] == 3 * 10 ** 8
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()

def test_http_record_lookup_is_open_and_records_never_expire(tmp_path):
    """Every record is public over HTTP; there is no window and no seal."""
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain),
        "--admin-token", "WINDOW")
    txid = state_of(chain)["records"][0]["txid"]
    port = _free_port()
    process = subprocess.Popen(
        [str(BINARY), "serve", "--bind", f"127.0.0.1:{port}", "--dir", str(chain)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health")
        time.sleep(1.1)  # a second later nothing has expired: there is no clock
        base = f"http://127.0.0.1:{port}/api/chain/tx/{txid}"
        with urllib.request.urlopen(base, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["ok"] is True and body["data"]["txid"] == txid
        assert "public_until" not in body["data"]

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/public/records", timeout=5) as records:
            payload = json.loads(records.read().decode("utf-8"))["data"]
        assert [row["txid"] for row in payload["records"]] == [txid]
        assert "window_days" not in payload

        # the archive route went with the window
        archive = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/admin/archive", data=b"{}", method="POST",
            headers={"x-admin-token": "WINDOW"})
        with pytest.raises(urllib.error.HTTPError) as gone:
            urllib.request.urlopen(archive, timeout=5)
        assert gone.value.code == 404
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()


def _post_json(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    head = {"content-type": "application/json"}
    if token:
        head["x-admin-token"] = token
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=head, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _signed(wallet_path: Path, *, nonce: int, to: str, token: str,
            amount: int = 1, note: str = "", kind: str = "nft_transfer") -> dict:
    """A real Ed25519 signature over exactly the bytes ``chain.rs`` verifies."""
    from tools.torchain_signer import sign_transaction

    wallet = json.loads(Path(wallet_path).read_text(encoding="utf-8"))
    return sign_transaction(wallet, {
        "kind": kind, "from": wallet["address"], "to": to,
        "amount": amount, "token": token, "note": note, "external_ref": "",
        "nonce": nonce, "timestamp": int(time.time())})


def _nonce(chain_dir: Path, address: str) -> int:
    return int(state_of(chain_dir)["accounts"].get(address, {}).get("nonce", 0))


def test_nft_transfer_moves_the_edition_and_survives_a_restart(tmp_path):
    """Directive §3, every case, on the release binary over HTTP.

    Founder mints a unique edition (``token_create --amount 1``), transfers it to
    Alice, Alice transfers it to Bob; an unauthorized transfer, a replayed
    payload, a nonexistent NFT and a fungible token are each refused with the code
    the directive asks for; and the ownership, the history and the receipt are
    still right after the node is restarted.
    """
    if not FOUNDER.exists():
        pytest.skip("founder wallet missing")
    chain = tmp_path / "chain"
    run("init", "--founder", str(FOUNDER), "--dir", str(chain), "--admin-token", "NFT")
    alice_path, bob_path = tmp_path / "alice.json", tmp_path / "bob.json"
    run("wallet", "new", "--out", str(alice_path), "--hide-mnemonic")
    run("wallet", "new", "--out", str(bob_path), "--hide-mnemonic")
    alice = json.loads(alice_path.read_text(encoding="utf-8"))["address"]
    bob = json.loads(bob_path.read_text(encoding="utf-8"))["address"]
    founder = state_of(chain)["founder"]

    # 1. the founder creates the edition: the mint *is* a token_create with supply 1
    run("token-create", "--key", str(FOUNDER), "--token", "CAPNFT", "--amount", "1",
        "--note", "Captain First Edition", "--ref", "ipfs://bafycapnft",
        "--dir", str(chain))
    state = state_of(chain)
    assert state["tokens"]["CAPNFT"]["supply"] == 1
    assert state["tokens"]["CAPNFT"]["decimals"] == 0
    assert state["accounts"][founder]["tokens"]["CAPNFT"] == 1

    port = _free_port()
    process = subprocess.Popen(
        [str(BINARY), "serve", "--bind", f"127.0.0.1:{port}", "--dir", str(chain)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        assert _wait_http(f"{base}/health")

        # the ownership lookup answers before anybody moves it
        edition = _get_json(f"{base}/api/nft/CAPNFT")["data"]
        assert edition["standard"] == "oneness-tor-nft/1"
        assert edition["owner"] == founder and edition["creator"] == founder
        assert edition["holders"] == [founder] and edition["verified"] is True
        assert edition["metadata_uri"] == "ipfs://bafycapnft"
        assert edition["transfer_count"] == 0 and edition["last_transfer"] is None
        assert edition["fee"]["transfer_micro"] == 0
        catalogue = _get_json(f"{base}/api/nft")["data"]
        assert catalogue["count"] == 1
        assert catalogue["nfts"][0]["owner"] == founder
        # the mint is in the NFT history as a mint, not as a transfer
        history = _get_json(f"{base}/api/nft/history?symbol=CAPNFT")["data"]
        assert history["count"] == 1 and history["records"][0]["action"] == "mint"


        # 2. founder → alice, through the NFT door
        signed = _signed(FOUNDER, nonce=_nonce(chain, founder), to=alice,
                         token="CAPNFT", note="first sale")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 200, body
        receipt = body["data"]["receipt"]
        assert receipt["verified"] is True
        assert receipt["transfer"]["fee"] == 0
        assert receipt["transfer"]["signature"] == signed["signature"]
        assert receipt["ownership"]["owner_now"] == alice
        assert receipt["ownership"]["destination_holds"] is True
        assert receipt["ownership"]["source_holds"] is False
        assert receipt["confirmations"] >= 1 and receipt["status"] == "confirmed"
        first_txid = receipt["txid"]
        assert body["data"]["records"][0]["kind"] == "nft_transfer"
        # this door refuses any other kind instead of quietly sending coins
        wrong = dict(signed, kind="transfer")
        status, body = _post_json(f"{base}/api/nft/transfer", wrong)
        assert status == 422 and "nft_transfer" in body["error"]["message"], body

        # 3./4. alice owns it now; the founder holds no edition at all
        edition = _get_json(f"{base}/api/nft/CAPNFT")["data"]
        assert edition["owner"] == alice and edition["creator"] == founder
        assert edition["transfer_count"] == 1
        assert edition["last_transfer"]["to"] == alice
        assert edition["holders"] == [alice]
        held = _get_json(f"{base}/api/account/{alice}/nfts")["data"]
        assert held["count"] == 1 and held["nfts"][0]["symbol"] == "CAPNFT"
        assert _get_json(f"{base}/api/account/{founder}/nfts")["data"]["count"] == 0
        assert _get_json(f"{base}/api/account/{bob}/nfts")["data"]["count"] == 0
        history = _get_json(f"{base}/api/nft/history?symbol=CAPNFT")["data"]
        assert [row["action"] for row in history["records"]] == ["transfer", "mint"]
        incoming = _get_json(f"{base}/api/nft/history?address={alice}")["data"]
        assert incoming["records"][0]["direction"] == "in"
        assert incoming["records"][0]["counterparty"] == founder

        # 5./6. alice → bob through the general door: one kind, two doors
        signed = _signed(alice_path, nonce=_nonce(chain, alice), to=bob, token="CAPNFT")
        status, body = _post_json(f"{base}/api/chain/tx", signed)
        assert status == 200, body
        assert body["data"]["records"][0]["kind"] == "nft_transfer"
        assert _get_json(f"{base}/api/nft/CAPNFT")["data"]["owner"] == bob


        # 7. unauthorized: the founder no longer owns it
        signed = _signed(FOUNDER, nonce=_nonce(chain, founder), to=alice, token="CAPNFT")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 403 and body["error"]["code"] == "UNAUTHORIZED", body
        assert bob in body["error"]["message"]        # it names the real owner

        # 8. duplicate execution: the same bytes cannot land twice
        signed = _signed(bob_path, nonce=_nonce(chain, bob), to=alice, token="CAPNFT")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 200, body
        last_txid = body["data"]["records"][0]["txid"]
        status, body = _post_json(f"{base}/api/nft/transfer", dict(signed))
        assert status == 409 and body["error"]["code"] == "NONCE_MISMATCH", body
        assert _get_json(f"{base}/api/nft/CAPNFT")["data"]["owner"] == alice

        # 9. nonexistent NFT
        signed = _signed(alice_path, nonce=_nonce(chain, alice), to=bob, token="GHOST")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 404 and body["error"]["code"] == "UNKNOWN_TOKEN", body

        # a fungible token cannot be sneaked through the NFT door. The mint goes
        # through the API, not the CLI: a running node holds its chain in memory
        # (§19.10), so a CLI write is invisible to it and the nonce would drift.
        status, body = _post_json(f"{base}/api/chain/tx", _signed(
            FOUNDER, nonce=_nonce(chain, founder), to=founder, token="MEME",
            amount=1000, note="meme coin", kind="token_create"))
        assert status == 200, body
        signed = _signed(FOUNDER, nonce=_nonce(chain, founder), to=alice, token="MEME")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 422 and body["error"]["code"] == "VALIDATION_ERROR", body
        assert "not a unique edition" in body["error"]["message"]
        # an edition is exactly one unit: two is refused, not coerced
        signed = _signed(alice_path, nonce=_nonce(chain, alice), to=bob,
                         token="CAPNFT", amount=2)
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 422 and "amount must be 1" in body["error"]["message"], body
        # and a transfer to the current holder is not a transfer
        signed = _signed(alice_path, nonce=_nonce(chain, alice), to=alice, token="CAPNFT")
        status, body = _post_json(f"{base}/api/nft/transfer", signed)
        assert status == 409 and body["error"]["code"] == "CONFLICT", body
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()

    # 10. ownership, history and the receipt survive a cold start
    process = subprocess.Popen(
        [str(BINARY), "serve", "--bind", f"127.0.0.1:{port}", "--dir", str(chain)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_http(f"{base}/health")
        edition = _get_json(f"{base}/api/nft/CAPNFT")["data"]
        assert edition["owner"] == alice and edition["transfer_count"] == 3
        assert edition["verified"] is True and edition["holders"] == [alice]
        receipt = _get_json(f"{base}/api/nft/tx/{last_txid}")["data"]
        assert receipt["verified"] is True
        assert receipt["ownership"]["owner_now"] == alice
        assert receipt["transfer"]["from"] == bob
        history = _get_json(f"{base}/api/nft/history?symbol=CAPNFT")["data"]
        assert history["count"] == 4          # mint + three transfers
        assert [row["action"] for row in history["records"]] \
            == ["transfer", "transfer", "transfer", "mint"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()

    report = json.loads(run("verify", "--dir", str(chain)).stdout)
    assert report["ok"] is True and report["errors"] == []
    final = state_of(chain)
    assert final["accounts"][alice]["tokens"]["CAPNFT"] == 1
    assert final["accounts"][bob]["tokens"].get("CAPNFT", 0) == 0
    assert final["accounts"][founder]["tokens"].get("CAPNFT", 0) == 0
    assert final["tokens"]["CAPNFT"]["owner"] == alice
    # one edition, one holder, forever — the supply never moved
    assert sum(account["tokens"].get("CAPNFT", 0)
               for account in final["accounts"].values()) == 1


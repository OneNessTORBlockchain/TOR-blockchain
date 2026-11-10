"""The NFT transfer door through the real Flask API, masked and unmasked.

Two things only this file can prove, because they live in the Python layer and
not in the ledger:

* ``POST /api/chain/nft/transfer`` is a real door: it signs, broadcasts and
  returns the node's receipt, and it refuses any kind that is not
  ``nft_transfer``;
* **the public door masks an anonymous NFT transfer.** §19.10 closed the side
  door around ``/api/account/<address>/history``; an NFT history, an ownership
  lookup and a receipt all carry the sender's address too, so all three run
  through ``privacy.public_view`` and none of them may leak what
  ``GET /api/chain/tx/<txid>`` hides.

Everything goes through sockets: a real ``torchain`` node on a free port, the
real Flask app on another, Ed25519 signatures made by the repository's own
signer (``tools/torchain_signer.py``).
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = [
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain",
]
BINARY = next((path for path in CANDIDATES if path.exists()), None)
FOUNDER = ROOT / "data" / "wallets" / "founder.json"
TOKEN = "pytest-nft-admin-token"

pytestmark = pytest.mark.skipif(
    BINARY is None or not FOUNDER.exists(),
    reason="build rust/torchain and the founder wallet first")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return True
        except requests.RequestException:
            time.sleep(0.25)
    return False


def run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([str(BINARY), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"{args} -> {result.returncode}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    folder = tmp_path_factory.mktemp("nft-chain")
    run("init", "--founder", str(FOUNDER), "--dir", str(folder),
        "--admin-token", TOKEN)
    port = free_port()
    process = subprocess.Popen(
        [str(BINARY), "serve", "--bind", f"127.0.0.1:{port}", "--dir", str(folder)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    assert wait_http(url + "/health"), "torchain serve did not start"
    yield {"url": url, "dir": folder, "token": TOKEN}
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()


@pytest.fixture(scope="module")
def api(chain, tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("nft-api-data")
    shutil.copytree(ROOT / "data" / "wallets", data_dir / "wallets")
    sandbox = pytest.MonkeyPatch()
    sandbox.setenv("ONENESS_CHAIN_DIR", str(chain["dir"]))
    sandbox.setenv("ONENESS_NODE_API", chain["url"])
    sandbox.setenv("ONENESS_ADMIN_TOKEN", TOKEN)
    sandbox.setenv("ONENESS_DATA_DIR", str(data_dir))
    import core.node_client as node_module

    node_module.node._base = chain["url"]
    from oneness_tor.app import create_app

    app = create_app()
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    sandbox.setenv("ONENESS_PUBLIC_BASE_URL", base)
    sandbox.setenv("ONENESS_INTERNAL_BASE_URL", base)
    assert wait_http(base + "/api/health")
    yield {"base": base, "token": TOKEN, "chain": chain, "tmp": data_dir}
    server.shutdown()
    sandbox.undo()



def sign(wallet_path: Path, *, nonce: int, kind: str, to: str, token: str,
         amount: int, note: str = "", ref: str = "") -> dict:
    """Sign with the repository's own signer — the bytes ``chain.rs`` verifies."""
    from tools.torchain_signer import sign_transaction

    wallet = json.loads(Path(wallet_path).read_text(encoding="utf-8"))
    return sign_transaction(wallet, {
        "kind": kind, "from": wallet["address"], "to": to, "amount": amount,
        "token": token, "note": note, "external_ref": ref, "nonce": nonce,
        "timestamp": int(time.time())})


def nonce_of(chain: dict, address: str) -> int:
    state = json.loads((Path(chain["dir"]) / "state.json").read_text(encoding="utf-8"))
    return int(state["accounts"].get(address, {}).get("nonce", 0))


def get(api: dict, path: str, *, admin: bool = True) -> dict:
    headers = {"x-admin-token": api["token"]} if admin else {}
    response = requests.get(api["base"] + path, headers=headers, timeout=30)
    assert response.status_code == 200, \
        f"{path} -> {response.status_code} {response.text[:300]}"
    return response.json()


def post(api: dict, path: str, body: dict | None = None, *,
         admin: bool = True) -> requests.Response:
    headers = {"x-admin-token": api["token"]} if admin else {}
    return requests.post(api["base"] + path, json=body or {}, headers=headers,
                         timeout=60)


def test_an_anonymous_nft_transfer_is_masked_on_every_new_door(api, tmp_path):
    """Mint on the founder wallet, sell it anonymously, then read both sides."""
    from oneness_tor.services import privacy

    founder = json.loads(FOUNDER.read_text(encoding="utf-8"))
    found = founder["address"]

    # a wallet this node has never seen: the TOR Anonymous Wallet's own key
    anon_path = tmp_path / "anon-wallet.json"
    run("wallet", "new", "--out", str(anon_path), "--hide-mnemonic")
    anon = json.loads(anon_path.read_text(encoding="utf-8"))["address"]
    assert anon != found

    # 1. mint: a unique edition is a token_create with supply 1
    response = post(api, "/api/chain/tx", sign(
        FOUNDER, nonce=nonce_of(api["chain"], found), kind="token_create",
        to=found, token="ANONART", amount=1, note="Captain Anonymous Edition",
        ref="ipfs://bafyanonart"))
    assert response.status_code == 200, response.text[:400]
    edition = get(api, "/api/chain/nft/ANONART", admin=False)["data"]
    assert edition["symbol"] == "ANONART" and edition["owner"] == found
    assert edition["metadata_uri"] == "ipfs://bafyanonart"
    assert edition["verified"] is True

    # 2. sell it anonymously, through the public NFT door
    response = post(api, "/api/chain/nft/transfer", sign(
        FOUNDER, nonce=nonce_of(api["chain"], found), kind="nft_transfer",
        to=anon, token="ANONART", amount=1, note="anonymous sale",
        ref=privacy.chain_ref(privacy.ANONYMOUS)), admin=False)
    assert response.status_code == 200, response.text[:400]
    receipt = response.json()["data"]["receipt"]
    txid = receipt["txid"]
    assert receipt["verified"] is True
    assert receipt["ownership"]["owner_now"] == anon
    # the public receipt already masks the sender; the receiver never is masked
    assert receipt["transfer"]["from"] == privacy.label()
    assert receipt["transfer"]["to"] == anon
    assert receipt["anonymous"] is True

    # 3. ownership lookup: owner is the new holder, the seller is masked
    public = get(api, "/api/chain/nft/ANONART", admin=False)["data"]
    assert public["owner"] == anon
    # the creator is NOT masked, and that is the rule working rather than missing:
    # the mint was a public act (its external_ref is the metadata document, not a
    # privacy choice), so only the *anonymous sale* hides its sender
    assert public["creator"] == found
    assert public["last_transfer"]["from"] == privacy.label()
    assert public["last_transfer"]["to"] == anon
    # the admin door still reads the ledger as it is
    admin = get(api, "/api/chain/nft/ANONART")["data"]
    assert admin["owner"] == anon
    assert admin["creator"] == found
    assert admin["last_transfer"]["from"] == found


    # 4. history: masked for the public, raw for the admin, on both filters
    history = get(api, "/api/chain/nft/history?symbol=ANONART", admin=False)["data"]
    transfer_row = next(row for row in history["records"] if row["action"] == "transfer")
    assert transfer_row["from"] == privacy.label()
    assert transfer_row["to"] == anon
    incoming = get(api, f"/api/chain/nft/history?address={anon}", admin=False)["data"]
    row = incoming["records"][0]
    assert row["direction"] == "in"
    assert row["counterparty"] == privacy.label()   # recomputed from masked fields
    raw = get(api, f"/api/chain/nft/history?address={anon}")["data"]["records"][0]
    assert raw["from"] == found and raw["counterparty"] == found

    # 5. the receipt, both sides
    masked = get(api, f"/api/chain/nft/tx/{txid}", admin=False)["data"]
    assert masked["transfer"]["from"] == privacy.label()
    assert masked["masked_fields"] == ["from"]
    assert masked["verified"] is True
    truth = get(api, f"/api/chain/nft/tx/{txid}")["data"]
    assert truth["transfer"]["from"] == found

    # 6. the wallet's NFT pane answers for an address this node never saw
    holdings = get(api, f"/api/account/{anon}/nfts", admin=False)["data"]
    assert holdings["count"] == 1
    assert holdings["nfts"][0]["symbol"] == "ANONART"
    assert holdings["nfts"][0]["metadata_uri"] == "ipfs://bafyanonart"
    assert get(api, f"/api/account/{found}/nfts", admin=False)["data"]["count"] == 0

    # 7. the door refuses a kind that is not an NFT transfer
    wrong = post(api, "/api/chain/nft/transfer", sign(
        FOUNDER, nonce=nonce_of(api["chain"], found), kind="transfer",
        to=anon, token="TOR", amount=1), admin=False)
    assert wrong.status_code == 400      # the API layer's validation status
    assert "nft_transfer" in wrong.json()["error"]["message"]

    # 8. and the same signed bytes cannot land twice
    replay = sign(anon_path, nonce=nonce_of(api["chain"], anon),
                  kind="nft_transfer", to=found, token="ANONART", amount=1)
    assert post(api, "/api/chain/nft/transfer", replay,
                admin=False).status_code == 200
    again = post(api, "/api/chain/nft/transfer", dict(replay), admin=False)
    assert again.json()["error"]["code"] == "NONCE_MISMATCH", again.text[:300]
    # ownership moved back, and the edition is still single-holder
    final = get(api, "/api/chain/nft/ANONART", admin=False)["data"]
    assert final["owner"] == found and final["holders"] == [found]


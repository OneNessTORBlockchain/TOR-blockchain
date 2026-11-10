"""End-to-end tests: a real ``torchain`` node + the real Flask server over HTTP.

Every assertion here goes through sockets — the node on a free port, the API on
another free port — so the fetch-data tool, the wallet signatures, the on-chain
fees and the static pages are all exercised exactly as the browser does.

Skipped when the Rust binary or the founder wallet is missing.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

from core import api_catalog, coin
from core.config import settings

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = [
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain.exe",
    ROOT / "rust" / "torchain" / "target" / "release" / "torchain",
    ROOT / "rust" / "torchain" / "target" / "debug" / "torchain",
]
BINARY = next((path for path in CANDIDATES if path.exists()), None)
FOUNDER = ROOT / "data" / "wallets" / "founder.json"
USER1 = ROOT / "data" / "wallets" / "user1.json"
TOKEN = "pytest-admin-token-oneness"

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


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    folder = tmp_path_factory.mktemp("chain")
    init = subprocess.run(
        [str(BINARY), "init", "--founder", str(FOUNDER), "--dir", str(folder),
         "--admin-token", TOKEN],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert init.returncode == 0, init.stderr
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
    data_dir = tmp_path_factory.mktemp("api-data")
    shutil.copytree(ROOT / "data" / "wallets", data_dir / "wallets")
    sandbox = pytest.MonkeyPatch()
    sandbox.setenv("ONENESS_CHAIN_DIR", str(chain["dir"]))
    sandbox.setenv("ONENESS_NODE_API", chain["url"])
    sandbox.setenv("ONENESS_ADMIN_TOKEN", TOKEN)
    sandbox.setenv("ONENESS_DATA_DIR", str(data_dir))
    sandbox.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:9")
    sandbox.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    import core.node_client as node_module

    node_module.node._base = chain["url"]
    from oneness_tor.app import create_app

    app = create_app()
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    sandbox.setenv("ONENESS_PUBLIC_BASE_URL", base)
    # what `python -m oneness_tor` sets at boot: the fetch tool calls *this*
    # process over loopback, never the hostname the browser used
    sandbox.setenv("ONENESS_INTERNAL_BASE_URL", base)
    assert wait_http(base + "/api/health")
    yield {"base": base, "token": TOKEN, "chain": chain, "data_dir": data_dir}
    server.shutdown()
    sandbox.undo()


def headers(api: dict) -> dict:
    return {"x-admin-token": api["token"]}


def get(api: dict, path: str) -> dict:
    response = requests.get(api["base"] + path, timeout=30, headers=headers(api))
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:300]}"
    return response.json()


def post(api: dict, path: str, body: dict | None = None, *, admin: bool = True,
         expect: int = 200) -> requests.Response:
    response = requests.post(api["base"] + path, json=body or {},
                             headers=headers(api) if admin else {}, timeout=120)
    assert response.status_code == expect, (
        f"{path} -> {response.status_code} {response.text[:400]}")
    return response


# --------------------------------------------------------------- reference
def test_health_coin_and_pricing(api):
    health = get(api, "/api/health")["data"]
    assert health["node_reachable"] is True
    assert health["service"] == "oneness-tor-api"

    coin_doc = get(api, "/api/coin")["data"]
    assert coin_doc["symbol"] == "TOR" and coin_doc["decimals"] == 8
    assert coin_doc["name"] == coin.COIN_NAME
    assert coin_doc["image"] == coin.LOGO_URL
    assert coin_doc["properties"]["logo_cid"] == coin.LOGO_CID

    rows = {row["code"]: row for row in get(api, "/api/pricing")["data"]["rows"]}
    assert rows["contract_create"]["tor"] == 5.0 and rows["contract_create"]["locked"]
    assert rows["mint"]["tor"] == 3.0
    assert rows["transfer"]["tor"] == 0.1
    assert "ai_website" not in rows and "hosting_30d" not in rows


def test_chain_endpoints_are_live(api):
    info = get(api, "/api/chain/info")["data"]
    assert info["chain_id"] == "072316"
    assert int(info["total_supply_micro"]) == 50_000_000 * 10 ** 8

    account = get(api, f"/api/account/{info['founder']}")["data"]
    assert int(account["balance_micro"]) == int(info["total_supply_micro"])

    blocks = get(api, "/api/chain/blocks?from=0")["data"]
    assert blocks["count"] >= 1 and blocks["blocks"][0]["height"] == 0

    records = get(api, "/api/public/records?limit=5")["data"]
    assert records["records"]

    verify = get(api, "/api/chain/verify")["data"]
    assert int(verify["founder_balance"]) == int(info["total_supply_micro"])

    sync = get(api, "/api/chain/sync")["data"]
    assert sync["mode"] == "single-node-poa"
    assert sync["catch_up"]["endpoint"] == "/api/chain/blocks"


def test_catalog_matches_the_flask_routes():
    from oneness_tor.app import create_app

    adapter = create_app().url_map.bind("127.0.0.1")
    missing = []
    for entry in api_catalog.ALL:
        path = re.sub(r"\{[^}]+\}", "x", entry["path"])
        try:
            adapter.match(path, method=entry["method"])
        except Exception as problem:  # noqa: BLE001 - reported below
            missing.append(f"{entry['name']} {entry['method']} {path}: {problem}")
    assert not missing, "\n".join(missing)


# ------------------------------------------------------------- fetch tool
def test_fetch_tool_returns_the_real_http_response(api):
    info = get(api, "/api/chain/info")["data"]
    result = post(api, "/api/tools/fetch", {"endpoint": "chain.info"}, admin=False).json()["data"]
    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["request"]["url"].startswith(api["base"])
    assert result["response"]["data"]["height"] == info["height"]
    assert result["latency_ms"] >= 0

    failure = post(api, "/api/tools/fetch",
                   {"endpoint": "account.get", "params": {"address": "nope"}},
                   admin=False)
    assert failure.json()["data"]["http_status"] in (400, 404, 422)

    unknown = post(api, "/api/tools/fetch", {"endpoint": "nope.nope"}, admin=False, expect=400)
    assert unknown.json()["error"]["code"] == "VALIDATION_ERROR"


def test_fetch_tool_uses_loopback_for_an_onion_visitor(api):
    """A Tor visitor's Host must never leak into the in-process tool call.

    Browsing the hidden service sends ``Host: <id>.onion``. Dialling that name
    from inside the server fails (no SOCKS resolver), which is exactly how
    ``TOOL_UNREACHABLE`` used to appear for onion users.
    """
    onion = "4thdw6cvoacqtxpyvxziebiif5qmiwginb7wwgktsalmevrabqubpxid.onion"
    response = requests.post(api["base"] + "/api/tools/fetch",
                             json={"endpoint": "chain.info"}, timeout=30,
                             headers={**headers(api), "Host": onion})
    assert response.status_code == 200, response.text[:300]
    result = response.json()["data"]
    assert result["ok"] is True, result["error"]
    assert ".onion" not in result["request"]["url"]
    assert result["request"]["url"].startswith(api["base"])
    assert result["http_status"] == 200


def test_tools_catalog_exposes_the_single_tool(api):
    document = get(api, "/api/tools")["data"]
    assert document["tool"] == "fetch_oneness_data"
    assert document["count"] == len(api_catalog.ALL)
    assert document["spec"]["function"]["parameters"]["required"] == ["endpoint"]


# ----------------------------------------------------------------- wallets
@pytest.mark.skipif(not USER1.exists(),
                    reason="data/wallets/user1.json is not in this checkout — "
                           "create a second wallet to run the transfer test")
def test_wallets_list_and_a_real_transfer(api):
    wallets = get(api, "/api/wallets")["data"]["wallets"]
    assert {"founder", "user1"} <= {wallet["name"] for wallet in wallets}
    assert all("secret" not in json.dumps(wallet).lower() for wallet in wallets)

    user1 = json.loads(USER1.read_text(encoding="utf-8"))["address"]
    before_response = requests.get(api["base"] + f"/api/account/{user1}", timeout=30,
                                   headers=headers(api))
    before = (int(before_response.json()["data"]["balance_micro"])
              if before_response.status_code == 200 else 0)  # unknown account == no funds
    sent = post(api, "/api/wallets/founder/send",
                {"to": user1, "amount": 0.5, "note": "pytest transfer"}).json()["data"]
    assert sent["txid"] and int(sent["height"]) >= 1
    assert int(sent["amount_micro"]) == 50_000_000
    after = int(get(api, f"/api/account/{user1}")["data"]["balance_micro"])
    assert after == before + 50_000_000
    assert sent["balance_micro"] < 50_000_000 * 10 ** 8

    signed = post(api, "/api/wallets/founder/sign",
                  {"kind": "transfer", "to": user1, "amount": 1}).json()["data"]
    assert signed["signed_tx"]["signature"] and signed["unit"] == "microTOR"


def test_admin_routes_reject_a_missing_or_wrong_token(api):
    assert requests.post(api["base"] + "/api/private/records", json={},
                         timeout=30).status_code == 403
    assert requests.post(api["base"] + "/api/private/records", json={},
                         headers={"x-admin-token": "wrong"},
                         timeout=30).status_code == 403


def test_the_window_routes_are_gone(api):
    """The 30-day window and its archive were removed; the routes must 404."""
    assert requests.post(api["base"] + "/api/admin/archive", json={},
                         timeout=30).status_code == 404
    assert requests.get(api["base"] + "/api/policy", timeout=30).status_code == 404
    assert requests.post(api["base"] + "/api/policy/sweep", json={},
                         timeout=30).status_code == 404


# ---------------------------------------------------------------- payments
def test_invoice_lifecycle_settles_on_chain(api):
    created = post(api, "/api/payments/invoices",
                   {"code": "contract_create", "wallet": "founder",
                    "note": "pytest invoice"}).json()["data"]["invoice"]
    assert created["status"] == "open"
    assert int(created["amount_micro"]) == 500_000_000
    assert created["address"].startswith("TOR1")

    paid = post(api, f"/api/payments/invoices/{created['id']}/pay",
                {"wallet": "founder"}).json()["data"]
    assert paid["invoice"]["status"] == "paid"
    assert paid["txid"] and int(paid["height"]) >= 1

    again = requests.post(api["base"] + f"/api/payments/invoices/{created['id']}/pay",
                          json={"wallet": "founder"}, headers=headers(api), timeout=60)
    assert again.status_code == 400  # already paid

    revenue = get(api, "/api/payments/revenue")["data"]
    assert revenue["count"] >= 1
    assert float(revenue["total_tor"]) >= 5

    quote = get(api, "/api/payments/quote?code=contract_create&quantity=2")["data"]
    assert quote["tor"] == 10.0 and quote["micro_tor"] == 1_000_000_000


# --------------------------------------------------------------- contracts
def test_contract_deploy_compiles_and_call_updates_state(api):
    templates = {row["id"] for row in get(api, "/api/contracts/templates")["data"]["templates"]}
    assert {"escrow", "token_gate", "subscription"} <= templates

    created = post(api, "/api/contracts",
                   {"name": "pytest-escrow", "template": "escrow",
                    "wallet": "founder", "owner_wallet": "founder"}).json()["data"]
    contract = created["contract"]
    assert contract["status"] == "deployed"
    assert contract["charge"]["amount_micro"] == 500_000_000
    assert contract["source_sha3"] and len(contract["source_sha3"]) == 64
    if contract["compile"]["checked"]:
        assert contract["compile"]["ok"] is True, contract["compile"].get("stderr")

    called = post(api, f"/api/contracts/{contract['id']}/call",
                  {"function": "deposit", "args": {"amount_tor": 2}}).json()["data"]
    assert called["state"]["amount_micro"] == 200_000_000
    released = post(api, f"/api/contracts/{contract['id']}/call",
                    {"function": "release", "args": {}}).json()["data"]
    assert released["result"] == 200_000_000
    assert released["state"]["released"] is True

    nodes = get(api, "/api/contracts/nodes")["data"]["nodes"]
    assert nodes and nodes[0]["authorized"] is True


def test_broken_contract_source_is_rejected(api):
    from oneness_tor.services.contracts import find_rustc

    if find_rustc() is None:
        pytest.skip("rustc is not installed — compilation cannot be verified")
    response = requests.post(
        api["base"] + "/api/contracts",
        json={"name": "broken", "source": "pub fn call( {", "wallet": "founder"},
        headers=headers(api), timeout=120)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "does not compile" in error["message"]


# --------------------------------------------------------------------- tor
def test_tor_status_is_honest(api):
    status = get(api, "/api/tor/status")["data"]
    assert status["control_port"] == 9051
    assert isinstance(status["reachable"], bool)
    if not status["reachable"]:
        assert status["error"], "an unreachable daemon must explain itself"


def test_a_generated_landing_page_is_served(api):
    from oneness_tor.services import builder

    site = builder.create(
        "pytest landing page",
        "<!DOCTYPE html><html><head><title>pytest</title></head>"
        "<body><h1>pytest site</h1></body></html>",
        model="test-model", wallet="founder", txid="test-txid",
        title="Pytest Site",
        charge={"amount_micro": 1_000_000_000, "amount_tor": "10"})
    page = requests.get(f"{api['base']}/sites/{site['id']}/", timeout=30)
    assert page.status_code == 200
    assert "pytest site" in page.text
    assert page.headers["x-oneness-site"] == site["id"]


# ------------------------------------------------------------------ ollama
def test_ollama_status_prompt_and_offline_bottom(api):
    status = get(api, "/api/ollama/status")["data"]
    assert status["reachable"] is False
    assert status["error"] and status["api_url"].startswith("http://")

    prompt = get(api, "/api/ollama/prompt")["data"]
    assert prompt["endpoint_count"] == len(api_catalog.ALL)
    assert "fetch_oneness_data" in prompt["system_prompt"]
    assert "chain.info" in prompt["system_prompt"]
    assert "microTOR" in prompt["system_prompt"]

    chat = requests.post(api["base"] + "/api/ollama/chat", json={"prompt": "hello"},
                         headers=headers(api), timeout=60)
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "OLLAMA_UNAVAILABLE"


# ------------------------------------------------------------------- pages
def test_every_page_and_the_logo_are_served(api):
    for path in ["/", "/dashboard", "/explorer", "/wallet", "/payments",
                 "/contracts", "/workbench", "/meme", "/nft",
                 "/ollama", "/docs", "/coin"]:
        response = requests.get(api["base"] + path, timeout=30)
        assert response.status_code == 200, path
        assert "<!DOCTYPE html>" in response.text
        assert "OneNess" in response.text
    image = requests.get(api["base"] + "/assets/images/TOR%20Coin%20Logo.png", timeout=60)
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")
    assert len(image.content) > 1000

    docs = get(api, "/api/docs")["data"]
    assert docs["count"] == len(api_catalog.ALL)
    assert docs["node"]["reachable"] is True
    assert docs["base_url"] == api["base"]
    assert docs["base_urls"]["current"] == api["base"]
    assert all(entry["url"].startswith(api["base"]) for entry in docs["endpoints"])
    openapi = requests.get(api["base"] + "/api/openapi.json", timeout=30).json()
    assert openapi["openapi"] == "3.1.0"
    assert openapi["servers"][0]["url"] == api["base"]


# ------------------------------------------------------------ meme launcher
def test_meme_launcher_token_pool_swap_and_quotes(api):
    created = post(api, "/api/meme/tokens", {
        "symbol": "MEME", "name": "Meme Onion", "supply": 1_000_000,
        "description": "a community coin on TOR", "wallet": "founder",
    }).json()["data"]
    token = created["token"]
    assert token["symbol"] == "MEME" and token["supply"] == 1_000_000
    assert token["owner"] == created["launcher"]["owner"]
    assert created["txid"]
    assert created["fee"]["charged_by"].startswith("the Rust chain")

    duplicate = post(api, "/api/meme/tokens",
                     {"symbol": "MEME", "supply": 10}, expect=400)
    assert duplicate.json()["error"]["code"] == "VALIDATION_ERROR"

    pool = post(api, "/api/meme/pools", {
        "symbol": "MEME", "token_amount": 500_000, "tor_amount": 100,
        "wallet": "founder",
    }).json()["data"]
    assert int(pool["pool"]["token_reserve"]) == 500_000
    assert int(pool["pool"]["tor_reserve"]) == 100 * 10 ** 8

    quote_row = get(api, "/api/meme/quote?symbol=MEME&side=buy&amount=5")["data"]
    assert quote_row["executable"] is True and quote_row["amount_out"] > 0
    before = int(get(api, "/api/meme/tokens/MEME/market")["data"]["pool"]["token_reserve"])
    swapped = post(api, "/api/meme/pools/MEME/swap",
                   {"side": "buy", "amount": 5, "wallet": "founder"}).json()["data"]
    after = int(get(api, "/api/meme/tokens/MEME/market")["data"]["pool"]["token_reserve"])
    # the chain applied exactly the quote's integer math
    assert before - after == int(quote_row["amount_out"])
    assert swapped["txid"]

    sold = post(api, "/api/meme/pools/MEME/swap",
                {"side": "sell", "amount": 1000, "wallet": "founder"}).json()["data"]
    assert int(sold["quote"]["amount_micro_out"]) > 0

    market = get(api, "/api/meme/tokens/MEME/market")["data"]
    assert market["indexed"] is True
    assert market["volume"]["swaps_in_window"] >= 2
    assert market["chart"]["embed"] is None  # no fake candles


def test_meme_paid_tools_promotions_and_honest_placeholders(api):
    promoted = post(api, "/api/meme/promotions", {
        "symbol": "MEME", "kind": "trending", "days": 2, "wallet": "founder",
    }).json()["data"]
    assert promoted["charge"]["amount_tor"] == "10"
    board = get(api, "/api/meme/trending")["data"]
    assert any(row["symbol"] == "MEME" and row["promoted"] for row in board["board"])

    session = post(api, "/api/meme/volume-sessions", {
        "symbol": "MEME", "rounds": 5, "wallet": "founder",
    }).json()["data"]
    assert session["session"]["status"] == "queued"
    assert session["charge"]["amount_tor"] == "10"
    run = requests.post(api["base"] + session["session"]["run_endpoint"], json={},
                        headers=headers(api), timeout=30)
    assert run.status_code == 501
    assert run.json()["error"]["code"] == "ENGINE_NOT_DEPLOYED"
    assert run.json()["error"]["details"]["engine"] == "not_deployed"

    venues = get(api, "/api/meme/dex/venues")["data"]
    assert venues["available"] == 1 and venues["placeholders"] >= 1
    external = requests.get(api["base"] + "/api/meme/dex/quote?symbol=MEME&amount=1",
                            timeout=30)
    assert external.status_code == 501
    assert external.json()["error"]["code"] == "DEX_NOT_DEPLOYED"
    external_swap = requests.post(api["base"] + "/api/meme/dex/swap",
                                  json={"symbol": "MEME", "amount": 1},
                                  headers=headers(api), timeout=30)
    assert external_swap.status_code == 501

    link = get(api, "/api/meme/dex/link?url=https://dexscreener.com/solana/token/"
                     "So11111111111111111111111111111111111111112")["data"]
    assert link["ok"] is True
    assert link["mint"] == "So11111111111111111111111111111111111111112"

    tools = get(api, "/api/meme/tools")["data"]
    assert tools["placeholders"] >= 3
    assert any(row["id"] == "volume_bot" and not row["ready"]
               for row in tools["tools"])


# ------------------------------------------------------------- AI-NFT studio
def test_ai_nft_mint_metadata_and_prompt(api):
    minted = post(api, "/api/nft/mint", {
        "name": "Cyan Warden",
        "description": "A cyan-lit warden of the onion relays.",
        "image": "https://example.com/warden.png", "symbol": "WARDEN",
        "attributes": [{"trait_type": "Aura", "value": "cyan"}],
        "wallet": "founder",
    }).json()["data"]
    record = minted["nft"]
    assert record["token_id"] == 1
    assert minted["charge"]["amount_tor"] == "3"
    assert minted["prompt"].splitlines()[0] == "I AM THIS NFT"
    assert "A cyan-lit warden" in minted["prompt"]
    assert "- Token ID: #1" in minted["prompt"]

    meta = requests.get(api["base"] + "/api/nft/metadata",
                        params={"address": record["address"], "token_id": 1},
                        timeout=30)
    assert meta.status_code == 200, meta.text[:300]
    payload = meta.json()["data"]
    assert payload["nft"]["source"] == "oneness-tor"
    assert payload["nft"]["attributes"][0]["value"] == "cyan"
    assert payload["prompt"].splitlines()[0] == "I AM THIS NFT"

    by_id = get(api, "/api/nft/catalogue/nft_0001")["data"]
    assert by_id["nft"]["symbol"] == "WARDEN"
    catalogue = get(api, "/api/nft/catalogue")["data"]
    assert catalogue["count"] >= 1 and catalogue["price_tor"] == 3.0

    missing = requests.get(api["base"] + "/api/nft/metadata",
                          params={"address": "TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion",
                                  "token_id": 99}, timeout=30)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NFT_NOT_FOUND"

    chat = requests.post(api["base"] + "/api/nft/chat", json={
        "address": record["address"], "token_id": 1, "prompt": "who are you?"},
        headers=headers(api), timeout=60)
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "OLLAMA_UNAVAILABLE"

    command = requests.post(api["base"] + "/api/ai/command", json={
        "prompt": "who are you?",
        "nft": {"address": record["address"], "token_id": 1}},
        headers=headers(api), timeout=60)
    assert command.status_code == 503
    assert command.json()["error"]["code"] == "OLLAMA_UNAVAILABLE"


# ------------------------------------------- TOR Anonymous Wallet: the read panes
def test_account_holdings_and_history_answer_for_any_address(api):
    """TOKEN/NFT HOLDINGS and TRANSACTION HISTORY, through the product surface.

    A wallet generated in a product's own keystore owns an address this node has
    never seen, so these two routes answer by **address** and never by wallet
    name. The numbers still come from the ledger: nothing here is cached, summed
    in the browser or invented by the pop-up.
    """
    founder = get(api, "/api/chain/info")["data"]["founder"]
    holdings = get(api, f"/api/account/{founder}/holdings")["data"]
    assert holdings["address"] == founder
    assert holdings["tor"]["symbol"] == "TOR" and holdings["tor"]["decimals"] == 8
    assert int(holdings["tor"]["balance_micro"]) > 0
    assert isinstance(holdings["tokens"], list) and isinstance(holdings["nfts"], list)
    assert holdings["token_count"] == len(holdings["tokens"])
    assert holdings["nft_count"] == len(holdings["nfts"])

    history = get(api, f"/api/account/{founder}/history")["data"]
    assert history["address"] == founder and history["count"] >= 1
    genesis = history["records"][0]
    assert genesis["direction"] in {"in", "out", "self"}
    assert genesis["confirmed"] is True and genesis["confirmations"] >= 1
    assert genesis["status"] == "confirmed"
    assert genesis["detail"].startswith("/api/chain/tx/")
    assert genesis["block_hash"] and genesis["txid"]
    assert "tor_in" in history["totals"] and "tor_out" in history["totals"]
    # the page bound is honoured and the totals do not shrink with it
    page = get(api, f"/api/account/{founder}/history?limit=1")["data"]
    assert len(page["records"]) == 1 and page["limit"] == 1
    assert page["totals"] == history["totals"]


def test_an_address_the_node_never_saw_reads_empty_not_an_error(api):
    """A freshly generated wallet must open its pop-up before it owns anything."""
    stranger = "TOR1" + "b" * 40 + "onion"
    history = get(api, f"/api/account/{stranger}/history")["data"]
    assert history["count"] == 0 and history["records"] == []
    assert history["totals"]["tor_in"] == 0.0
    # a generated wallet has no account row yet: zeros, not a 404, or the first
    # thing a new user sees is an error box
    fresh = get(api, f"/api/account/{stranger}/holdings")["data"]
    assert fresh["exists"] is False and fresh["address"] == stranger
    assert fresh["tor"]["balance_micro"] == 0
    assert fresh["tokens"] == [] and fresh["nfts"] == []
    bad = requests.get(api["base"] + "/api/account/NOTANADDRESS/history",
                       timeout=30, headers=headers(api))
    assert bad.status_code in (400, 422)
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"



def test_the_public_history_masks_an_anonymous_sender(api):
    """One masking rule, two doors: the history route must agree with /chain/tx.

    A real anonymous transfer is signed here (``--ref oneness-privacy:1|mode=anonymous``)
    and then read back through the *public* history route. The sender's address may
    not appear in ``from`` or in ``counterparty``; the receiver must still appear,
    because the receiver never chose to be hidden.
    """
    chain = api["chain"]
    recipient = "TOR1" + "c" * 40 + "onion"
    # Sign a real anonymous transfer and POST it through the product surface —
    # the same route the pop-up's SEND pane uses (§19.4.3). A CLI write would not
    # do: the served node holds its state in memory, so a transfer appended to
    # state.json behind its back is invisible to it.
    from tools.torchain_signer import sign_transaction

    wallet = json.loads(FOUNDER.read_text(encoding="utf-8"))
    account = get(api, f"/api/account/{wallet['address']}")["data"]
    signed = sign_transaction(wallet, {
        "kind": "transfer", "from": wallet["address"], "to": recipient,
        "amount": 100_000_000, "token": None, "note": "",
        "external_ref": "oneness-privacy:1|mode=anonymous",
        "nonce": int(account["nonce"]), "timestamp": int(time.time())})
    accepted = post(api, "/api/chain/tx", signed).json()
    assert accepted["ok"] is True, accepted
    assert accepted["data"]["accepted"] == 1

    history = get(api, f"/api/account/{recipient}/history")["data"]
    assert any(record["to"] == recipient for record in history["records"]), (
        f"no record for {recipient}; rows={[record.get('to') for record in history['records']]}")
    row = next(record for record in history["records"] if record["to"] == recipient)
    assert row["direction"] == "in"
    assert row["privacy"] == "anonymous" and row["anonymous"] is True
    assert row["to"] == recipient                        # the receiver is never hidden
    # the admin door (this helper sends the token) still reads the real ledger
    assert row["from"] == wallet["address"]
    assert row["counterparty"] == wallet["address"]
    # the public door masks, exactly as GET /api/chain/tx does for the same caller
    public = requests.get(api["base"] + f"/api/account/{recipient}/history", timeout=30)
    assert public.status_code == 200
    anon = next(record for record in public.json()["data"]["records"]
                if record["to"] == recipient)
    assert anon["from"] == "Tor-Anonymous-onion"
    assert anon["counterparty"] == "Tor-Anonymous-onion"
    assert anon["masked_fields"] == ["from"]
    assert anon["to"] == recipient
    detail = requests.get(api["base"] + f"/api/chain/tx/{row['txid']}", timeout=30).json()["data"]
    assert detail["from"] == "Tor-Anonymous-onion"
    assert detail["to"] == recipient


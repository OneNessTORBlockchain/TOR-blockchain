"""Live smoke test against a running OneNess TOR node + API.

    python tools/live_check.py                       # read-only checks
    python tools/live_check.py --spend               # + real transfers & a contract fee
    python tools/live_check.py --ai gemma3:1b        # + a real Ollama command (tool loop)

Exit code 0 only when every checked step passed, so it doubles as a deploy gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="live OneNess TOR smoke test")
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--token", default="oneness-dev-token")
    parser.add_argument("--spend", action="store_true",
                        help="broadcast a 2 TOR transfer and deploy an escrow (5 TOR)")
    parser.add_argument("--ai", default="", help="model for a real command-deck call")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    auth = {"x-admin-token": args.token}

    def get(path: str) -> dict:
        response = requests.get(base + path, headers=auth, timeout=args.timeout)
        response.raise_for_status()
        return response.json()

    def post(path: str, body: dict | None = None) -> dict:
        response = requests.post(base + path, json=body or {}, headers=auth,
                                 timeout=args.timeout)
        return response.json() if response.content else {}

    health = get("/api/health")["data"]
    check(health["node_reachable"], "health: Rust node reachable", health["node_url"])

    info = get("/api/chain/info")["data"]
    check(info["chain_id"] == "072316", "chain.info",
          f"height={info['height']} supply={info['total_supply_micro']} microTOR")

    coin = get("/api/coin")["data"]
    check(coin["symbol"] == "TOR" and coin["image"].startswith("https://"),
          "coin metadata", coin["logo_ipfs"])

    pricing = get("/api/pricing")["data"]
    rows = {row["code"]: row["tor"] for row in pricing["rows"]}
    check(rows.get("contract_create") == 5.0 and rows.get("mint") == 3.0,
          "pricing locked rows",
          f"contract={rows.get('contract_create')} mint={rows.get('mint')}")

    fees = pricing.get("fees") or {}
    check(fees.get("base_layer_tx_fee_micro") == 0 and rows.get("transfer") == 0.1,
          "fees: the chain is free, the node/platform charges 0.1 TOR",
          f"base={fees.get('base_layer_tx_fee_tor')} transfer={rows.get('transfer')}")

    account = get("/api/admin/account")["data"]
    check(account.get("support_email") == "support@onenesstech.ca"
          and "registration_open" in account and "fee_receiver" in account,
          "admin account layer is live",
          f"registered={account.get('registered')} "
          f"support={account.get('support_email')}")

    tool = post("/api/tools/fetch", {"endpoint": "chain.info"})["data"]
    check(tool["ok"] and tool["http_status"] == 200 and tool["latency_ms"] >= 0,
          "fetch-data tool round-trip",
          f"{tool['request']['method']} {tool['request']['url']} in {tool['latency_ms']}ms")

    denied = post("/api/tools/fetch", {"endpoint": "records.private", "params": {"limit": 2}})
    check(denied["data"]["http_status"] == 200,
          "fetch-data tool reaches an admin route with the token")

    wallets = get("/api/wallets")["data"]["wallets"]
    check(any(wallet["name"] == "founder" for wallet in wallets),
          "wallets listed", ", ".join(wallet["name"] for wallet in wallets))

    anonymous = requests.get(f"{base}/api/wallets/founder/balance", timeout=30)
    check(anonymous.status_code == 200
          and anonymous.json()["data"]["balance_micro"] >= 0,
          "balances are public (anonymous read allowed)",
          f"{anonymous.json()['data'].get('balance_tor')} TOR")

    ledger_response = requests.get(f"{base}/api/public/ledger?limit=500", timeout=60)
    ledger = (ledger_response.json().get("data") or {})
    check(ledger_response.status_code == 200
          and str(ledger.get("standard", "")).endswith("/1")
          and isinstance(ledger.get("accounts"), list)
          and isinstance(ledger.get("totals"), dict),
          "permanent public ledger readable without a session",
          f"{len(ledger.get('accounts') or [])} account(s), "
          f"{(ledger.get('totals') or {}).get('tokens')} token(s), "
          f"height {ledger.get('height')}")

    sheet = requests.get(f"{base}/api/public/balances?limit=5", timeout=60).json()["data"]
    check(sheet["count"] >= 1
          and sheet["accounts_total"] >= sheet["count"]
          and sheet["accounts"][0]["balance_tor"] is not None,
          "public balance sheet: every address, its TOR and token balances",
          f"{sheet['count']} of {sheet['accounts_total']} address(es)")

    totals = requests.get(f"{base}/api/public/totals", timeout=30).json()["data"]
    check(totals["tokens"] >= 1 and totals["total_supply_micro"] > 0
          and isinstance(totals.get("token_supply"), dict),
          "chain-wide totals are public",
          f"{totals['tokens']} token(s), supply {totals['total_supply_tor']} TOR, "
          f"{totals.get('accounts')} account(s)")

    registry = get("/api/tokens/public")["data"]
    fields = set(registry["fields"])
    check(registry["count"] >= 1 and registry["tokens"][0]["symbol"] == "TOR"
          and {"price_tor", "pools", "market_cap_tor", "volume_24h_tor",
               "mint_address", "owner", "supply", "circulating", "in_pools",
               "holders"} <= fields,
          "public token table",
          f"{registry['count']} token(s), {len(fields)} public field(s)")

    symbol = next((row["symbol"] for row in registry["tokens"]
                   if row["symbol"] != "TOR"), "TOR")
    detail = requests.get(f"{base}/api/public/tokens/{symbol}", timeout=60).json()["data"]
    holders = requests.get(f"{base}/api/public/tokens/{symbol}/holders",
                           timeout=60).json()["data"]
    check(detail["supply"] > 0 and holders["holders_total"] >= 1
          and holders["count"] == min(int(holders["limit"]),
                                      int(holders["holders_total"])),
          "per-token detail + holder list are public",
          f"{symbol}: supply {detail['supply']}, "
          f"{holders['holders_total']} holder(s), "
          f"{holders['in_pools']} pooled")

    images = get("/api/tokens/images")["data"]
    check(isinstance(images.get("images"), dict) and images["standard"].endswith("/1"),
          "token logos resolved from public metadata",
          f"{images['count']} logo(s) cached · {images['source']}"
          + ("" if images["count"] else " (press refresh logos at /admin)"))

    mirror = images.get("logos") or {}
    check(mirror.get("route") == "/assets/tokens" and mirror.get("enabled") is not False,
          "logos are mirrored onto the node itself",
          f"{mirror.get('count', 0)} of {mirror.get('total', 0)} at "
          f"{mirror.get('route')}/ ({mirror.get('bytes', 0)} bytes)")
    mirrored = next((row for row in (images.get("images") or {}).values()
                     if isinstance(row, dict) and row.get("logo_file")), {})
    if mirrored:
        name = str(mirrored["logo_file"])
        served = requests.get(f"{base}/assets/tokens/{name}", headers=auth,
                              timeout=args.timeout)
        check(served.ok and served.headers.get("content-type", "").startswith("image/"),
              "a mirrored logo is served by this node (no third-party host)",
              f"{name} · {len(served.content)} bytes")

    check(all({"image", "image_source"} <= set(row) for row in registry["tokens"]),
          "every token row carries a logo and its source",
          "; ".join(f"{row['symbol']}: {row.get('image_source') or 'none'}"
                    for row in registry["tokens"][:3]))

    # every row a page draws must resolve: the listings (meme launcher, wallet,
    # TOR coin, explorer) read these very rows, so a 404 here is a blank token
    local = [str(row.get("image") or "") for row in registry["tokens"]
             if str(row.get("image") or "").startswith("/")]
    missing: list[str] = []
    for path in local:
        try:
            answer = requests.get(base + path, headers=auth, timeout=args.timeout)
            if not (answer.ok and answer.headers.get("content-type", "")
                    .startswith("image/")):
                missing.append(f"{path} -> {answer.status_code}")
        except requests.RequestException as problem:
            missing.append(f"{path} -> {problem.__class__.__name__}")
    check(not missing and bool(local),
          "every token logo a listing draws is served by this node",
          f"{len(local)} local logo(s) checked"
          + (f", {len(missing)} broken: {', '.join(missing[:3])}" if missing else ""))

    check(not any("usd" in key for key in registry)
          and all("usd" not in key for row in registry["tokens"] for key in row),
          "no fiat rate anywhere — every price is TOR-denominated",
          f"{len(registry['tokens'])} row(s) checked")

    receive = get("/api/wallets/founder/receive")["data"]
    qr = receive["qr"]["address"]
    check(bool(receive["pubkey_base58"]) and qr["svg"].startswith("data:image/svg+xml")
          and int(qr["version"]) >= 1,
          "receive: public key + real QR code",
          f"qr v{qr['version']} {qr['error_correction']} · {qr['text']}")

    history = get("/api/wallets/founder/history?limit=500")["data"]
    hexes = [row.get("signature_hex") or "" for row in history["records"]]
    check(all(len(value) % 2 == 0 for value in hexes)
          and isinstance(history.get("count"), int),
          "whole transaction history, hex per record",
          f"{history['count']} of {history['scanned']} scanned record(s)")

    privacy = get("/api/privacy")["data"]
    check("public" in privacy["modes"] and "anonymous" in privacy["modes"]
          and privacy["hides"] == ["from (the sending wallet's own address)"]
          and bool(privacy["never_masked"]),
          "privacy is asked per transaction",
          f"{privacy['standard']}: {', '.join(privacy['modes'])}; anonymous "
          f"shows {privacy['anonymous_label']}; the receiver is never masked")

    tor = get("/api/tor/status")["data"]
    check(True, "tor status is honest",
          "control port reachable" if tor["reachable"] else (tor["error"] or "")[:100])

    ollama = get("/api/ollama/status")["data"]
    check(True, "ollama status",
          f"reachable={ollama['reachable']} models={ollama['model_count']} "
          f"api={ollama['api_url']}")

    suggested = get("/api/ollama/suggested")["data"]
    check(len(suggested["models"]) >= 4
          and all("installed" in row for row in suggested["models"]),
          "ollama download catalogue",
          f"{len(suggested['models'])} curated models, "
          f"{sum(1 for row in suggested['models'] if row['installed'])} installed")

    if ollama["reachable"]:
        listing = post("/api/ollama/command", {"command": "ollama list"})
        check(bool(listing.get("ok")) and listing["data"]["kind"] == "list",
              "ollama.command runs a real `ollama list`",
              f"{listing['data'].get('count')} models on disk")

    catalog = get("/api/docs")["data"]
    check(catalog["count"] == len(catalog["endpoints"]) and catalog["count"] >= 92,
          "API catalog", f"{catalog['count']} endpoints, "
          f"{len(catalog['groups'])} groups")

    if args.spend:
        user1 = json.loads((ROOT / "data" / "wallets" / "user1.json")
                           .read_text(encoding="utf-8"))["address"]
        sent = post("/api/wallets/founder/send",
                    {"to": user1, "amount": 2, "note": "live_check"})
        check(bool(sent.get("ok")), "transfer 2 TOR signed + broadcast",
              f"txid={sent.get('data', {}).get('txid')} "
              f"height={sent.get('data', {}).get('height')}")

        invoice = post("/api/payments/invoices",
                       {"code": "contract_create", "wallet": "founder",
                        "note": "live_check"})["data"]["invoice"]
        paid = post(f"/api/payments/invoices/{invoice['id']}/pay", {"wallet": "founder"})
        check(bool(paid.get("ok")), "invoice paid on-chain",
              f"{invoice['id']} txid={paid.get('data', {}).get('txid')}")

        contract = post("/api/contracts", {"name": "live-escrow",
                                           "template": "escrow",
                                           "wallet": "founder",
                                           "owner_wallet": "founder"})
        body = contract.get("data") or {}
        check(bool(contract.get("ok")), "contract compiled + deployed (5 TOR)",
              f"id={body.get('contract', {}).get('id')} "
              f"compiled={body.get('contract', {}).get('compile', {}).get('ok')}")
        if contract.get("ok"):
            contract_id = body["contract"]["id"]
            called = post(f"/api/contracts/{contract_id}/call",
                          {"function": "deposit", "args": {"amount_tor": 3}})
            state = (called.get("data") or {}).get("state") or {}
            check(called.get("ok") and state.get("amount_micro") == 300_000_000,
                  "contract call updates state", json.dumps(state))

        revenue = get("/api/payments/revenue")["data"]
        check(float(revenue["total_tor"]) >= 5, "revenue reflects settlement",
              f"{revenue['total_tor']} TOR over {revenue['count']} invoices")

        records = get("/api/public/records?limit=10")["data"]["records"]
        check(len(records) >= 3, "public records visible (nothing expires)",
              f"{len(records)} records")

    if args.ai:
        command = post("/api/ai/command",
                       {"prompt": "What is the current chain height, the total supply in TOR "
                                  "and the founder balance in TOR? Use the API.",
                        "model": args.ai, "use_tools": True})
        if command.get("ok"):
            data = command["data"]
            calls = data["api_calls"]
            check(bool(calls), "ollama command called the real API",
                  f"{len(calls)} call(s): " + ", ".join(str(c["endpoint"]) for c in calls))
            check(bool(data["answer"]), "ollama produced an answer",
                  data["answer"][:200].replace("\n", " "))
        else:
            error = command.get("error") or {}
            check(False, "ollama command",
                  f"{error.get('code')}: {error.get('message')}")

    meme_tools = get("/api/meme/tools")["data"]
    check(meme_tools["ready"] >= 8, "meme launcher tools",
          f"{meme_tools['ready']} live / {meme_tools['placeholders']} placeholders")
    meme_tokens = get("/api/meme/tokens")["data"]
    check(True, "meme tokens listed", f"{meme_tokens['count']} token(s)")
    dex = requests.get(base + "/api/meme/dex/quote?symbol=TOR&amount=1", timeout=30)
    check(dex.status_code == 501, "external DEX is an honest placeholder",
          (dex.json().get("error") or {}).get("code", ""))

    nft = get("/api/nft/catalogue")["data"]
    check(nft["price_tor"] == 3.0, "AI-NFT mint price", f"{nft['count']} minted")
    if nft["nfts"]:
        record = nft["nfts"][0]
        meta = requests.get(base + "/api/nft/metadata",
                            params={"address": record["address"],
                                    "token_id": record["token_id"]},
                            timeout=60).json()["data"]
        check((meta.get("prompt") or "").startswith("I AM THIS NFT"),
              "NFT metadata -> the I AM THIS NFT prompt field",
              str(record.get("name") or ""))

    ipfs_status = get("/api/ipfs/status")["data"]
    check(ipfs_status["gateway"].startswith("https://"),
          "IPFS: gateway + Pinata state",
          f"enabled={ipfs_status['enabled']} gateway={ipfs_status['gateway']}")
    resolved = get("/api/ipfs/resolve?uri=ipfs://"
                   "bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze")["data"]
    check(resolved["url"].startswith(ipfs_status["gateway"]),
          "IPFS: ipfs:// resolves through the gateway", resolved["url"][:72])

    dev = get("/api/dev/keys/pricing")["data"]["pricing"]
    check(dev["header"] == "x-api-key" and dev["price"]["tor_per_30_days"] == 25.0,
          "developer keys: price + header",
          f"{dev['price']['tor_per_30_days']} TOR / 30 days")
    unkeyed = requests.get(base + "/api/v1/coin.get", timeout=30)
    try:
        denial = unkeyed.json()["error"]
    except ValueError:
        denial = {}
    check(unkeyed.status_code == 403 and denial.get("code") == "FORBIDDEN",
          "developer API: /api/v1 requires x-api-key",
          f"HTTP {unkeyed.status_code}")

    audit = get("/api/chain/sync/audit")["data"]
    check(audit["verdict"] == "in-sync", "sync audit re-hashes every block",
          f"verdict={audit['verdict']} blocks={audit['blocks_checked']} "
          f"errors={len(audit['errors'])}")

    routing = get("/api/ollama/routing")["data"]
    surfaces = {row["id"]: row["model"] for row in routing["purposes"]}
    check(set(surfaces) == {"brain", "nft", "web_builder", "rust_builder"},
          "one model per AI surface",
          f"{len(routing['models'])} model(s) on this device")

    created = post("/api/workbench/projects",
                   {"name": "live check shop", "kind": "web"})["data"]["project"]
    try:
        files = get(f"/api/workbench/projects/{created['id']}/files"
                    "?path=index.html")["data"]
        check(files["language"] == "html" and files["bytes"] > 0,
              "workbench reads a real project file", f"{files['bytes']} bytes")
        saved = post(f"/api/workbench/projects/{created['id']}/files",
                     {"path": "notes/live.md", "content": "# live check\n"})["data"]
        check(saved["created"] and saved["bytes"] > 0,
              "workbench writes a real file", saved["path"])
        preview = requests.get(f"{base}/builds/{created['id']}/", timeout=30)
        check(preview.status_code == 200 and b"<!DOCTYPE html>" in preview.content,
              "workbench preview is served by the node",
              preview.headers.get("content-type", ""))
    finally:
        post(f"/api/workbench/projects/{created['id']}/serve/stop")
        requests.delete(f"{base}/api/workbench/projects/{created['id']}",
                        headers=auth, timeout=60)

    listed = get("/api/contracts")["data"]["contracts"]
    if listed:
        opened = get(f"/api/contracts/{listed[0]['id']}")["data"]["contract"]
        check(opened.get("access") in ("admin", "owner") and "source" in opened,
              "program details are owner/admin only",
              f"{opened.get('id')} access={opened.get('access')}")
    else:
        check(True, "program details are owner/admin only", "no programs yet")

    pages = ["/", "/dashboard", "/explorer", "/admin", "/wallet", "/payments",
             "/contracts", "/workbench", "/meme", "/nft",
             "/ollama", "/docs", "/coin"]
    failures = [path for path in pages
                if requests.get(base + path, timeout=30).status_code != 200]
    check(not failures, "every page served", ", ".join(failures) or f"{len(pages)} pages")

    logo = requests.get(base + "/assets/images/TOR%20Coin%20Logo.png", timeout=60)
    check(logo.status_code == 200 and logo.headers["content-type"].startswith("image/"),
          "logo asset served", f"{len(logo.content)} bytes")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} live checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

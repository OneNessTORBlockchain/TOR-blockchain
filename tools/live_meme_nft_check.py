"""Live proof for the meme launcher + AI-NFT studio against the running stack.

    python tools/live_meme_nft_check.py
    python tools/live_meme_nft_check.py --model gemma3:1b --no-chat

Every check is a real HTTP call; paid steps broadcast signed transactions on the
demo chain (5 TOR token, 10 TOR pool, 5 TOR promotion, 10 TOR session, 3 TOR NFT).
Exit code 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys

import requests

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="live meme + NFT smoke test")
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--token", default="oneness-dev-token")
    parser.add_argument("--model", default="gemma3:1b")
    parser.add_argument("--no-chat", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    auth = {"x-admin-token": args.token}

    def get(path: str) -> dict:
        response = requests.get(base + path, timeout=90)
        response.raise_for_status()
        return response.json()

    def post(path: str, body: dict | None = None) -> tuple[int, dict]:
        response = requests.post(base + path, json=body or {}, headers=auth,
                                 timeout=300)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"raw": response.text[:300]}

    tools = get("/api/meme/tools")["data"]
    check(tools["ready"] >= 8 and tools["placeholders"] >= 3, "meme.tools",
          f"{tools['ready']} live / {tools['placeholders']} placeholders")

    existing = {row["symbol"] for row in get("/api/meme/tokens")["data"]["tokens"]}
    symbol = next((f"LIVE{n}" for n in range(1, 10) if f"LIVE{n}" not in existing), None)
    if not check(symbol is not None, "pick a free ticker", str(sorted(existing))):
        return 1

    status, created = post("/api/meme/tokens", {
        "symbol": symbol, "name": f"Live Proof {symbol}", "supply": 250_000,
        "description": "created by tools/live_meme_nft_check.py",
        "image": "https://example.com/coin.png", "wallet": "founder"})
    data = created.get("data") or {}
    check(status == 200 and (data.get("token") or {}).get("supply") == 250_000,
          "meme.token_create (5 TOR charged by the chain)",
          f"txid={data.get('txid')} height={data.get('height')}")
    if status != 200:
        print(json.dumps(created, indent=2)[:900])
        return 1

    status, pooled = post("/api/meme/pools", {
        "symbol": symbol, "token_amount": 100_000, "tor_amount": 50,
        "wallet": "founder"})
    pool = (pooled.get("data") or {}).get("pool") or {}
    check(status == 200 and int(pool.get("token_reserve") or 0) == 100_000,
          "meme.pool_create (10 TOR + reserves moved on-chain)",
          f"txid={(pooled.get('data') or {}).get('txid')}")
    if status != 200:
        print(json.dumps(pooled, indent=2)[:900])
        return 1

    quote_row = get(f"/api/meme/quote?symbol={symbol}&side=buy&amount=2")["data"]
    before = int(get(f"/api/meme/tokens/{symbol}/market")["data"]["pool"]["token_reserve"])
    status, swapped = post(f"/api/meme/pools/{symbol}/swap",
                           {"side": "buy", "amount": 2, "wallet": "founder"})
    after = int(get(f"/api/meme/tokens/{symbol}/market")["data"]["pool"]["token_reserve"])
    check(status == 200 and before - after == int(quote_row["amount_out"]),
          "meme.swap — the chain matched the quote exactly",
          f"{quote_row['amount_out']} {symbol} for 2 TOR, "
          f"txid={(swapped.get('data') or {}).get('txid')}")

    market = get(f"/api/meme/tokens/{symbol}/market")["data"]
    check(bool(market["indexed"]) and market["volume"]["swaps_in_window"] >= 1,
          "meme.market", f"price={market['price_tor']} TOR, "
          f"liquidity={market['liquidity_tor']} TOR")

    status, promo = post("/api/meme/promotions", {
        "symbol": symbol, "kind": "trending", "days": 1, "wallet": "founder"})
    check(status == 200
          and ((promo.get("data") or {}).get("charge") or {}).get("amount_tor") == "5",
          "meme.promote (5 TOR/day)",
          f"ends {(promo.get('data') or {}).get('ends_at')}")
    board = get("/api/meme/trending")["data"]["board"]
    check(any(row["symbol"] == symbol and row["promoted"] for row in board),
          "meme.trending shows the paid slot")

    status, session = post("/api/meme/volume-sessions", {
        "symbol": symbol, "rounds": 5, "wallet": "founder"})
    session_row = ((session.get("data") or {}).get("session") or {})
    check(status == 200 and session_row.get("status") == "queued",
          "meme.volume_session_create (10 TOR, queued)",
          f"id={session_row.get('id')}")
    run_status, run = post(session_row.get("run_endpoint")
                           or "/api/meme/volume-sessions/x/run")
    check(run_status == 501
          and (run.get("error") or {}).get("code") == "ENGINE_NOT_DEPLOYED",
          "meme.volume_run is honest (501, no trades)",
          (run.get("error") or {}).get("message", "")[:90])

    dex_status, dex = post("/api/meme/dex/swap", {"symbol": symbol, "amount": 1})
    check(dex_status == 501
          and (dex.get("error") or {}).get("code") == "DEX_NOT_DEPLOYED",
          "meme.dex_swap placeholder (501)")
    link = get("/api/meme/dex/link?url=https://dexscreener.com/solana/token/"
               "So11111111111111111111111111111111111111112")["data"]
    check(bool(link["ok"]) and link["mint"].startswith("So1111"),
          "meme.dex_link parser", link["mint"])

    svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='128' height='128'>"
           "<rect width='128' height='128' fill='#061018'/>"
           "<circle cx='64' cy='64' r='40' fill='none' stroke='#22f5ff' "
           "stroke-width='4'/></svg>")
    image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    status, minted = post("/api/nft/mint", {
        "name": "Live Cyan Warden",
        "description": "A cyan-lit warden of the onion relays, minted by the live check.",
        "image": image, "symbol": "WARDEN",
        "attributes": [{"trait_type": "Aura", "value": "cyan"}],
        "wallet": "founder"})
    minted_data = minted.get("data") or {}
    record = minted_data.get("nft") or {}
    check(status == 200
          and (minted_data.get("charge") or {}).get("amount_tor") == "3",
          "nft.mint (3 TOR on-chain)",
          f"#{record.get('token_id')} txid={minted_data.get('txid')}")
    if status != 200:
        print(json.dumps(minted, indent=2)[:900])
        return 1

    prompt = minted_data.get("prompt") or ""
    check(prompt.splitlines()[0] == "I AM THIS NFT"
          and "Live Cyan Warden" in prompt,
          "nft.mint returns the I AM THIS NFT prompt field", f"{len(prompt)} chars")

    meta = requests.get(base + "/api/nft/metadata",
                        params={"address": record.get("address"),
                                "token_id": record.get("token_id")}, timeout=90)
    meta_data = meta.json().get("data") or {}
    check(meta.status_code == 200
          and (meta_data.get("nft") or {}).get("source") == "oneness-tor"
          and (meta_data.get("prompt") or "").splitlines()[0] == "I AM THIS NFT",
          "nft.metadata resolves the stored record",
          str((meta_data.get("nft") or {}).get("image"))[:48] + "…")

    catalogue = get("/api/nft/catalogue")["data"]
    check(catalogue["count"] >= 1, "nft.catalogue",
          f"{catalogue['count']} minted, {catalogue['price_tor']} TOR each")

    if not args.no_chat:
        status, chat = post("/api/nft/chat", {
            "address": record.get("address"), "token_id": record.get("token_id"),
            "prompt": "Who are you, and what do you guard? Two sentences.",
            "model": args.model})
        chat_data = chat.get("data") or {}
        answer = str(chat_data.get("answer") or "")
        check(status == 200 and len(answer) > 10,
              "nft.chat — the model answered as the NFT",
              answer[:170].replace("\n", " "))
        check((chat_data.get("prompt_block") or "").startswith("I AM THIS NFT"),
              "nft.chat reports the exact prompt block",
              f"{len(chat_data.get('prompt_block') or '')} chars, system prompt "
              f"{chat_data.get('system_prompt_chars')} chars")

    passed = sum(1 for ok, _label, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} live checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

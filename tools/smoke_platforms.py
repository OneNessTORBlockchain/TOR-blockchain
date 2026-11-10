"""End-to-end smoke test of the six-platform surface, against a live API.

    python tools/smoke_platforms.py [--base http://127.0.0.1:8090]

Creates a throwaway account, then exercises the exact calls the apps make:
sign-in session, market reads, a real swap on a TOR pool (after funding the
fresh wallet from the founder), a USDC book quote, the AI ChatBox on a free
OpenRouter model, the banner rotation and a full TOR Pump launch.

It is a smoke test, not a test-suite: it *mints and trades* on whatever chain the
API points at, which is why it lives in ``tools/`` and is never imported by
pytest.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8090"
ADMIN_TOKEN = "14e477cb31413df1c4dc81933f7d4b10"


def call(path, *, body=None, session="", method=None, admin=False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if session:
        headers["x-wallet-session"] = session
    if admin:
        headers["x-admin-token"] = ADMIN_TOKEN
    request = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as problem:
        return json.loads(problem.read().decode())


def show(label, body, *, keys=()):
    if not body.get("ok"):
        print(f"  FAIL {label}: {body.get('error')}")
        return None
    data = body["data"]
    if keys and isinstance(data, dict):
        data = {key: data.get(key) for key in keys}
    text = json.dumps(data, default=str)
    print(f"  ok   {label}: {text[:220]}")
    return body["data"]


print("1. market engine")
overview = show("overview", call("/api/market/overview"), keys=("totals",))
show("tor", call("/api/market/tor"), keys=("price_usd", "anchor_usd", "market_cap_usd"))
pools = show("pools", call("/api/market/pools?limit=3"),
             keys=("count", "tor_pools", "usdc_pools", "liquidity_usd"))
show("chart", call("/api/market/chart/TOR?points=8"), keys=("points", "source"))
show("projects", call("/api/projects"), keys=("count",))
show("ads", call("/api/ads"), keys=("count",))
models = show("ai models", call("/api/ai/models"), keys=("count", "source"))

print("\n2. account + session")
username = f"smoke{int(time.time()) % 100000}"
signup = call("/api/auth/signup", body={"username": username, "password": "Smoke-test-2026"})
if not signup.get("ok"):
    raise SystemExit(f"signup failed: {signup.get('error')}")
session = signup["data"]["session"]["session"]
wallet = signup["data"]["wallet"]
print(f"  ok   wallet {wallet['name']} {wallet['address']}")

print("\n3. real swap on a TOR pool")
funded = call("/api/wallets/founder/send",
              body={"to": wallet["address"], "amount": 5}, admin=True)
show("fund the new wallet", funded, keys=("txid", "amount_micro"))
quote = show("quote", call("/api/market/quote?symbol=GRAM&side=buy&amount=0.5&base=TOR"),
             keys=("amount_out", "unit_out", "price_usd"))
swap = call("/api/market/swap", body={"symbol": "GRAM", "side": "buy", "amount": 0.5, "base": "TOR"},
            session=session)
show("swap", swap, keys=("txid", "height", "swap"))

print("\n4. USDC book")
show("usdc quote", call("/api/market/quote?symbol=BTC&side=buy&amount=10&base=USDC"),
     keys=("amount_out", "unit_out", "price_usd", "engine"))

print("\n5. AI-NFT ChatBox")
if models:
    answer = call("/api/ai/chat", body={"prompt": "In one short sentence: what is TOR?",
                                        "model": "openrouter/free"})
    show("chat", answer, keys=("model", "answer"))

print("\n6. TOR Pump launch")
ticker = f"SMK{int(time.time()) % 10000}"
launch = call("/api/pump/launch", body={"symbol": ticker, "name": "Smoke Coin",
                                        "description": "smoke test launch",
                                        "supply": 1_000_000_000, "first_buy_tor": 2},
              session=session)
if launch.get("ok"):
    data = launch["data"]
    print(f"  ok   launch {ticker}: pool {data.get('pool', {}).get('pool', {}).get('price_tor')} "
          f"tx {data.get('pool', {}).get('txid')} curve {data.get('curve', {}).get('progress')}")
    show("launch status", call(f"/api/pump/launches/{ticker}"),
         keys=("market_cap_usd", "progress", "graduated", "liquidity_usd"))
else:
    print(f"  FAIL launch: {launch.get('error')}")

print("\n7. listings + profiles")
show("tokens", call("/api/market/tokens?limit=2"), keys=("totals",))
show("token", call(f"/api/market/tokens/{ticker}"), keys=("price_usd", "market_cap_usd", "quote_assets"))
print("\nsmoke test complete")

"""Seed the TOR marketplace: platform wallets, 170 pools, first real trades.

    python tools/seed_market.py wallets        # platform wallets + fee receiver
    python tools/seed_market.py tor-pools      # 120 TOR pools, $20 000 000 total
    python tools/seed_market.py usdc-books     # 50 USDC books, $5 000 000 total
    python tools/seed_market.py trades         # first real swaps (charts + volume)
    python tools/seed_market.py all

Every pool is a real chain ``pool_create`` signed by the wallet that owns the
token: the tool funds that wallet with the TOR side plus the chain's 10 TOR fee,
then mints nothing — the token side comes from the supply the fleet mint already
put there. USDC books move their reserves into the custody wallet with real
transfers and record the book afterwards, so no book claims liquidity the
custody wallet does not hold.

``--dry-run`` prints the plan (pool count, exact USD total, TOR needed) and does
not touch the chain.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from core.validator import format_tor, to_micro  # noqa: E402
from oneness_tor.services import ledger, market_engine, meme, pool_books  # noqa: E402

FLEET = ROOT / "data" / "metadata" / "token-fleet.json"
TOR_TOTAL_USD = 20_000_000.0
USDC_TOTAL_USD = 5_000_000.0
USDC_BASE = "USDC"
USDC_SUPPLY = 2_500_000          # whole USDC moved into custody
PLATFORM_WALLETS = {
    "tor-ai-nft-wallet": 0.0,        # "TOR AI-NFT Wallet" — receives every platform fee
    "pump-owner": 120_000.0,         # $5 000 launch liquidity × launches
    "swap-usdc-pool": 500.0,         # custody wallet's TOR buffer
}
FEE_WALLET = "tor-ai-nft-wallet"
FEE_WALLET_LABEL = "Tor AI-NFT Wallet"


def _usd() -> float:
    return float(market_engine.snapshot()["tor"]["price_usd"] or 3.6)


def ladder(total_usd: float, count: int, *, low: float = 10_000.0,
           high: float = 1_000_000.0, exponent: float = 6.0) -> list[float]:
    """A rank-weighted ladder that sums to exactly ``total_usd``.

    The deepest pool sits at ``high`` ($1 000 000), the shallowest at ``low``
    ($10 000) and every pool in between is strictly smaller than the one above
    it — the shape the operator asked for, with the exact total the operator
    funded.
    """
    weights = [(count - index) ** exponent for index in range(count)]
    scale = total_usd / sum(weights)
    values = [max(low, min(high, round(weight * scale))) for weight in weights]
    residual = round(total_usd - sum(values))
    index = 0
    while residual != 0 and index < count:
        room = (high - values[index]) if residual > 0 else (low - values[index])
        step = int(min(abs(room), abs(residual)))
        values[index] += step if residual > 0 else -step
        residual -= step if residual > 0 else -step
        index += 1
    return sorted((float(value) for value in values), reverse=True)


def fleet_rows(exclude: tuple[str, ...] = ()) -> list[dict]:
    doc = json.loads(FLEET.read_text(encoding="utf-8"))
    entries = doc.get("entries") or []
    rows = []
    for entry in entries:
        symbol = str(entry.get("symbol") or "").upper()
        price = float(entry.get("price_usd") or 0)
        if not symbol or symbol in exclude or price <= 0:
            continue
        rows.append({"symbol": symbol, "name": entry.get("name"),
                     "price_usd": price, "supply": int(entry.get("supply") or 0),
                     "wallet": (entry.get("wallet") or {}).get("name"),
                     "image": entry.get("logo") or entry.get("image")})
    rows.sort(key=lambda row: row["price_usd"] * row["supply"], reverse=True)
    return rows


def _wallet_holdings(name: str, symbol: str) -> int:
    wallet = ledger.load_wallet(name)
    account = ledger.node_account(str(wallet.get("address") or ""))
    return int((account.get("tokens") or {}).get(symbol) or 0)


def ensure_wallets(*, dry: bool = False) -> list[dict]:
    """Create the platform wallets, fund them and point the fee receiver at TOR AI-NFT."""
    done = []
    for name, tor in PLATFORM_WALLETS.items():
        exists = True
        try:
            ledger.load_wallet(name)
        except Exception:  # noqa: BLE001 - the wallet simply does not exist yet
            exists = False
        if not exists:
            if dry:
                done.append({"wallet": name, "created": False, "dry": True})
                continue
            ledger.new_wallet(name)
        if tor:
            wallet = ledger.load_wallet(name)
            address = str(wallet.get("address") or "")
            account = ledger.node_account(address)
            held = int(account.get("balance_micro") or 0)
            top_up = to_micro(tor) - held
            if top_up > 0 and not dry:
                ledger.send_micro("founder", address, top_up,
                                  note=f"platform funding: {name}")
        done.append({"wallet": name, "created": not exists,
                     "address": str(ledger.load_wallet(name).get("address") or ""),
                     "target_tor": tor})
    if not dry:
        from core.node_client import node

        receiver = ledger.load_wallet(FEE_WALLET)
        node.set_fee_receiver(str(receiver.get("address") or ""))
    return done


def tor_pool_plan() -> list[dict]:
    usd = _usd()
    rows = fleet_rows()[:120]
    values = ladder(TOR_TOTAL_USD, len(rows))
    plan = []
    for row, value in zip(rows, values):
        price_tor = row["price_usd"] / usd
        tor_amount = value / usd
        tokens_in = int(round(tor_amount / price_tor))
        held = _wallet_holdings(row["wallet"], row["symbol"]) if row["wallet"] else 0
        plan.append({**row, "liquidity_usd": value, "tor_amount": round(tor_amount, 8),
                     "tokens_in": min(tokens_in, int(held * 0.9)), "held": held,
                     "price_tor": price_tor})
    return plan


def seed_tor_pools(*, dry: bool = False, offset: int = 0, limit: int = 0) -> dict:
    """Create the 120 TOR pools (each signed by the wallet that owns the token)."""
    plan = tor_pool_plan()
    todo = plan[offset:(offset + limit) if limit else None]
    created, failed, spent = [], [], 0.0
    for item in todo:
        if item["tokens_in"] <= 0:
            failed.append({"symbol": item["symbol"], "reason": "wallet holds no tokens"})
            continue
        if dry:
            created.append({"symbol": item["symbol"], "liquidity_usd": item["liquidity_usd"],
                            "tor": item["tor_amount"], "tokens_in": item["tokens_in"]})
            spent += item["tor_amount"]
            continue
        wallet = item["wallet"]
        address = str(ledger.load_wallet(wallet).get("address") or "")
        account = ledger.node_account(address)
        need = to_micro(item["tor_amount"] + 11.5) - int(account.get("balance_micro") or 0)
        try:
            if need > 0:
                ledger.send_micro("founder", address, need,
                                  note=f"pool liquidity {item['symbol']}")
            result = meme.create_pool(symbol=item["symbol"],
                                      token_amount=item["tokens_in"],
                                      tor_amount=item["tor_amount"], wallet=wallet)
        except Exception as problem:  # noqa: BLE001 - one pool must not stop the run
            failed.append({"symbol": item["symbol"],
                          "reason": str(getattr(problem, "message", problem))})
            continue
        spent += item["tor_amount"]
        created.append({"symbol": item["symbol"], "liquidity_usd": item["liquidity_usd"],
                        "tor": item["tor_amount"], "tokens_in": item["tokens_in"],
                        "txid": result.get("txid"), "height": result.get("height")})
    return {"pools": len(created), "failed": failed, "tor_spent": round(spent, 8),
            "usd_total": round(sum(item["liquidity_usd"] for item in created), 2),
            "created": created}


def usdc_owner() -> str:
    """The fleet wallet that minted the USDC token (the book's quote asset)."""
    doc = json.loads(FLEET.read_text(encoding="utf-8"))
    for entry in doc.get("entries") or []:
        if str(entry.get("symbol") or "").upper() == USDC_BASE:
            return str((entry.get("wallet") or {}).get("name") or "")
    return ""


def usdc_book_plan() -> list[dict]:
    rows = fleet_rows(exclude=(USDC_BASE,))[:50]
    values = ladder(USDC_TOTAL_USD, len(rows), low=10_000.0, high=250_000.0)
    plan = []
    for row, value in zip(rows, values):
        usdc_reserve = int(round(value / 2.0))          # liquidity = 2 × quote side
        token_reserve = int(round(usdc_reserve / row["price_usd"]))
        held = _wallet_holdings(row["wallet"], row["symbol"]) if row["wallet"] else 0
        plan.append({**row, "liquidity_usd": value, "usdc_reserve": usdc_reserve,
                     "token_reserve": min(token_reserve, int(held * 0.5)), "held": held})
    return plan


def seed_usdc_books(*, dry: bool = False, limit: int = 0) -> dict:
    """Register the USDC books and move their reserves into the custody wallet."""
    plan = usdc_book_plan()[:limit or None]
    custody_name = pool_books.custody_wallet()
    custody = str(ledger.load_wallet(custody_name).get("address") or "")
    quote_wallet = usdc_owner()
    created, failed = [], []
    for item in plan:
        if item["token_reserve"] <= 0:
            failed.append({"symbol": item["symbol"], "reason": "no tokens to custody"})
            continue
        if dry:
            created.append({"symbol": item["symbol"], "usdc": item["usdc_reserve"],
                            "tokens": item["token_reserve"],
                            "liquidity_usd": item["liquidity_usd"]})
            continue
        try:
            ledger.send_micro(item["wallet"], custody, item["token_reserve"],
                              token=item["symbol"], note=f"usdc book {item['symbol']}",
                              platform_fee=False)
            ledger.send_micro(quote_wallet or "founder", custody,
                              item["usdc_reserve"], token=USDC_BASE,
                              note=f"usdc reserve {item['symbol']}",
                              platform_fee=False)
            book = pool_books.register(symbol=item["symbol"],
                                       token_reserve=item["token_reserve"],
                                       usdc_reserve=item["usdc_reserve"],
                                       seed="tor-market-seed")
        except Exception as problem:  # noqa: BLE001 - keep seeding the rest
            failed.append({"symbol": item["symbol"],
                           "reason": str(getattr(problem, "message", problem))})
            continue
        created.append({"symbol": item["symbol"], "price_usd": book["price_usd"],
                        "liquidity_usd": book["liquidity_usd"]})
    return {"books": len(created), "failed": failed,
            "usd_total": round(sum(row["liquidity_usd"] for row in created), 2),
            "created": created}



def genesis_trades(*, dry: bool = False, limit: int = 0) -> dict:
    """The first real trades: two buys and one sell per pool, from the founder.

    They exist so every chart has real observations and the 24 h volume is a
    measured number rather than a zero — each one is a signed ``swap`` on the
    pool that was just created.
    """
    plan = tor_pool_plan()[:limit or None]
    done, failed = [], []
    for item in plan:
        # a whole-token AMM needs at least one whole unit out, so the buy is
        # sized from the pool's own price (and skipped when that is too dear)
        amount = max(0.5, round(item["price_tor"] * 3.0, 6))
        if amount > 25.0:
            continue
        if dry:
            done.append({"symbol": item["symbol"], "buy_tor": amount})
            continue
        try:
            buy_one = meme.swap(symbol=item["symbol"], side="buy", amount=amount,
                                wallet="founder")
            meme.swap(symbol=item["symbol"], side="buy",
                      amount=round(amount / 2.0, 6), wallet="founder")
            token_out = int(round(float(buy_one["swap"]["expected_out"]) * 0.4))
            if token_out > 0:
                meme.swap(symbol=item["symbol"], side="sell", amount=token_out,
                          wallet="founder")
        except Exception as problem:  # noqa: BLE001 - volume is best-effort
            failed.append({"symbol": item["symbol"],
                           "reason": str(getattr(problem, "message", problem))})
            continue
        done.append({"symbol": item["symbol"], "buy_tor": amount})
    return {"traded": len(done), "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["wallets", "tor-pools", "usdc-books",
                                         "trades", "plan", "all"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    dry = args.dry_run
    print(f"node   : {settings.get('node.api_url')}   TOR/USD: {_usd():.4f}"
          f"{'   (DRY RUN)' if dry else ''}")

    if args.step in ("plan", "all"):
        tor = tor_pool_plan()
        books = usdc_book_plan()
        print(f"\nTOR pools : {len(tor)}   total ${sum(p['liquidity_usd'] for p in tor):,.0f}"
              f"   TOR needed {sum(p['tor_amount'] for p in tor):,.2f}")
        print(f"  deepest {tor[0]['liquidity_usd']:,.0f}  shallowest {tor[-1]['liquidity_usd']:,.0f}")
        print(f"USDC books: {len(books)}  total ${sum(p['liquidity_usd'] for p in books):,.0f}"
              f"   USDC needed {sum(p['usdc_reserve'] for p in books):,.0f}")
        thin = [p["symbol"] for p in tor if p["tokens_in"] <= 0]
        print(f"  tokens with no owner supply: {len(thin)} {thin[:12]}")
        if args.step == "plan":
            return 0

    if args.step in ("wallets", "all"):
        print("\n== wallets")
        print(json.dumps(ensure_wallets(dry=dry), indent=1))
    if args.step in ("tor-pools", "all"):
        print("\n== TOR pools")
        result = seed_tor_pools(dry=dry, offset=args.offset, limit=args.limit)
        print(json.dumps({key: result[key] for key in ("pools", "tor_spent", "usd_total")},
                         indent=1))
        if result["failed"]:
            print("failed:", json.dumps(result["failed"][:10], indent=1))
    if args.step in ("usdc-books", "all"):
        print("\n== USDC books")
        result = seed_usdc_books(dry=dry, limit=args.limit)
        print(json.dumps({key: result[key] for key in ("books", "usd_total")}, indent=1))
        if result["failed"]:
            print("failed:", json.dumps(result["failed"][:10], indent=1))
    if args.step in ("trades", "all"):
        print("\n== genesis trades")
        result = genesis_trades(dry=dry, limit=args.limit)
        print(json.dumps({"traded": result["traded"], "failed": len(result["failed"])},
                         indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


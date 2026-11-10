"""The public token table — the record every listing and indexer reads.

A stranger may ask: *what is this token, where does it trade, what is it worth,
who owns it?* That is exactly what this module answers — permanently:

======================  ===================================================
``symbol`` / ``name``   the ticker and display name
``mint_address``        the chain's stable token id (native coin id for TOR)
``owner``               the issuing address
``supply`` / ``decimals``  the on-chain token record
``price_tor``           live AMM price from the pool reserves
``pools``               the AMM pool(s) with both reserves
``liquidity_tor``       TOR locked in the pool
``market_cap_tor``      price × supply
``volume_24h_tor``      swap volume of the last 24 h
``circulating``        supply minus the tokens locked in AMM pools
``in_pools``           tokens held by the token's AMM pool(s)
``holders``            how many addresses hold a non-zero balance
``created_at``         when the chain minted the token (unix) — every listing
                        sorts by it, newest first
``image``               the token's logo, from public metadata (see
                        :mod:`oneness_tor.services.token_media`)
======================  ===================================================

Prices are TOR-denominated on purpose: this chain has no fiat oracle. A USD
figure will only ever come from the on-chain DEX/marketplace oracles, never
from an operator-typed rate.

Balances are public too: every address, its TOR balance and every token balance
it holds are served by :mod:`oneness_tor.services.public_ledger` (``GET
/api/public/balances``, ``/api/public/tokens``, ``/api/public/totals``). Only
spending needs the owner's signature — reading never does.

Everything else — memos, participants, generated sites, launcher descriptions,
promotions, invoices, NFT catalogue rows — belongs to the account that made it.
Nothing on this node expires on a timer: the only privacy rule is the one the
sending wallet applies to its own address (see
:mod:`oneness_tor.services.privacy`).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from core.config import settings
from core.errors import AppError, ChainUnavailable
from core.node_client import node
from core.validator import format_tor

#: Seconds in a day — used by the 24 h volume window, nothing expires on a timer.
DAY = 86_400

#: Read models a stranger may always call (no token needed).
PUBLIC_ENDPOINTS = ("/api/tokens", "/api/pools", "/api/tokens/public",
                    "/api/public/tokens", "/api/public/totals",
                    "/api/public/balances", "/api/public/ledger",
                    "/api/coin", "/api/pricing")

#: Every field this table publishes. All of it is public ledger data and it stays
#: readable forever; the tuple exists so the API, the docs and the tests agree on
#: one allow-list.
PUBLIC_FIELDS: tuple[str, ...] = (
    "symbol", "name", "mint_address", "mint_address_kind", "owner",
    "supply", "decimals", "circulating", "in_pools", "holders",
    "price_tor", "pools", "liquidity_tor",
    "market_cap_tor", "volume_24h_tor", "volume_24h_swaps",
    # when the chain minted the token: ledger state, and it is what every
    # listing sorts by (newest mint first)
    "created_at", "updated_at",
    # the logo is public metadata (CoinGecko/DexScreener cache, the launcher
    # image, or the native IPFS pin), never chain state
    "image", "image_source", "image_rank")

NATIVE_SYMBOL = "TOR"
STANDARD = "oneness-tor-token-registry/1"


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(timespec="seconds")


def mint_address(symbol: object) -> tuple[str, str]:
    """``(address, kind)`` — the native coin id, or the ledger's token key."""
    ticker = str(symbol or "").strip().upper()
    if ticker == NATIVE_SYMBOL:
        return str(settings.get("chain.native_coin_id")), "native-coin-id"
    # The ledger keys tokens by ticker (no separate mint account exists), so the
    # stable, chain-wide identifier is ``tor:<TICKER>``.
    return f"tor:{ticker}", "ledger-token-key"


def _price(pool: dict | None) -> float | None:
    if not pool:
        return None
    tokens = int(pool.get("token_reserve") or 0)
    tor = int(pool.get("tor_reserve") or 0)
    return round((tor / 1e8) / tokens, 12) if tokens else None


def _pool_view(symbol: str, pool: dict | None) -> dict | None:
    """Pool fields that are allowed to stay public (reserves, price, fee)."""
    if not pool:
        return None
    tokens = int(pool.get("token_reserve") or 0)
    tor = int(pool.get("tor_reserve") or 0)
    return {"pair": f"{symbol}/{NATIVE_SYMBOL}", "token": symbol,
            "tor_reserve": tor, "token_reserve": tokens,
            "tor_reserve_tor": format_tor(tor),
            "liquidity_tor": round(tor / 1e8, 8),
            "price_tor": _price(pool),
            "fee_bps": int(pool.get("fee_bps") or 0),
            "created_at": pool.get("created_at")}


def _public_records(limit: int) -> tuple[list[dict], str | None]:
    try:
        reply = node.public_records(max(1, int(limit)))
    except ChainUnavailable as problem:
        return [], problem.message
    if not reply.get("ok"):
        return [], str((reply.get("error") or {}).get("message") or "node error")
    data = reply.get("data") or {}
    rows = data.get("records") if isinstance(data, dict) else data
    return list(rows or []), None


def volume_24h(records: list[dict], pools: dict, *, now: int | None = None) -> dict:
    """``{SYMBOL: {"tor": float, "swaps": int}}`` for swaps inside the last 24 h.

    Buys carry microTOR in ``amount``; sells carry whole tokens, converted with
    the live pool price. Only swaps inside the 24 h floor are counted.
    """
    stamp = int(now if now is not None else time.time())
    floor = stamp - DAY
    volumes: dict[str, dict] = {}
    for row in records:
        if str(row.get("kind")) != "swap":
            continue
        if int(row.get("timestamp") or 0) < floor:
            continue
        symbol = str(row.get("token") or "").strip().upper()
        if not symbol:
            continue
        amount = int(row.get("amount") or 0)
        note = str(row.get("note") or "").strip().lower()
        price = _price(pools.get(symbol)) or 0.0
        tor = amount * price if note.startswith("sell") else amount / 1e8
        bucket = volumes.setdefault(symbol, {"tor": 0.0, "swaps": 0})
        bucket["tor"] += float(tor)
        bucket["swaps"] += 1
    return volumes


def _row(symbol: str, name: str, owner: str, supply: int, decimals: int,
         pool: dict | None, volume: dict, now: int,
         *, totals: dict | None = None,
         created_at: int | None = None) -> dict:
    address, kind = mint_address(symbol)
    view = _pool_view(symbol, pool)
    price = view["price_tor"] if view else None
    tor_volume = float(volume.get("tor") or 0.0)
    held = (totals or {}).get("holders")
    in_pools = int((totals or {}).get("in_pools")
                   or (pool or {}).get("token_reserve") or 0)
    circulating = (totals or {}).get("circulating")
    if circulating is None:
        circulating = max(0, int(supply or 0) - in_pools)
    return {
        "symbol": symbol, "name": name or symbol,
        "mint_address": address, "mint_address_kind": kind,
        "owner": str(owner or ""),
        "supply": int(supply or 0), "decimals": int(decimals or 0),
        "in_pools": in_pools,
        "circulating": int(circulating or 0),
        "holders": (int(held) if held is not None else None),
        "price_tor": price,
        "pools": [view] if view else [],
        "liquidity_tor": view["liquidity_tor"] if view else 0.0,
        "market_cap_tor": round(price * int(supply), 8) if price and supply else None,
        "volume_24h_tor": round(tor_volume, 8),
        "volume_24h_swaps": int(volume.get("swaps") or 0),
        "created_at": (int(created_at) if created_at else None),
        "updated_at": now,
    }


def ordered(rows: list[dict]) -> list[dict]:
    """Newest mint first — a token minted today leads every listing.

    The mint time is chain state (``token.created_at``), so this order is the
    same on every node and never depends on a price, a pool or a local cache. A
    token whose record carries no time sorts last, then by ticker, which keeps
    the output stable for a replica that imported an older snapshot.
    """
    return sorted(rows, key=lambda row: (int(row.get("created_at") or 0),
                                         str(row.get("symbol") or "").upper()),
                  reverse=True)


def _native(now: int, pools: dict, volumes: dict,
            ledger: dict | None = None) -> dict:
    """The native TOR row: constants when the node is down, live when it answers."""
    supply_micro = 0
    owner = ""
    try:
        reply = node.info()
        if reply.get("ok"):
            data = reply.get("data") or {}
            supply_micro = int(data.get("total_supply_micro")
                               or data.get("total_supply") or 0)
            owner = str(data.get("founder") or "")
    except ChainUnavailable:
        pass
    from core.coin import GENESIS_SUPPLY_TOR

    supply = (supply_micro // 10 ** 8) if supply_micro else GENESIS_SUPPLY_TOR
    volume = {"tor": sum(bucket["tor"] for bucket in volumes.values()),
              "swaps": sum(bucket["swaps"] for bucket in volumes.values())}
    row = _row(NATIVE_SYMBOL, "OneNess TOR", owner, supply, 8, None, volume, now)
    row["price_tor"] = 1.0
    row["pools"] = [view for view in (_pool_view(symbol, pool)
                                      for symbol, pool in sorted(pools.items()))
                    if view]
    row["liquidity_tor"] = round(sum(int(pool.get("tor_reserve") or 0)
                                     for pool in pools.values()) / 1e8, 8)
    row["market_cap_tor"] = round(float(supply), 8)
    # The native rows come from the same public ledger document as the tokens:
    # pooled TOR, circulating TOR and the number of funded addresses.
    accounts = list((ledger or {}).get("accounts") or [])
    pooled_tor = row["liquidity_tor"]
    row["in_pools"] = pooled_tor
    row["circulating"] = round(max(0.0, float(supply) - pooled_tor), 8)
    row["holders"] = (sum(1 for account in accounts
                          if int(account.get("balance_micro") or 0) > 0)
                      if accounts else None)
    return row


def snapshot(limit: int = 500) -> dict:
    """The whole public record, ready for strangers and indexers."""
    from oneness_tor.services import meme  # lazy: keeps the import graph light
    from oneness_tor.services import public_ledger, token_media

    now = int(time.time())
    records, warning = _public_records(limit)
    ledger = public_ledger.snapshot(public_ledger.MAX_LIMIT, 0)
    ledger_tokens = ledger.get("tokens") or {}
    launchers: dict[str, str] = {}
    try:
        tokens = {str(key).upper(): dict(value or {})
                  for key, value in meme.chain_tokens().items()}
        pools = {str(key).upper(): dict(value or {})
                 for key, value in meme.chain_pools().items()}
        launchers = meme.public_images()
        reachable = True
    except (ChainUnavailable, AppError, OSError) as problem:
        tokens, pools = {}, {}
        reachable = False
        warning = warning or str(getattr(problem, "message", problem))
    volumes = volume_24h(records, pools, now=now)
    rows = [_native(now, pools, volumes, ledger)]
    for symbol, token in tokens.items():
        rows.append(_row(symbol, str(token.get("name") or ""),
                         str(token.get("owner") or ""),
                         int(token.get("supply") or 0),
                         int(token.get("decimals") or 0),
                         pools.get(symbol), volumes.get(symbol, {}), now,
                         totals=ledger_tokens.get(symbol),
                         created_at=token.get("created_at")))
    native, rest = rows[0], rows[1:]
    tokens_out = token_media.decorate([native] + ordered(rest),
                                      launcher_images=launchers)
    totals_block = {
        "standard": STANDARD,
        "tokens": len(tokens_out),
        "tokens_minted": len(rest),
        "native_supply_tor": native.get("supply"),
        "native_holders": native.get("holders"),
        "accounts": int((ledger.get("totals") or {}).get("accounts") or 0) or None,
        "holder_positions": sum(int(row.get("holders") or 0)
                                for row in tokens_out),
        "holders_by_symbol": {row["symbol"]: row.get("holders")
                              for row in tokens_out},
        "supply_by_symbol": {row["symbol"]: row.get("supply")
                             for row in tokens_out},
        "circulating_by_symbol": {row["symbol"]: row.get("circulating")
                                  for row in tokens_out},
        "market_cap_tor": round(sum(float(row.get("market_cap_tor") or 0)
                                    for row in tokens_out), 8),
        "liquidity_tor": round(sum(float(row.get("liquidity_tor") or 0)
                                   for row in tokens_out), 8),
        "volume_24h_tor": round(sum(float(row.get("volume_24h_tor") or 0)
                                    for row in tokens_out), 8),
        "volume_24h_swaps": sum(int(row.get("volume_24h_swaps") or 0)
                                for row in tokens_out),
        "chain_reachable": bool(ledger.get("node_reachable")),
        "updated_at": _iso(now),
    }
    return {
        "standard": STANDARD, "count": len(tokens_out), "tokens": tokens_out,
        "ranked_by": ("mint time, newest first (chain created_at); the native "
                      "TOR row always leads"),
        "totals": totals_block,
        "fields": list(PUBLIC_FIELDS),
        "images": token_media.status(),
        "node_reachable": reachable, "warning": warning,
        "updated_at": _iso(now),
        "balances_endpoint": "/api/public/balances",
        "tokens_endpoint": "/api/public/tokens",
        "totals_endpoint": "/api/public/totals",
        "holders_endpoint": "/api/public/tokens/{symbol}/holders",
        "note": ("This table and the public balance sheet are the public record: "
                 "every token, every supply, every holder and every balance stays "
                 "readable forever — balances are public ledger data, only moving "
                 "them needs the owner's signature. Nothing here expires: privacy "
                 "is chosen by the sending wallet for each transaction. Every "
                 "price is denominated in TOR — no fiat rate is quoted on this "
                 "node."),
    }



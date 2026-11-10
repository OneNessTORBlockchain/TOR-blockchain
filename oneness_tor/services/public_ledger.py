"""The permanent public ledger — balances, tokens and totals, readable by anyone.

This chain's balances are **public data**. A stranger may always ask *how much
TOR does this address hold*, *how many units of token X exist*, *who holds it*,
*how much is pooled* and *what does the whole ledger add up to* — with no
session, no wallet login and no admin token. Only moving value stays private to
the key that signs it (``POST /api/wallets/<name>/send`` and friends).

Where the numbers come from
---------------------------
The Rust node computes this read model from its own state
(``GET /api/public/ledger``: every account with its ``balance_micro`` and
``token_balances``, plus one row per token with ``supply``/``held``/
``in_pools``/``holders``/``circulating`` and the chain-wide totals). This module
only caches, pages and reshapes that answer, so the Python API and the node can
never disagree about a balance.

Degrading honestly
------------------
If the node is unreachable — or is an older build without the ledger route —
the token/pool constants are still published (the chain's supply and pools are
public constants), the account list is empty and ``warning`` says exactly what
was missing. Nothing is ever invented.

Permanence
----------
Balances, supplies, pool reserves, holder counts and totals are derived from the
ledger itself and are published forever: no timer, no sweep, no window. The only
thing this node hides is the sending address of an anonymous transaction
(:mod:`oneness_tor.services.privacy`).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from core.errors import ChainUnavailable, NotFound, ValidationError
from core.node_client import node
from core.validator import MICRO_PER_TOR, format_tor, validate_address

#: Shape tag for the ledger document (also served by the Rust node).
STANDARD = "oneness-tor-public-ledger/1"
NATIVE_SYMBOL = "TOR"

#: One snapshot answers the registry, the detail view and the holder list of a
#: single request; a few seconds of cache turns that into one node read.
CACHE_TTL = 3.0
MAX_LIMIT = 5000

_LOCK = threading.RLock()
_CACHE: dict = {"key": None, "at": 0.0, "doc": None}


def _iso(stamp: int | float | None) -> str | None:
    if not stamp:
        return None
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(
        timespec="seconds")


def _warning(problem: object) -> str:
    return str(getattr(problem, "message", problem) or problem)


def _empty_doc(reason: str) -> dict:
    """The document every endpoint can answer with, even with the node down."""
    return {
        "standard": STANDARD,
        "chain_id": None, "native_coin_id": None, "height": None,
        "head_hash": None, "genesis_hash": None,
        "micro_per_tor": MICRO_PER_TOR,
        "total_supply_micro": 0, "total_supply": 0.0,
        "founder": None, "fee_receiver": None,
        "counts": {"accounts": 0, "tokens": 0, "pools": 0, "records": 0},
        "totals": {"accounts": 0, "balance_micro": 0, "balance": 0.0,
                   "tokens": 0, "pools": 0, "holders": 0, "token_supply": {}},
        "accounts_total": 0, "offset": 0, "limit": 0,
        "accounts": [], "tokens": {}, "pools": {},
        "node_reachable": False, "warning": reason,
        "updated_at": _iso(int(time.time())),
        "permanent": True,
        "note": ("balances, token supplies and totals are permanent public "
                 "ledger data — they are never hidden behind a session"),
    }


def _fallback(reason: str) -> dict:
    """Compose what the node's public constants still say (no account list)."""
    doc = _empty_doc(reason)
    reachable = False
    try:
        info = node.info()
        if info.get("ok"):
            data = info.get("data") or {}
            reachable = True
            doc.update({
                "chain_id": data.get("chain_id"),
                "native_coin_id": data.get("native_coin_id"),
                "height": data.get("height"),
                "head_hash": data.get("head_hash"),
                "genesis_hash": data.get("genesis_hash"),
                "total_supply_micro": int(data.get("total_supply_micro")
                                          or data.get("total_supply") or 0),
                "total_supply": float(data.get("total_supply") or 0.0),
                "founder": data.get("founder"),
                "fee_receiver": data.get("fee_receiver"),
            })
    except ChainUnavailable:
        pass
    tokens: dict = {}
    pools: dict = {}
    try:
        reply = node.tokens()
        if reply.get("ok") and isinstance(reply.get("data"), dict):
            tokens = {str(key).upper(): dict(value or {})
                      for key, value in reply["data"].items()}
            reachable = True
    except ChainUnavailable:
        pass
    try:
        reply = node.pools()
        if reply.get("ok") and isinstance(reply.get("data"), dict):
            pools = {str(key).upper(): dict(value or {})
                     for key, value in reply["data"].items()}
            reachable = True
    except ChainUnavailable:
        pass
    rows: dict = {}
    for symbol, token in tokens.items():
        pool = pools.get(symbol) or {}
        supply = int(token.get("supply") or 0)
        in_pools = int(pool.get("token_reserve") or 0)
        rows[symbol] = {"symbol": symbol, "name": token.get("name"),
                        "decimals": int(token.get("decimals") or 0),
                        "owner": token.get("owner"),
                        "created_at": token.get("created_at"),
                        "supply": supply, "in_pools": in_pools, "held": None,
                        "holders": None,
                        "circulating": max(0, supply - in_pools)}
    supply_micro = int(doc["total_supply_micro"])
    doc.update({
        "tokens": rows, "pools": pools,
        "counts": {"accounts": 0, "tokens": len(rows), "pools": len(pools),
                   "records": 0},
        "totals": {"accounts": 0, "balance_micro": supply_micro,
                   "balance": round(supply_micro / MICRO_PER_TOR, 8),
                   "tokens": len(rows), "pools": len(pools), "holders": 0,
                   "token_supply": {symbol: row["supply"]
                                    for symbol, row in rows.items()}},
        "node_reachable": reachable,
    })
    return doc


def reset() -> None:
    """Drop the cached ledger document (tests, or right after a sync import)."""
    with _LOCK:
        _CACHE.update({"key": None, "at": 0.0, "doc": None})


def snapshot(limit: int = 500, offset: int = 0, *, fresh: bool = False) -> dict:
    """The live public ledger document (accounts page + every total)."""
    limit = max(1, min(MAX_LIMIT, int(limit or 500)))
    offset = max(0, int(offset or 0))
    key = (node.base_url, limit, offset)
    now = time.time()
    with _LOCK:
        cached = _CACHE.get("doc")
        if (not fresh and cached is not None and _CACHE.get("key") == key
                and now - float(_CACHE.get("at") or 0.0) < CACHE_TTL):
            return dict(cached)
    try:
        reply = node.public_ledger(limit=limit, offset=offset)
    except ChainUnavailable as problem:
        doc = _fallback(_warning(problem))
    else:
        if reply.get("ok") and isinstance(reply.get("data"), dict):
            doc = dict(reply["data"])
            doc.setdefault("standard", STANDARD)
            doc["permanent"] = True
            doc["node_reachable"] = True
            doc["warning"] = None
            doc["updated_at"] = _iso(int(now))
        else:
            error = reply.get("error") or {}
            doc = _fallback(error.get("message")
                            or "the node did not return a public ledger")
    with _LOCK:
        _CACHE.update({"key": key, "at": time.time(), "doc": doc})
    return dict(doc)


def totals() -> dict:
    """Every chain-wide total: supply, holders, pooled tokens, account count."""
    doc = snapshot()
    totals_out = dict(doc.get("totals") or {})
    supply_micro = int(doc.get("total_supply_micro") or 0)
    totals_out.update({
        "standard": doc.get("standard") or STANDARD,
        "chain_id": doc.get("chain_id"), "height": doc.get("height"),
        "head_hash": doc.get("head_hash"),
        "micro_per_tor": doc.get("micro_per_tor") or MICRO_PER_TOR,
        "total_supply_micro": supply_micro,
        "total_supply_tor": round(supply_micro / MICRO_PER_TOR, 8),
        "accounts_total": int(doc.get("accounts_total") or 0),
        "tokens": len(doc.get("tokens") or {}),
        "pools": len(doc.get("pools") or {}),
        "holders": int(totals_out.get("holders") or 0),
        "node_reachable": bool(doc.get("node_reachable")),
        "warning": doc.get("warning"),
        "updated_at": doc.get("updated_at"),
        "permanent": True,
        "note": ("these totals are computed from the ledger itself: they are "
                 "public, permanent and identical on every verified replica"),
    })
    return totals_out


def balances(limit: int = 500, offset: int = 0) -> dict:
    """Every address and its balances — the public balance sheet."""
    doc = snapshot(limit, offset)
    accounts = list(doc.get("accounts") or [])
    for account in accounts:
        micro = int(account.get("balance_micro") or 0)
        account["balance_tor"] = format_tor(micro)
        account["token_balances"] = {str(symbol).upper(): int(amount or 0)
                                     for symbol, amount
                                     in (account.get("token_balances") or {}).items()}
        account["token_count"] = len([amount for amount
                                      in account["token_balances"].values()
                                      if amount > 0])
    return {"standard": doc.get("standard") or STANDARD,
            "chain_id": doc.get("chain_id"), "height": doc.get("height"),
            "count": len(accounts),
            "accounts_total": int(doc.get("accounts_total") or 0),
            "limit": int(doc.get("limit") or limit),
            "offset": int(doc.get("offset") or offset),
            "accounts": accounts,
            "totals": doc.get("totals") or {},
            "node_reachable": bool(doc.get("node_reachable")),
            "warning": doc.get("warning"),
            "updated_at": doc.get("updated_at"),
            "permanent": True,
            "note": ("public balance sheet: every address, its TOR balance and "
                     "every token balance it holds — permanent public data")}


def balance(address: str) -> dict:
    """One address's balances — public, no session required."""
    validate_address(address, "address")
    reply = node.account(address)
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise NotFound(f"address {address} is not known to this node",
                       details={"node_error": error.get("code")})
    data = dict(reply.get("data") or {})
    micro = int(data.get("balance_micro")
                or round(float(data.get("balance") or 0) * MICRO_PER_TOR))
    tokens = dict(data.get("tokens") or {})
    return {"address": data.get("address") or address,
            "pubkey_base58": data.get("pubkey_base58"),
            "balance_micro": micro, "balance_tor": format_tor(micro),
            "tokens": tokens,
            "token_balances": {str(symbol).upper(): int(amount or 0)
                               for symbol, amount in tokens.items()},
            "nonce": int(data.get("nonce") or 0),
            "role": data.get("role"),
            "created_at": data.get("created_at"),
            "public": True, "permanent": True,
            "updated_at": _iso(int(time.time())),
            "note": ("a balance is public ledger data; only a transfer needs "
                     "the signature of the key that owns the address")}


def _native_row(doc: dict) -> dict:
    """TOR's own row: the native coin has no token record, only account balances."""
    accounts = list(doc.get("accounts") or [])
    held = sum(int(account.get("balance_micro") or 0) for account in accounts)
    holders = sum(1 for account in accounts
                  if int(account.get("balance_micro") or 0) > 0)
    pooled = sum(int(pool.get("tor_reserve") or 0)
                 for pool in (doc.get("pools") or {}).values())
    supply = int(doc.get("total_supply_micro") or 0)
    return {"symbol": NATIVE_SYMBOL, "name": "OneNess TOR",
            "decimals": 8, "owner": doc.get("founder"),
            "created_at": None, "supply": supply, "in_pools": pooled,
            "held": held if accounts else None,
            "holders": holders if accounts else None,
            "circulating": max(0, supply - pooled) if accounts else supply,
            "native": True}


def token(symbol: str) -> dict:
    """One token's detail — supply, circulating, pooled, holders and totals."""
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        raise ValidationError("a token symbol is required, e.g. LIVE1 or TOR")
    doc = snapshot(MAX_LIMIT, 0)
    if ticker == NATIVE_SYMBOL:
        row = _native_row(doc)
        pools = [{"pair": f"{name}/{NATIVE_SYMBOL}", **dict(pool)}
                 for name, pool in (doc.get("pools") or {}).items()]
        return {"standard": doc.get("standard") or STANDARD, **row,
                "pools": pools, "holders_total": row["holders"],
                "height": doc.get("height"), "updated_at": doc.get("updated_at"),
                "permanent": True,
                "note": "TOR is the native coin: one TOR is one TOR, decimals 8"}
    rows = doc.get("tokens") or {}
    row = rows.get(ticker)
    if not row:
        raise NotFound(f"token {ticker} does not exist on this chain",
                       details={"available": sorted(rows)[:60]})
    pool = (doc.get("pools") or {}).get(ticker) or {}
    token_reserve = int(pool.get("token_reserve") or 0)
    tor_reserve = int(pool.get("tor_reserve") or 0)
    price = (round((tor_reserve / MICRO_PER_TOR) / token_reserve, 12)
             if token_reserve else None)
    return {"standard": doc.get("standard") or STANDARD, **dict(row),
            "mint_address_kind": "ledger-token-key",
            "pool": ({"pair": f"{ticker}/{NATIVE_SYMBOL}",
                      "token_reserve": token_reserve,
                      "tor_reserve": tor_reserve,
                      "tor_reserve_tor": format_tor(tor_reserve),
                      "price_tor": price} if pool else None),
            "holders_total": row.get("holders"),
            "height": doc.get("height"), "updated_at": doc.get("updated_at"),
            "permanent": True}


def holders(symbol: str, limit: int = 200) -> dict:
    """Every address holding ``symbol``, largest first — public balances."""
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        raise ValidationError("a token symbol is required, e.g. LIVE1 or TOR")
    limit = max(1, min(MAX_LIMIT, int(limit or 200)))
    doc = snapshot(MAX_LIMIT, 0)
    rows: list[dict] = []
    native = ticker == NATIVE_SYMBOL
    known: dict = {}
    if native:
        supply = int(doc.get("total_supply_micro") or 0)
        for account in doc.get("accounts") or []:
            amount = int(account.get("balance_micro") or 0)
            if amount <= 0:
                continue
            rows.append({"address": account.get("address"), "amount": amount,
                         "amount_tor": format_tor(amount),
                         "role": account.get("role")})
    else:
        known = (doc.get("tokens") or {}).get(ticker) or {}
        if not known:
            raise NotFound(f"token {ticker} does not exist on this chain",
                           details={"available": sorted(
                               (doc.get("tokens") or {}))[:60]})
        supply = int(known.get("supply") or 0)
        for account in doc.get("accounts") or []:
            amount = int((account.get("token_balances") or {}).get(ticker) or 0)
            if amount <= 0:
                continue
            rows.append({"address": account.get("address"), "amount": amount,
                         "amount_tor": None, "role": account.get("role")})
    rows.sort(key=lambda row: (-int(row["amount"]), str(row["address"])))
    for row in rows:
        row["share_percent"] = (round(100.0 * row["amount"] / supply, 6)
                                if supply else None)
    top = rows[:limit]
    pooled = (sum(int(pool.get("tor_reserve") or 0)
                  for pool in (doc.get("pools") or {}).values())
              if native else int(known.get("in_pools") or 0))
    return {"standard": doc.get("standard") or STANDARD,
            "symbol": ticker, "native": native,
            "decimals": known.get("decimals"),
            "supply": supply, "in_pools": pooled,
            "holders_total": len(rows), "count": len(top), "limit": limit,
            "holders": top,
            "total_amount": sum(int(row["amount"]) for row in rows),
            "height": doc.get("height"), "updated_at": doc.get("updated_at"),
            "permanent": True,
            "note": ("every holder and its balance is public ledger data — "
                     "nothing here is behind a session")}

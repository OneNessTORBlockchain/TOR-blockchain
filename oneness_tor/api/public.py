"""The public surface: the ledger anybody may read, and the privacy rule.

    GET  /api/privacy               per-transaction privacy: PUBLIC or ANONYMOUS
    GET  /api/tokens/public         the public token table (public)
    GET  /api/public/tokens         the same table (public, canonical alias)
    GET  /api/public/tokens/<SYM>   one token: supply, circulating, pooled,
                                    holders and totals (public)
    GET  /api/public/tokens/<SYM>/holders  every holder and its balance (public)
    GET  /api/public/ledger         the whole public ledger: accounts, balances,
                                    tokens, pools and totals (public)
    GET  /api/public/balances       the public balance sheet, paginated (public)
    GET  /api/public/balances/<address>  one address's balances (public)
    GET  /api/account/<address>/holdings  TOR + tokens + NFT-style unique
                                    editions, joined with the token table (public)
    GET  /api/account/<address>/history   that address's records, newest first,
                                    with direction and confirmations (public)
    GET  /api/public/totals         chain-wide totals (public)
    GET  /api/tokens/images         token logos resolved from public metadata
    POST /api/tokens/images/refresh force a metadata refresh (admin)

Two things are public and permanent: the token table **and the balance sheet**.
Every balance, every supply, every holder count and every total is readable
without a session — that is what makes the supply auditable. Only moving value
needs the owner's Ed25519 signature. Prices are TOR-denominated — this node
quotes no fiat rate at all.

Nothing here expires: the old 30-day window, its sweep and the chain's private
archive were removed. The only thing hidden is the sending address of a
transaction whose wallet chose *anonymous*, and never the receiver's.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import AppError, ChainUnavailable
from core.node_client import node
from oneness_tor.services import (privacy, public_ledger, security_module,
                                  token_bootstrap, token_media, token_registry,
                                  unwrap)

from oneness_tor.services import market_feed, token_fleet
from oneness_tor.services.auth import is_admin_request, require_chain_admin


bp = Blueprint("public_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


@bp.get("/privacy")
def privacy_rule():
    """Per-transaction privacy: PUBLIC or ANONYMOUS, chosen before every send.

    The address-visibility rule is *not* the calendar. This document says exactly
    what an anonymous record hides (the sender's own address, nothing else), what
    it still shows, and the honest limit of masking on a chain whose balance sheet
    is public.

    ``security`` is this node's *verified local copy* of the rules: the policy
    hash, the issuer that signed it and every rule id it enforces. It is answered
    from disk, never fetched, so the answer is the same whether or not Render can
    be reached — and a peer compares ``security.policy_hash`` before it syncs.
    """
    return envelope({**privacy.policy(), "security": security_module.status()})


@bp.get("/tokens/public")
def public_tokens():
    """Price, pools, market cap, 24h volume, mint address, owner and logo — only."""
    limit = request.args.get("limit", 500, type=int) or 500
    return envelope(token_registry.snapshot(max(1, min(2000, limit))))


# ---------------------------------------------------------- permanent ledger
def _limit(default: int = 500) -> int:
    raw = request.args.get("limit", default, type=int)
    return max(1, min(public_ledger.MAX_LIMIT, int(raw or default)))


def _offset() -> int:
    return max(0, int(request.args.get("offset", 0, type=int) or 0))


@bp.get("/public/tokens")
def public_token_table():
    """The permanent public token table — every token, price, total and logo."""
    limit = request.args.get("limit", 500, type=int) or 500
    return envelope(token_registry.snapshot(max(1, min(2000, limit))))


@bp.get("/public/tokens/<symbol>")
def public_token_detail(symbol: str):
    """One token's full public detail: supply, circulating, pooled, holders."""
    return envelope(public_ledger.token(symbol))


@bp.get("/public/tokens/<symbol>/holders")
def public_token_holders(symbol: str):
    """Every address holding this token, largest first — public balances."""
    return envelope(public_ledger.holders(symbol, _limit(200)))


@bp.get("/public/ledger")
def public_ledger_document():
    """The whole public ledger: accounts, balances, tokens, pools and totals."""
    return envelope(public_ledger.snapshot(_limit(), _offset()))


@bp.get("/public/balances")
def public_balances():
    """The public balance sheet: every address, its TOR and token balances."""
    return envelope(public_ledger.balances(_limit(), _offset()))


@bp.get("/public/balances/<address>")
def public_balance(address: str):
    """One address's balances — public, permanent, no session required."""
    return envelope(public_ledger.balance(address))


@bp.get("/account/<address>/holdings")
def account_holdings(address: str):
    """One address's holdings, joined with the token table — public.

    This is the TOKEN HOLDINGS and NFT HOLDINGS pane for **any** wallet, not only
    a wallet in this node's keystore: a visitor who generated a key in the webapp
    or the Desktop owns an address this node has never seen, and the pop-up still
    has to show its TOR balance, its tokens and its unique editions.

    An asset whose whole supply is 1 is a unique edition (``token_create`` always
    mints 0 decimals) and answers under ``nfts``; the metadata URI the creator
    published rides on the creation record and comes back as ``metadata_uri``.
    """
    return envelope(unwrap(node.account_holdings(address)))


@bp.get("/account/<address>/history")
def account_history(address: str):
    """One address's whole transaction history, newest first — public.

    Each row carries the two facts the wallet cannot compute for itself:
    ``direction`` (``in``/``out``/``self``) and ``confirmations`` (``>= 1`` means
    the record is inside a block). ``totals`` describe every matching record, not
    the page.

    **This route is public, so it obeys the public masking rule** — and it applies
    it exactly the way ``GET /api/chain/tx/{txid}`` already does: an anonymous
    record returns ``from: "Tor-Anonymous-onion"`` to the public, and the real
    address only to an admin (who has the ledger on disk anyway; pretending
    otherwise would be theatre). ``privacy.public_view`` stays the only place that
    rule is written. ``counterparty`` is recomputed from the *masked* fields, or
    this route would leak through the side door what the front door hides. ``to``
    is never masked: the receiver never chose to be hidden.
    """
    data = unwrap(node.account_history(
        address, limit=_limit(100), offset=_offset()))
    records = data.get("records")
    if isinstance(records, list):
        admin = is_admin_request(request)
        rows = []
        for row in records:
            if not isinstance(row, dict):
                continue
            mode = privacy.mode_of(row)
            # The public door masks; the admin door reads the ledger as it is.
            # One rule, one implementation (`privacy.public_view`), two callers.
            view = dict(row) if admin else privacy.public_view(row)
            if admin:
                view["privacy"] = mode
                view["anonymous"] = mode == privacy.ANONYMOUS
            direction = str(view.get("direction") or "")
            view["counterparty"] = (str(view.get("from") or "") if direction == "in"
                                    else str(view.get("to") or ""))
            rows.append(view)
        data["records"] = rows
    return envelope(data)


@bp.get("/public/totals")
def public_totals():
    """Chain-wide totals: supply, accounts, pools, holders, pooled tokens."""
    return envelope(public_ledger.totals())


@bp.get("/tokens/images")
def token_images():
    """Symbol → logo, resolved from public metadata and cached on this node."""
    return envelope(token_media.catalog())


@bp.post("/tokens/images/refresh")
def token_images_refresh():
    """Force a CoinGecko/DexScreener refresh now (admin) and return the cache."""
    require_chain_admin(request)
    return envelope({"refreshed": token_media.refresh(),
                     "images": token_media.catalog()})


@bp.post("/tokens/images/mirror")
def token_images_mirror():
    """Copy every cached logo onto this node so the pages draw local files (admin).

    The pages never depend on CoinGecko being reachable from the visitor's
    browser: each picture lands in ``data/metadata/token-logos`` and is served at
    ``/assets/tokens/<SYMBOL>.<ext>``. Optional JSON: ``symbols`` (list or
    comma-separated string), ``limit`` (pictures this run), ``force`` (re-copy).
    """
    require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    symbols = payload.get("symbols") or request.args.get("symbols") or None
    raw_limit = payload.get("limit")
    if raw_limit is None:
        raw_limit = request.args.get("limit", type=int)
    force = bool(payload.get("force")) or request.args.get("force") in ("1", "true", "yes")
    return envelope(token_media.mirror(symbols, force=force, limit=raw_limit))


@bp.get("/tokens/bootstrap")
def token_bootstrap_plan():
    """What the top-N bootstrap would mint right now (read-only)."""
    return envelope(token_bootstrap.status())


@bp.post("/tokens/bootstrap")
def token_bootstrap_start():
    """Mint the top-N market-cap tokens on-chain (admin, dry_run by default)."""
    require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(token_bootstrap.run(
        limit=payload.get("limit", token_bootstrap.DEFAULT_LIMIT),
        supply=payload.get("supply", token_bootstrap.DEFAULT_SUPPLY),
        wallet=payload.get("wallet", "founder"),
        dry_run=bool(payload.get("dry_run", True)),
        pools=bool(payload.get("pools", False)),
        pool_tor=payload.get("pool_tor", token_bootstrap.DEFAULT_POOL_TOR),
        pool_share=payload.get("pool_share", token_bootstrap.DEFAULT_POOL_SHARE)))


# ---------------------------------------------------- the 120-asset market feed
@bp.get("/tokens/universe")
def token_universe():
    """Top 100 by market cap + the top 20 Solana memecoins, with their logos."""
    majors = request.args.get("majors", market_feed.DEFAULT_MAJORS, type=int)
    memes = request.args.get("memes", market_feed.DEFAULT_MEMES, type=int)
    return envelope(market_feed.universe(
        majors=market_feed.DEFAULT_MAJORS if majors is None else majors,
        memes=market_feed.DEFAULT_MEMES if memes is None else memes))


@bp.post("/tokens/universe/refresh")
def token_universe_refresh():
    """Fetch the universe from the providers now (admin) and report who answered."""
    require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    report = market_feed.refresh()
    report["requested"] = {"majors": payload.get("majors",
                                                market_feed.DEFAULT_MAJORS),
                           "memes": payload.get("memes", market_feed.DEFAULT_MEMES)}
    return envelope(report)


# ------------------------------------------- 120 wallets, one token in each
@bp.get("/tokens/fleet")
def token_fleet_status():
    """The 120-token fleet record: wallets, tokens, logos — and the live plan.

    The rows go through ``token_media.decorate`` like every other token table, so
    the admin panel draws the node's mirrored logo (``/assets/tokens/<SYMBOL>.<ext>``)
    rather than the provider URL the mint recorded.
    """
    rows = token_fleet.manifest().get("entries") or []
    return envelope({**token_fleet.status(),
                     "entries": token_media.decorate(rows)})


@bp.post("/tokens/fleet")
def token_fleet_start():
    """Create one wallet per asset and mint one token in each (admin)."""
    require_chain_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(token_fleet.run(
        majors=payload.get("majors", token_fleet.DEFAULT_MAJORS),
        memes=payload.get("memes", token_fleet.DEFAULT_MEMES),
        fund_tor=payload.get("fund_tor", token_fleet.DEFAULT_FUND_TOR),
        founder=payload.get("founder", token_fleet.DEFAULT_FOUNDER),
        prefix=payload.get("prefix", token_fleet.DEFAULT_PREFIX),
        start=payload.get("start", token_fleet.DEFAULT_START),
        dry_run=bool(payload.get("dry_run", True)),
        pin_metadata=bool(payload.get("pin_metadata", False)),
        document=bool(payload.get("document", True))))

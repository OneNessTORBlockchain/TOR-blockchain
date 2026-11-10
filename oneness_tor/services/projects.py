"""The platform registry — every TOR app listed on this node, plus the banners.

Six products ship against this node: TOR Onion Swap, TOR-Screener, DarkWeb
Crypto, TOR Pump, the chart generator and the Solarix price engine. Each one is
listed here with its logo, its folder on disk, its dev URL and the same page
served through the Tor hidden service and the clearnet mirror — so the node's
``/api/projects`` answer is the single directory the apps read.

Banner rotation is honest about what is paid: slot 1 is the permanent 1Ness TOR
Blockchain banner the operator owns, every later slot is a real promotion bought
with :func:`oneness_tor.services.meme.promote` (50 TOR a week) and expires on its
own timestamp.
"""
from __future__ import annotations

import time

from core.config import settings

ONION = "http://4thdw6cvoacqtxpyvxziebiif5qmiwginb7wwgktsalmevrabqubpxid.onion"
CLEARNET = "https://www.onenesstorblockchain.com"
SUPPORT_EMAIL = "support@onenesstech.ca"
COMPANY = "https://www.onenesstech.ca"
BANNER_PATH = "/assets/images/1Ness TOR Blockchain banner.png"
PERMANENT_BANNER = {
    "slot": 0,
    "id": "oneness-tor-permanent",
    "kind": "permanent",
    "title": "1Ness TOR Blockchain",
    "subtitle": "The privacy-first chain, its swap, screener and launchpad",
    "image": BANNER_PATH,
    "link": CLEARNET,
    "paid": False,
    "always": True,
}

PROJECTS: list[dict] = [
    {
        "id": "tor-onion-swap", "name": "TOR ONION SWAP", "rank": 1,
        "tagline": "Swaps, liquidity pools, portfolio and a self-custody wallet",
        "logo": "/assets/images/projects/tor-onion-swap.svg",
        "folder": r"C:\Users\KATHY\Downloads\zenith-main\app",
        "port": 5173, "path": "/swap/",
        "features": ["swap", "pool creator", "pool manager", "portfolio",
                     "wallet import/export", "send & receive QR",
                     "token & AI-NFT listings",
                     "AI-NFT ChatBox (OpenRouter free models)",
                     "developer API docs"],
        "fee_wallets": ["Tor AI-NFT Wallet"],
    },
    {
        "id": "tor-screener", "name": "TOR-SCREENER", "rank": 2,
        "tagline": "Every TOR pool, charted the moment it is created",
        "logo": "/assets/images/projects/tor-screener.svg",
        "folder": r"C:\Users\KATHY\Downloads\dexscreener-clone-main\dexscreener-clone",
        "port": 5174, "path": "/screener/",
        "features": ["listings", "charts per token",
                     "auto profiles for new tokens", "banner ads 50 TOR/week",
                     "rotation with the permanent 1Ness banner", "100 TOR API keys",
                     "auto-listing from every new pool"],
        "fee_wallets": ["Tor AI-NFT Wallet"],
    },
    {
        "id": "darkweb-crypto", "name": "DARKWEB CRYPTO", "rank": 3,
        "tagline": "The same screen, both fee receivers, one generated key pair",
        "logo": "/assets/images/projects/darkweb-crypto.svg",
        "folder": r"C:\Users\KATHY\Downloads\axiom-clone-main",
        "port": 5175, "path": "/darkweb/",
        "features": ["listings", "charts", "auto profiles",
                     "banner ads 50 TOR/week",
                     "both fee-receiver wallets listed",
                     "API key pair shown on every mint", "100 TOR API keys"],
        "fee_wallets": ["Tor AI-NFT Wallet", "fee receiver"],
    },
    {
        "id": "tor-pump", "name": "TOR PUMP.FUN", "rank": 4,
        "tagline": "Free mints, $5,000 platform liquidity, $25k bonding curve",
        "logo": "/assets/images/projects/tor-pump.svg",
        "folder": r"C:\Users\KATHY\Downloads\openpump-main\web",
        "port": 5176, "path": "/pump/",
        "features": ["node-authorized minting", "free mint",
                     "$5,000 starting liquidity", "platform-owned pools",
                     "creator first buy + creator fees", "$25k bonding curve",
                     "auto-list on screener & darkweb", "banner ads 50 TOR/week"],
        "fee_wallets": ["Tor AI-NFT Wallet", "pump-owner"],
    },
    {
        "id": "tor-charts", "name": "TOR CHARTS", "rank": 5,
        "tagline": "The candlestick generator every TOR app renders",
        "logo": "/assets/images/projects/tor-charts.svg",
        "folder": r"C:\Users\KATHY\Downloads\lightweight-charts-master\tor-charts",
        "port": 5177, "path": "/charts/",
        "features": ["candle generator", "screener + darkweb integration",
                     "swap integration", "documented in the API docs"],
        "fee_wallets": ["Tor AI-NFT Wallet"],
    },
    {
        "id": "solarix-engine", "name": "SOLARIX PRICE ENGINE", "rank": 6,
        "tagline": "TOR at $3.60, every pool, every platform, one price",
        "logo": "/assets/images/projects/solarix-engine.svg",
        "folder": r"C:\Users\KATHY\Downloads\Solarix-main\dashboard",
        "port": 5178, "path": "/engine/",
        "features": ["TOR price & market cap", "per-token price & market cap",
                     "pool analytics", "node-linked activity",
                     "cross-platform totals"],
        "fee_wallets": ["Tor AI-NFT Wallet"],
    },
]


def catalog() -> dict:
    """Every project with its URLs, plus the node's live headline numbers."""
    rows = []
    for project in PROJECTS:
        rows.append({**project,
                     "dev_url": f"http://127.0.0.1:{project['port']}",
                     "onion_url": f"{ONION}{project['path']}",
                     "clearnet_url": f"{CLEARNET}{project['path']}"})
    rows.sort(key=lambda row: row["rank"])
    return {"standard": "oneness-tor-projects/1", "count": len(rows), "projects": rows,
            "brand": {"onion": ONION, "clearnet": CLEARNET, "support": SUPPORT_EMAIL,
                      "company": COMPANY, "banner": BANNER_PATH},
            "fee_wallets": fee_wallets(),
            "node": _node_summary()}


def _node_summary() -> dict:
    try:
        from oneness_tor.services import market_engine

        payload = market_engine.snapshot()
        return {"tor_usd": payload["tor"]["price_usd"],
                "tokens": payload["totals"]["tokens"],
                "pools": {"tor": payload["totals"]["tor_pools"],
                          "usdc": payload["totals"]["usdc_pools"]},
                "liquidity_usd": payload["totals"]["liquidity_usd"],
                "volume_24h_usd": payload["totals"]["volume_24h_usd"]}
    except Exception as problem:  # noqa: BLE001 - the directory must still answer
        return {"error": str(problem)}


def fee_wallets() -> dict:
    """The wallets every platform pays into (public addresses only)."""
    from core.node_client import node
    from oneness_tor.services import accounts, ledger

    rows = []
    for name, label, purpose in (
            ("tor-ai-nft-wallet", "Tor AI-NFT Wallet",
             "advertising, API keys, launch and swap fees"),
            ("pump-owner", "TOR Pump (platform pool owner)",
             "platform liquidity + creator fee payouts"),
            ("swap-usdc-pool", "USDC pool custody",
             "backs every USDC book")):
        try:
            wallet = ledger.public_wallet(name)
        except Exception:  # noqa: BLE001 - a wallet that does not exist yet
            continue
        rows.append({"name": name, "label": label,
                     "address": wallet.get("address"), "purpose": purpose})
    try:
        receiver = node.fee_receiver()
    except Exception:  # noqa: BLE001
        receiver = ""
    return {"standard": "oneness-tor-fee-wallets/1", "fee_receiver": receiver,
            "wallets": rows, "support": accounts.support_email()}


def banners(limit: int = 6) -> dict:
    """Permanent banner first, then every active paid slot (newest ending first)."""
    from oneness_tor.services import meme

    now = int(time.time())
    paid = []
    for row in meme.PROMOTIONS.all():
        if str(row.get("kind")) != "banner" or str(row.get("status")) != "active":
            continue
        if int(row.get("ends_unix") or 0) <= now:
            continue
        paid.append({"slot": 0, "id": row.get("id"), "kind": "paid",
                     "title": row.get("title") or f"{row.get('symbol')} banner",
                     "subtitle": row.get("subtitle") or "Sponsored on the TOR network",
                     "image": row.get("image") or "",
                     "link": row.get("link") or "",
                     "symbol": row.get("symbol"),
                     "paid": True, "tor": row.get("tor"),
                     "ends_at": row.get("ends_at"),
                     "ends_unix": row.get("ends_unix")})
    paid.sort(key=lambda row: int(row.get("ends_unix") or 0), reverse=True)
    slots = ([PERMANENT_BANNER] + paid)[:max(1, int(limit))]
    for index, slot in enumerate(slots):
        slot["slot"] = index
    return {"standard": "oneness-tor-banners/1", "count": len(slots),
            "slots": slots, "rotation_seconds": 8,
            "price_tor": 50, "price_days": 7,
            "policy": ("slot 1 is permanent; paid slots run 7 days for 50 TOR and "
                       "rotate in a synchronous loop on every TOR page")}


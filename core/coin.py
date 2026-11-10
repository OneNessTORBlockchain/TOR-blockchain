"""TOR coin metadata — the Python twin of ``rust/torchain/src/coin.rs``.

Every page, API response and AI answer reads the coin's name, symbol, decimals
and logo from here so the branding never drifts. ``tests/test_coin_metadata.py``
parses the Rust module and fails if the two sides disagree.

The logo is pinned to IPFS. Both URIs are published: ``ipfs://<cid>`` for
NFT/token tooling and the HTTPS gateway URL for browsers (``<img src>``).

Runtime overrides (same env names as the Rust node):
``TOR_LOGO_URL`` / ``TOR_LOGO_IPFS``.
"""
from __future__ import annotations

import os

from core.validator import NATIVE_COIN_ID, TOR_DECIMALS

COIN_NAME = "OneNess TOR"
COIN_SYMBOL = "TOR"
COIN_DESCRIPTION = (
    "TOR is the native coin of the OneNess TOR chain — a Tor-first, "
    "privacy-preserving ledger with a permanent block record. The sending wallet "
    "chooses public or anonymous for every transfer, and nothing expires. Genesis "
    "mints 50,000,000 TOR to the founder wallet; fees are paid in TOR."
)

# Pinned logo (IPFS CIDv1, sha2-256 addressed) + Pinata gateway URL.
LOGO_CID = "bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze"
LOGO_IPFS = f"ipfs://{LOGO_CID}"
LOGO_URL = (
    "https://cyan-deep-magpie-676.mypinata.cloud/ipfs/"
    f"{LOGO_CID}"
    "?pinataGatewayToken=fvfdMA6s8XyRJ3C2Lp6_ScBrMLuuvUoo5tKWCGpXCkPdYVsm6mW_TWXlXE9PYi7A"
)

GENESIS_SUPPLY_TOR = 50_000_000
#: Privacy is chosen by the sending wallet for each transaction, not by a clock.
PRIVACY_STANDARD = "oneness-tor/privacy/1"


def logo_ipfs() -> str:
    """``ipfs://…`` logo URI (env ``TOR_LOGO_IPFS`` wins)."""
    return os.environ.get("TOR_LOGO_IPFS", "").strip() or LOGO_IPFS


def logo_url() -> str:
    """HTTPS logo URL for pages and wallets (env ``TOR_LOGO_URL`` wins)."""
    return os.environ.get("TOR_LOGO_URL", "").strip() or LOGO_URL


def as_dict() -> dict:
    """Compact block embedded in ``/api/chain/info`` and ``/api/pricing``."""
    return {
        "name": COIN_NAME,
        "symbol": COIN_SYMBOL,
        "decimals": TOR_DECIMALS,
        "native_coin_id": NATIVE_COIN_ID,
        "description": COIN_DESCRIPTION,
        "logo_url": logo_url(),
        "logo_ipfs": logo_ipfs(),
        "image": logo_url(),
    }


def metadata() -> dict:
    """Full ``GET /api/coin`` document, pinnable as the coin's metadata JSON."""
    return {
        "standard": "oneness-coin-metadata/1",
        "name": COIN_NAME,
        "symbol": COIN_SYMBOL,
        "decimals": TOR_DECIMALS,
        "description": COIN_DESCRIPTION,
        "image": logo_url(),
        "logo_url": logo_url(),
        "logo_ipfs": logo_ipfs(),
        "native_coin_id": NATIVE_COIN_ID,
        "properties": {
            "chain": "OneNess TOR",
            "chain_id": "072316",
            "consensus": "Proof-of-Authority (single authorized proposer)",
            "genesis_supply_tor": GENESIS_SUPPLY_TOR,
            "privacy": "per transaction: the sending wallet chooses public or anonymous",
            "logo_cid": LOGO_CID,
        },
    }

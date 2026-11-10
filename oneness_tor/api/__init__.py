"""HTTP blueprints. Every route is declared in :mod:`core.api_catalog`."""
from __future__ import annotations

from oneness_tor.api.brain import bp as brain_bp
from oneness_tor.api.chain import bp as chain_bp
from oneness_tor.api.fees import bp as fees_bp
from oneness_tor.api.meme import bp as meme_bp
from oneness_tor.api.nft import bp as nft_bp
from oneness_tor.api.pages import bp as pages_bp
from oneness_tor.api.services import bp as services_bp
from oneness_tor.api.wallet import bp as wallet_bp

__all__ = ["brain_bp", "chain_bp", "fees_bp", "meme_bp", "nft_bp", "pages_bp",
           "services_bp", "wallet_bp"]

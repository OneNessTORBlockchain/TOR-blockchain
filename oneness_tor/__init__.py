"""OneNess TOR — the Python API + dashboard that fronts the Rust ledger."""
from __future__ import annotations

from oneness_tor.app import __version__, create_app

__all__ = ["create_app", "__version__"]

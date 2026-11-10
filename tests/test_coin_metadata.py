"""TOR coin metadata — the logo, name and symbol must agree everywhere.

The Rust node (``rust/torchain/src/coin.rs``) and the Python layer
(``core/coin.py``) publish the same coin document; this module parses the Rust
constants and fails the build if either side drifts. No node build required.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import coin
from core.validator import NATIVE_COIN_ID, TOR_DECIMALS

ROOT = Path(__file__).resolve().parent.parent
RUST_SRC = ROOT / "rust" / "torchain" / "src" / "coin.rs"
README = ROOT / "rust" / "torchain" / "README.md"
CID_PATTERN = r"^bafy[a-z2-7]{20,}$"


def rust_const(name: str) -> str:
    """Read ``pub const NAME: &str = "…";`` (joining ``\\`` continuations)."""
    source = RUST_SRC.read_text(encoding="utf-8")
    match = re.search(rf'pub const {name}: &str =\s*"(.*?)";', source, re.S)
    assert match, f"{name} not found in {RUST_SRC}"
    return re.sub(r"\\\s*\n\s*", "", match.group(1))


def test_rust_and_python_metadata_agree():
    assert rust_const("COIN_NAME") == coin.COIN_NAME
    assert rust_const("COIN_SYMBOL") == coin.COIN_SYMBOL
    assert rust_const("LOGO_CID") == coin.LOGO_CID
    assert rust_const("LOGO_IPFS") == coin.LOGO_IPFS
    assert rust_const("LOGO_URL") == coin.LOGO_URL


def test_logo_uris_point_at_the_same_pinned_cid():
    assert re.match(CID_PATTERN, coin.LOGO_CID), coin.LOGO_CID
    assert coin.logo_ipfs() == f"ipfs://{coin.LOGO_CID}"
    assert f"/ipfs/{coin.LOGO_CID}" in coin.logo_url()
    assert coin.logo_url().startswith("https://")
    assert "pinataGatewayToken=" in coin.logo_url()


def test_supply_and_decimals_match_the_ledger():
    assert coin.GENESIS_SUPPLY_TOR == 50_000_000
    assert coin.metadata()["decimals"] == TOR_DECIMALS == 8
    assert coin.metadata()["native_coin_id"] == NATIVE_COIN_ID


def test_coin_document_has_no_public_data_window():
    """The 30-day window is gone: the coin document must not advertise one."""
    properties = coin.metadata()["properties"]
    assert "public_data_window_days" not in properties
    assert "public or anonymous" in properties["privacy"]
    assert not hasattr(coin, "PUBLIC_DATA_WINDOW_DAYS")


def test_metadata_document_has_the_keys_wallets_read():
    document = coin.metadata()
    for key in ("name", "symbol", "decimals", "description", "image"):
        assert document.get(key), f"missing {key}"
    assert document["symbol"] == "TOR"
    assert document["image"] == document["logo_url"] == coin.LOGO_URL
    assert document["properties"]["logo_cid"] == coin.LOGO_CID
    assert json.loads(json.dumps(document)) == document  # JSON-safe


def test_compact_block_is_a_subset_of_the_document():
    compact, document = coin.as_dict(), coin.metadata()
    assert set(compact) <= set(document)
    assert all(compact[key] == document[key] for key in compact)


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv("TOR_LOGO_URL", "https://example.onion/tor-logo.png")
    monkeypatch.setenv("TOR_LOGO_IPFS", "ipfs://bafyexample")
    assert coin.logo_url() == "https://example.onion/tor-logo.png"
    assert coin.logo_ipfs() == "ipfs://bafyexample"
    assert coin.metadata()["image"] == "https://example.onion/tor-logo.png"


def test_readme_documents_the_logo():
    text = README.read_text(encoding="utf-8")
    assert "/api/coin" in text
    assert coin.LOGO_CID in text

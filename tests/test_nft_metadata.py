"""Unit tests for NFT metadata resolution and the AI-NFT prompt field.

No network: the RPC/metadata opener is injected everywhere. The PDA derivation
is cross-checked against ``solders`` when that package happens to be installed
(it is not a runtime dependency of the node).
"""
from __future__ import annotations

import json

import pytest

from core import nft
from core.errors import NftNotFound, NftUnavailable, ValidationError


def _borsh(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(4, "little") + raw


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int | None = None) -> bytes:
        if size in (None, -1):
            return self.payload
        return self.payload[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def fake_opener(payload: bytes, seen: list | None = None):
    def opener(request, timeout=None):  # noqa: ANN001 - mirrors urlopen
        if seen is not None:
            seen.append({"url": request.full_url, "timeout": timeout})
        return FakeResponse(payload)
    return opener


# ------------------------------------------------------------------ identity
def test_persona_block_starts_with_the_hardcoded_header():
    block = nft.persona_block({
        "name": "Cyan Warden", "symbol": "WARDEN",
        "description": "A cyan-lit warden of the onion relays.",
        "address": "TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion",
        "attributes": [{"trait_type": "Aura", "value": "cyan"}],
        "image": "https://example.com/warden.png",
    }, "7")
    lines = block.splitlines()
    assert lines[0] == nft.NFT_HEADER == "I AM THIS NFT"
    assert lines[1].startswith("(token metadata")
    assert "- Name: Cyan Warden ($WARDEN)" in block
    assert "- Token ID: #7" in block
    assert "- Description: A cyan-lit warden" in block
    assert "Aura: cyan" in block
    assert "Never give financial advice" in block


def test_nft_block_is_injected_right_after_the_persona():
    from core import ollama

    prompt = ollama.build_system_prompt(
        nft={"name": "Cyan Warden", "description": "guards the relays",
             "address": "TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion"},
        token_id=3)
    lines = prompt.splitlines()
    assert lines[0] == "I AM THIS NFT"
    assert lines[1].startswith("(token metadata")
    assert prompt.index("I AM THIS NFT") < prompt.index(ollama.PERSONA)
    assert "- Token ID: #3" in prompt
    assert ollama.NFT_REMINDER.split("{")[0].strip() in prompt

    plain = ollama.build_system_prompt()
    assert "I AM THIS NFT\n(token metadata" not in plain
    assert "FINAL REMINDER — YOU ARE THIS NFT" not in plain


def test_compact_nft_chat_prompt_has_no_api_schema():
    from core import ollama

    nft = {"name": "Cyan Warden", "description": "guards",
           "address": "TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion", "token_id": "2"}
    compact = ollama.build_nft_prompt(nft, "2")
    assert compact.startswith("I AM THIS NFT")
    assert "chain.info" not in compact          # no API catalog
    assert "YOU ARE THIS NFT" in compact
    assert "Cyan Warden (#2)" in compact
    assert len(compact) < 2000


# ------------------------------------------------------------- normalisation
def test_normalize_cleans_attributes_and_rewrites_ipfs():
    resolved = nft.normalize({
        "name": "  Warden  ", "symbol": "wrd",
        "description": "desc",
        "image": "ipfs://bafyabc/warden.png",
        "external_url": "https://example.com",
        "attributes": [
            {"trait_type": "Aura", "value": "cyan"},
            {"trait_type": "Empty", "value": ""},
            "not-a-dict",
            {"trait": "Eyes", "value": 9},
        ],
    }, address="TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion", token_id="4")
    assert resolved["name"] == "Warden"
    assert resolved["symbol"] == "wrd"
    assert resolved["image"].startswith("https://ipfs.io/ipfs/bafyabc/warden.png")
    assert resolved["attributes"] == [
        {"trait_type": "Aura", "value": "cyan"},
        {"trait_type": "Eyes", "value": "9"},
    ]
    assert resolved["chain"] == "oneness-tor"


def test_resolve_uri_and_mint_detection():
    assert nft.resolve_uri("ipfs://cid/x.json").startswith("https://ipfs.io/ipfs/")
    assert nft.resolve_uri("ar://txid").startswith("https://arweave.net/")
    assert nft.resolve_uri("https://example.com/m.json") == "https://example.com/m.json"
    assert nft.is_solana_mint("So11111111111111111111111111111111111111112")
    assert not nft.is_solana_mint("TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion")
    assert not nft.is_solana_mint("not-an-address")
    assert nft.normalize_token_id("#12") == "12"
    assert nft.normalize_token_id("") is None
    with pytest.raises(ValidationError):
        nft.normalize_token_id("abc")


# ------------------------------------------------------------------ Solana
def test_metadata_address_matches_solders_when_available():
    solders = pytest.importorskip("solders.pubkey")
    mint = "So11111111111111111111111111111111111111112"
    program = solders.Pubkey.from_string(nft.METAPLEX_PROGRAM)
    expected = solders.Pubkey.find_program_address(
        [b"metadata", bytes(program), bytes(solders.Pubkey.from_string(mint))],
        program)[0]
    assert nft.metadata_address(mint) == str(expected)


def test_parse_metaplex_account_reads_name_symbol_and_uri():
    data = (bytes([4]) + b"\x11" * 32 + b"\x22" * 32
            + _borsh("Cyan Warden\x00") + _borsh("WARDEN")
            + _borsh("https://x/y.json") + b"\x00\x00" + b"\x00")
    parsed = nft.parse_metaplex_account(data)
    assert parsed["name"] == "Cyan Warden"
    assert parsed["symbol"] == "WARDEN"
    assert parsed["uri"] == "https://x/y.json"
    with pytest.raises(NftNotFound):
        nft.parse_metaplex_account(b"\x09" + b"\x00" * 80)


def test_fetch_solana_metadata_uses_the_injected_rpc_and_metadata_uri():
    import base64

    mint = "So11111111111111111111111111111111111111112"
    account = (bytes([4]) + b"\x11" * 32 + bytes(nft.decode_mint(mint))
               + _borsh("Cyan Warden") + _borsh("WARDEN")
               + _borsh("https://example.com/meta.json") + b"\x00\x00\x00")
    rpc_payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "value": {"data": [base64.b64encode(account).decode(), "base64"]}}}).encode()
    offchain = json.dumps({"name": "Cyan Warden", "description": "guards",
                           "image": "https://example.com/w.png"}).encode()
    calls: list[dict] = []

    def opener(request, timeout=None):  # noqa: ANN001
        calls.append({"url": request.full_url, "body": request.data})
        if "example.com/meta.json" in request.full_url:
            return FakeResponse(offchain)
        return FakeResponse(rpc_payload)

    resolved = nft.fetch_solana_metadata(mint, token_id=2, opener=opener)
    assert resolved["source"] == "solana"
    assert resolved["name"] == "Cyan Warden"
    assert resolved["description"] == "guards"
    assert resolved["token_id"] == "2"
    assert resolved["metadata_account"] == nft.metadata_address(mint)
    assert any("example.com/meta.json" in call["url"] for call in calls)
    assert json.loads(calls[0]["body"])["method"] == "getAccountInfo"


def test_rpc_failure_is_honest():
    def opener(request, timeout=None):  # noqa: ANN001
        raise OSError("connection refused")

    with pytest.raises(NftUnavailable) as problem:
        nft.fetch_solana_metadata(
            "So11111111111111111111111111111111111111112", opener=opener)
    assert problem.value.code == "NFT_RPC_UNAVAILABLE"
    assert "ONENESS_SOLANA_RPC" in problem.value.message


# --------------------------------------------------------------- metadata URI
def test_fetch_metadata_uri_parses_and_respects_the_size_cap():
    seen: list[dict] = []
    payload = json.dumps({"name": "Doc", "description": "from a uri",
                          "image": "ipfs://cid/pic.png"}).encode()
    resolved = nft.fetch_metadata_uri("ipfs://cid/meta.json", token_id=5,
                                       opener=fake_opener(payload, seen))
    assert resolved["name"] == "Doc"
    assert resolved["token_id"] == "5"
    assert resolved["image"].startswith("https://ipfs.io/ipfs/cid/pic.png")
    assert seen[0]["url"].startswith("https://ipfs.io/ipfs/cid/meta.json")

    with pytest.raises(ValidationError):
        nft.fetch_metadata_uri("https://example.com/m.json",
                               opener=fake_opener(b"{" + b" " * 300_000 + b"}"))
    with pytest.raises(NftNotFound):
        nft.fetch_metadata_uri("https://example.com/m.json",
                               opener=fake_opener(b"not json"))
    with pytest.raises(ValidationError):
        nft.fetch_metadata_uri("tor://hidden/meta.json")

"""Pinata/IPFS: the metadata a meme launch or an AI-NFT leaves behind.

No test touches the network: ``core.ipfs._post`` is replaced with a recorder, so
the assertions are about the document that *would* be pinned and the honest
failure path when no credential exists.
"""
from __future__ import annotations

import json

import pytest

from core import ipfs
from oneness_tor.services import meme, nft_studio


def _stub_pinata(monkeypatch, cid="bafy-test") -> dict:
    monkeypatch.setenv("PINATA_JWT", "test-jwt-value")
    seen: dict = {}

    def fake_post(url, body, headers):
        seen.update(url=url, body=body, headers=dict(headers))
        return {"IpfsHash": cid}, None

    monkeypatch.setattr(ipfs, "_post", fake_post)
    return seen


def test_gateway_resolution_prefers_the_configured_gateway(monkeypatch):
    assert ipfs.resolve("ipfs://cid/x.json") == "https://ipfs.io/ipfs/cid/x.json"
    monkeypatch.setenv("IPFS_GATEWAY", "https://cyan-deep-magpie-676.mypinata.cloud")
    assert ipfs.resolve("ipfs://cid/x.json") == (
        "https://cyan-deep-magpie-676.mypinata.cloud/ipfs/cid/x.json")
    assert ipfs.cid_of("https://gw.example/ipfs/cid/x.json") == "cid"
    assert ipfs.resolve("https://example.com/a.png") == "https://example.com/a.png"
    assert ipfs.resolve("ar://txid") == "https://arweave.net/txid"
    assert ipfs.cid_of("https://example.com/a.png") == ""


def test_private_gateway_reads_take_the_token_exactly_once(monkeypatch):
    monkeypatch.setenv("IPFS_GATEWAY", "https://cyan-deep-magpie-676.mypinata.cloud")
    monkeypatch.setenv("PINATA_GATEWAY_TOKEN", "gw-token")
    assert ipfs.gateway() == "https://cyan-deep-magpie-676.mypinata.cloud/ipfs/"
    assert ipfs.resolve("ipfs://cid/x.json") == (
        "https://cyan-deep-magpie-676.mypinata.cloud/ipfs/cid/x.json"
        "?pinataGatewayToken=gw-token")
    # a URL that already carries a query is never rewritten
    assert ipfs.resolve("https://example.com/a.png?x=1") == "https://example.com/a.png?x=1"
    status = ipfs.status()
    assert status["gateway_token"] is True
    assert "gw-token" not in json.dumps(status)


def test_the_token_may_already_sit_in_the_gateway_url(monkeypatch):
    monkeypatch.setenv("IPFS_GATEWAY",
                       "https://gw.mypinata.cloud/?pinataGatewayToken=abc123")
    assert ipfs.gateway() == "https://gw.mypinata.cloud/ipfs/"
    assert ipfs.resolve("ipfs://cid") == (
        "https://gw.mypinata.cloud/ipfs/cid?pinataGatewayToken=abc123")


def test_pinned_urls_are_ready_for_a_browser(monkeypatch):
    _stub_pinata(monkeypatch, cid="bafy-tok")
    monkeypatch.setenv("IPFS_GATEWAY", "https://cyan-deep-magpie-676.mypinata.cloud")
    monkeypatch.setenv("PINATA_GATEWAY_TOKEN", "gw-token")
    result = ipfs.pin_json({"x": 1})
    assert result["url"] == ("https://cyan-deep-magpie-676.mypinata.cloud/ipfs/"
                            "bafy-tok?pinataGatewayToken=gw-token")
    assert result["uri"] == "ipfs://bafy-tok"


def test_pin_json_sends_the_document_and_returns_a_real_gateway(monkeypatch):
    seen = _stub_pinata(monkeypatch)
    result = ipfs.pin_json({"standard": "x", "name": "Meme"}, name="MEME.json")

    assert result["pinned"] is True
    assert result["cid"] == "bafy-test"
    assert result["uri"] == "ipfs://bafy-test"
    assert result["url"].endswith("/ipfs/bafy-test")
    assert result["provider"] == "pinata"

    sent = json.loads(seen["body"])
    assert sent["pinataContent"] == {"standard": "x", "name": "Meme"}
    assert sent["pinataMetadata"]["name"] == "MEME.json"
    assert seen["headers"]["authorization"] == "Bearer test-jwt-value"
    assert seen["url"].endswith("/pinJSONToIPFS")


def test_pin_data_url_uploads_the_decoded_bytes(monkeypatch):
    seen = _stub_pinata(monkeypatch, cid="bafy-image")
    result = ipfs.pin_data_url("data:image/png;base64,iVBORw0KGgo=", filename="logo")
    assert result["pinned"] is True and result["cid"] == "bafy-image"
    assert result["content_type"] == "image/png"
    assert b"PNG" in seen["body"]
    assert "multipart/form-data" in seen["headers"]["content-type"]


def test_without_a_credential_nothing_is_claimed():
    # the shared fixture removed PINATA_*: no credential, no pretend pin
    result = ipfs.pin_json({"name": "nobody"})
    assert result["pinned"] is False
    assert "PINATA_JWT" in result["error"]
    status = ipfs.status()
    assert status["enabled"] is False
    assert status["configured"] == {"jwt": False, "api_key": False, "secret": False}


def test_status_never_echoes_a_credential(monkeypatch):
    _stub_pinata(monkeypatch)
    text = json.dumps(ipfs.status())
    assert "test-jwt-value" not in text
    assert ipfs.status()["configured"]["jwt"] is True


def test_a_pin_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("PINATA_JWT", "test-jwt-value")
    monkeypatch.setattr(ipfs, "_post", lambda url, body, headers: (None, "pinata HTTP 401"))
    result = ipfs.pin_json({"x": 1})
    assert result["pinned"] is False
    assert "401" in result["error"]



# ----------------------------------------------------------------- the minters
def test_nft_mint_pins_its_metadata(sandbox, make_wallet, monkeypatch):
    owner = make_wallet("owner")
    _stub_pinata(monkeypatch, cid="bafy-nft")
    monkeypatch.setattr(nft_studio.ledger, "balance_of",
                        lambda name: {"balance_micro": 10 ** 9, "balance_tor": "10"})
    monkeypatch.setattr(nft_studio.ledger, "charge", lambda *a, **k: {
        "code": "nft_mint", "txid": "tx_nft", "height": 4,
        "amount_micro": 300_000_000, "amount_tor": "3",
        "from": owner["address"]})

    minted = nft_studio.mint(name="Cyan Warden", description="A cyan warden",
                             image="https://example.com/warden.png",
                             wallet="owner")
    record = minted["nft"]
    assert record["metadata_pinned"] is True
    assert record["metadata_cid"] == "bafy-nft"
    assert record["metadata_uri"] == "ipfs://bafy-nft"
    assert record["metadata"]["properties"]["metadata_uri"] == "ipfs://bafy-nft"
    assert minted["resolved"]["metadata_cid"] == "bafy-nft"
    assert minted["metadata"]["pinned"] is True


def test_meme_launch_pins_its_token_metadata(sandbox, make_wallet, monkeypatch):
    owner = make_wallet("owner")
    _stub_pinata(monkeypatch, cid="bafy-meme")
    monkeypatch.setattr(meme, "chain_tokens", lambda: {})
    monkeypatch.setattr(meme.ledger, "balance_of",
                        lambda name: {"balance_micro": 10 ** 9, "balance_tor": "10"})
    monkeypatch.setattr(meme.ledger, "send_micro",
                        lambda *a, **k: {"txid": "tx_meme", "height": 5})

    launched = meme.create_token(symbol="MEME", name="Meme Coin", supply=1000,
                                 description="the first meme", wallet="owner",
                                 image="ipfs://bafy-logo")
    row = launched["launcher"]
    assert row["metadata_pinned"] is True
    assert row["metadata_uri"] == "ipfs://bafy-meme"
    assert row["metadata"]["symbol"] == "MEME"
    assert row["metadata"]["properties"]["owner"] == owner["address"]
    assert launched["metadata"]["pinned"] is True
    assert meme.metadata_public("MEME")["metadata_uri"] == "ipfs://bafy-meme"


def test_meme_launch_can_skip_pinning_but_says_so(sandbox, make_wallet, monkeypatch):
    make_wallet("owner")
    _stub_pinata(monkeypatch, cid="bafy-unused")
    monkeypatch.setattr(meme, "chain_tokens", lambda: {})
    monkeypatch.setattr(meme.ledger, "balance_of",
                        lambda name: {"balance_micro": 10 ** 9, "balance_tor": "10"})
    monkeypatch.setattr(meme.ledger, "send_micro",
                        lambda *a, **k: {"txid": "tx_meme", "height": 5})

    launched = meme.create_token(symbol="NOPIN", supply=10, wallet="owner",
                                 pin_metadata=False)
    assert launched["launcher"]["metadata_pinned"] is False
    assert launched["launcher"]["metadata_uri"] == ""
    assert launched["metadata"]["pinned"] is False

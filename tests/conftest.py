"""Shared fixtures: a sandboxed data dir and a real Ed25519 wallet factory.

The factory writes genuine keystore files (address derived exactly the way the
chain derives it) so signup/login, ownership and deployment tests exercise the
real code paths without needing the Rust binary just to make a key.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import base58
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                         PrivateFormat, PublicFormat)

from oneness_tor.services import accounts, ledger, wallet_access


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every store at ``tmp_path`` and clear in-process sessions."""
    monkeypatch.setenv("ONENESS_DATA_DIR", str(tmp_path))
    # keep the suite quick: production costs are 2**14 / 2**15 iterations
    monkeypatch.setattr(accounts, "SCRYPT", {"n": 2 ** 10, "r": 8, "p": 1, "dklen": 32})
    monkeypatch.setattr(accounts, "SEAL_SCRYPT", {"n": 2 ** 10, "r": 8, "p": 1})
    (tmp_path / "wallets").mkdir(parents=True, exist_ok=True)
    accounts._SESSIONS.clear()
    accounts._FAILS.clear()
    wallet_access._SESSIONS.clear()
    wallet_access._CHALLENGES.clear()
    return tmp_path


@pytest.fixture(autouse=True)
def offline_token_media(monkeypatch, tmp_path):
    """No unit test may reach CoinGecko/DexScreener: the logo cache stays local.

    ``tests/test_token_media.py`` opts back in per test (with the HTTP layer
    monkeypatched), so the fetching path is still covered — just never online.
    The public-ledger document is cached for a few seconds in production, so the
    cache is cleared for every test too: one test's stubbed node can never leak
    into the next.
    """
    monkeypatch.setenv("ONENESS_TOKEN_IMAGES", "0")
    monkeypatch.setenv("ONENESS_TOKEN_IMAGES", "0")
    # the 120-asset market feed must never be fetched by a unit test either
    monkeypatch.setenv("ONENESS_UNIVERSE", "0")
    # a developer's local .env (real Pinata credentials, a private gateway) must
    # never leak into a unit test: the suite asserts the documented defaults.
    for name in ("IPFS_GATEWAY", "ONENESS_IPFS_GATEWAY", "PINATA_JWT",
                 "PINATA_API_KEY", "PINATA_API_SECRET", "PINATA_SECRET_API_KEY",
                 "PINATA_GATEWAY_TOKEN", "ONENESS_IPFS_GATEWAY_TOKEN",
                 "ONENESS_IPFS_PIN",
                 # the hosted-Ollama token in .env is the operator's, not the
                 # suite's: tests assert the documented "no key" behaviour and
                 # must never borrow it (or reach ollama.com with it)
                 "ONENESS_OLLAMA_API_KEY", "OLLAMA_API_KEY",
                 # the fleet sync wiring in .env points at the operator's origin
                 # tunnel: a unit test must decide its own role and origin
                 "ONENESS_SYNC_ORIGIN", "ONENESS_SYNC_TOKEN", "ONENESS_SYNC_INTERVAL",
                 "ONENESS_SYNC_AUTO", "ONENESS_NODE_ROLE",
                 # .env publishes this deployment as an onion with a committed
                 # key: a unit test must decide for itself whether Tor exists, so
                 # neither the key nor the boot-time autostart may leak into it
                 "ONENESS_TOR_ONION_KEY", "ONENESS_TOR_AUTOSTART",
                 "ONENESS_TOR_PUBLISH_NODE", "ONENESS_TOR_AUTO_INSTALL",
                 "ONENESS_TOR_CONTROL_PORT", "ONENESS_TOR_SOCKS_PORT",
                 "ONENESS_TOR_CONTROL_HOST", "ONENESS_TOR_BINARY",
                 # nor may an operator's privacy policy leak in: the suite
                 # asserts the documented default (public, Tor-Anonymous-onion)
                 "ONENESS_PRIVACY_DEFAULT", "ONENESS_PRIVACY_LABEL",
                 # nor may the host's own identity leak in (self-origin checks
                 # read these: a test is never "the public origin")
                 "ONENESS_PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL",
                 "RENDER_EXTERNAL_HOSTNAME"):
        monkeypatch.delenv(name, raising=False)
    # ``chain.data_dir`` is a path of its own (chain.data_dir: data/chain): moving
    # ONENESS_DATA_DIR into a sandbox does not move the ledger out of the checkout.
    # Without this, a machine that holds a live chain answers auth.expected_hash()
    # from data/chain/state.json, and every admin-token test 403s on a token that
    # is perfectly valid for its own sandbox. A test that needs a real ledger
    # (tests/test_api_app.py runs the Rust node) sets ONENESS_CHAIN_DIR again,
    # which wins because its fixture runs after this one.
    monkeypatch.setenv("ONENESS_CHAIN_DIR", str(tmp_path / "chain"))
    from oneness_tor.services import public_ledger, node_sync

    # the committed fleet wiring (config.fleet.yaml) describes the operator's real
    # origin tunnel: a unit test decides its own role, so the fleet file is
    # invisible unless a test points node_sync.FLEET_PATH at its own copy
    monkeypatch.setattr(node_sync, "FLEET_PATH", tmp_path / "no-fleet.yaml",
                        raising=False)
    monkeypatch.setattr(node_sync, "_FLEET_CACHE", {"mtime": None, "data": {}},
                        raising=False)
    # the hosts this machine has really been addressed by (data/node/self_hosts.json)
    # would make a test node believe it is the public origin: start every test with
    # none, and let a test that cares point the constant at its own copy
    from oneness_tor.services import base_url
    monkeypatch.setattr(base_url, "REMEMBERED_PATH", tmp_path / "self_hosts.json",
                        raising=False)

    public_ledger.reset()
    yield
    public_ledger.reset()


@pytest.fixture
def make_wallet(sandbox, monkeypatch):
    """Create keystore wallets and make ``ledger.new_wallet`` use them."""

    def factory(name: str = "owner", words: int = 24) -> dict:
        private = Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        secret = private.private_bytes(Encoding.Raw, PrivateFormat.Raw,
                                       NoEncryption())
        wallet = {
            "version": 1, "kind": "oneness-tor-test", "algorithm": "ed25519",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "address": wallet_access.address_for_pubkey(raw),
            "pubkey_base58": base58.b58encode(raw).decode("ascii"),
            "secret_key_base58": base58.b58encode(secret + raw).decode("ascii"),
            "mnemonic": "one two three four five six seven eight nine ten eleven twelve",
        }
        path = Path(sandbox) / "wallets" / f"{name}.json"
        path.write_text(json.dumps(wallet, indent=2), encoding="utf-8")
        return wallet

    def fake_new_wallet(name: str, words: int = 24) -> dict:
        wallet = factory(name, words)
        return {"name": name, "address": wallet["address"],
                "pubkey_base58": wallet["pubkey_base58"],
                "mnemonic": wallet["mnemonic"],
                "path": str(Path(sandbox) / "wallets" / f"{name}.json")}

    monkeypatch.setattr(ledger, "new_wallet", fake_new_wallet)
    return factory

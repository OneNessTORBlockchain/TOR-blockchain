"""The logo mirror: every token picture is copied onto the node and served locally.

Fully offline — ``token_media._fetch_bytes`` is monkeypatched with picture-shaped
answers, so the copy, the cache pointer, the page contract and the admin endpoint
are all exercised without a single network call. What is asserted is the promise:
a token row never points at a third-party host, a token minted later is mirrored by
``remember``, and a picture that is not an image is reported instead of served.
"""
from __future__ import annotations

import json
import time

import pytest

from oneness_tor.services import token_media

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 16


@pytest.fixture
def logos(sandbox, monkeypatch):
    """Fetching is allowed, but the metadata provider is never called."""
    monkeypatch.setenv("ONENESS_TOKEN_IMAGES", "1")
    monkeypatch.setattr(token_media, "_fetch_json", lambda url: (None, "offline test"))
    monkeypatch.setattr(token_media, "_may_attempt", lambda: False)
    return monkeypatch


def _store(sandbox):
    return sandbox / "metadata" / "token-images.json"


def test_mirror_copies_every_logo_and_records_the_pointer(sandbox, logos):
    token_media.remember(
        {"BTC": {"image": "https://images.test/btc.png", "source": "coingecko",
                 "rank": 1},
         "ETH": {"image": "https://images.test/eth.jpg", "source": "coingecko"}},
        mirror_logos=False)
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        return (PNG if url.endswith(".png") else JPEG,
                "image/png" if url.endswith(".png") else "image/jpeg", None)

    logos.setattr(token_media, "_fetch_bytes", fetch)
    report = token_media.mirror()

    assert report["mirrored"] == 2 and report["skipped"] == 0
    assert report["failed"] == [] and report["enabled"] is True
    folder = sandbox / "metadata" / "token-logos"
    assert (folder / "BTC.png").is_file() and (folder / "ETH.jpg").is_file()
    assert (folder / "BTC.png").read_bytes() == PNG

    doc = json.loads(_store(sandbox).read_text(encoding="utf-8"))
    entry = doc["images"]["BTC"]
    assert entry["logo_file"] == "BTC.png" and entry["logo_bytes"] == len(PNG)
    assert entry["logo_type"] == "image/png" and len(entry["logo_sha256"]) == 64
    assert entry["image"] == "https://images.test/btc.png"      # provenance kept
    assert doc["mirror"]["count"] == 2 and doc["mirror"]["route"] == "/assets/tokens"
    assert report["count"] == 2 and report["total"] == 2
    assert calls == ["https://images.test/btc.png", "https://images.test/eth.jpg"]



def test_a_second_run_downloads_nothing_and_force_recopies(sandbox, logos):
    token_media.remember(
        {"BTC": {"image": "https://images.test/btc.png", "source": "coingecko"}},
        mirror_logos=False)
    calls: list[str] = []
    logos.setattr(token_media, "_fetch_bytes",
                  lambda url: (calls.append(url) or PNG, "image/png", None))

    assert token_media.mirror()["mirrored"] == 1
    again = token_media.mirror()
    assert again["mirrored"] == 0 and again["skipped"] == 1
    assert len(calls) == 1

    forced = token_media.mirror(force=True)
    assert forced["mirrored"] == 1 and len(calls) == 2


def test_a_picture_that_is_not_an_image_is_reported_not_served(sandbox, logos):
    token_media.remember(
        {"BAD": {"image": "https://images.test/page.html", "source": "news"},
         "GONE": {"image": "https://images.test/gone.png", "source": "coingecko"}},
        mirror_logos=False)

    def fetch(url):
        if "gone" in url:
            return None, "", f"HTTP 404 from {url}"
        return b"<!doctype html>", "text/html", None

    logos.setattr(token_media, "_fetch_bytes", fetch)
    report = token_media.mirror()

    assert report["mirrored"] == 0 and report["count"] == 0
    assert {row["symbol"] for row in report["failed"]} == {"BAD", "GONE"}
    folder = sandbox / "metadata" / "token-logos"
    assert not (folder.is_dir() and [path for path in folder.iterdir()
                                     if path.is_file()])


def test_decorate_hands_the_page_the_local_file(sandbox, logos):
    token_media.remember(
        {"BTC": {"image": "https://images.test/btc.png", "source": "coingecko",
                 "rank": 1}}, mirror_logos=False)
    logos.setattr(token_media, "_fetch_bytes", lambda url: (PNG, "image/png", None))
    token_media.mirror()

    rows = [{"symbol": "BTC", "image": "https://images.test/btc.png"},
            {"symbol": "TOR"}, {"symbol": "GHOST"}]
    token_media.decorate(rows)

    assert rows[0]["image"] == "/assets/tokens/BTC.png"
    assert rows[0]["image_uri"] == "https://images.test/btc.png"
    assert "mirrored" in rows[0]["image_source"]
    assert rows[0]["image_rank"] == 1
    assert rows[1]["image_source"].startswith("pinned IPFS")
    assert rows[2]["image"] == "" and "identicon" in rows[2]["image_source"]



def test_the_page_route_serves_the_mirrored_picture(sandbox, logos):
    from oneness_tor.app import create_app

    folder = sandbox / "metadata" / "token-logos"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "BTC.png").write_bytes(PNG)
    client = create_app().test_client()

    served = client.get("/assets/tokens/BTC.png")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.data == PNG
    assert client.get("/assets/tokens/NOPE.png").status_code == 404
    assert client.get("/assets/tokens/..%2Ftoken-images.json").status_code == 404


def test_remember_mirrors_a_token_minted_later(sandbox, logos):
    logos.setattr(token_media, "_fetch_bytes", lambda url: (JPEG, "image/jpeg", None))
    report = token_media.remember(
        {"NEW": {"image": "https://images.test/new.jpg", "source": "launcher image",
                 "name": "New Coin"}})

    assert report["stored"] == 1 and report["mirrored"] == 1
    assert report["failed"] == [] and report["logos"]["count"] == 1
    assert (sandbox / "metadata" / "token-logos" / "NEW.jpg").is_file()

    rows = [{"symbol": "NEW"}]
    token_media.decorate(rows)
    assert rows[0]["image"] == "/assets/tokens/NEW.jpg"


def test_the_mirror_endpoint_is_admin_only_and_copies(sandbox, logos, monkeypatch):
    from oneness_tor.app import create_app
    from oneness_tor.services import auth

    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(auth, "expected_hash", lambda: None)
    token_media.remember(
        {"BTC": {"image": "https://images.test/btc.png", "source": "coingecko"}},
        mirror_logos=False)
    logos.setattr(token_media, "_fetch_bytes", lambda url: (PNG, "image/png", None))
    client = create_app().test_client()

    assert client.post("/api/tokens/images/mirror", json={}).status_code == 403
    allowed = client.post("/api/tokens/images/mirror", json={"symbols": ["BTC"]},
                          headers={"x-admin-token": "test-admin-token"})
    assert allowed.status_code == 200
    body = allowed.get_json()["data"]
    assert body["mirrored"] == 1 and body["count"] == 1
    assert body["route"] == "/assets/tokens"


def test_the_background_mirror_reports_when_there_is_nothing_to_copy(sandbox, logos):
    report = token_media.mirror_in_background()
    assert report["started"] is False and "empty" in report["note"]


def test_the_background_mirror_copies_what_is_missing(sandbox, logos):
    token_media.remember(
        {"BTC": {"image": "https://images.test/btc.png", "source": "coingecko"}},
        mirror_logos=False)
    logos.setattr(token_media, "_fetch_bytes", lambda url: (PNG, "image/png", None))

    report = token_media.mirror_in_background()
    assert report["started"] is True
    target = sandbox / "metadata" / "token-logos" / "BTC.png"
    for _ in range(100):
        if target.is_file():
            break
        time.sleep(0.05)
    assert target.is_file() and target.read_bytes() == PNG


def test_a_webp_logo_is_served_as_an_image(sandbox, logos):
    """Market logos come as webp too: the route must say ``image/*``, not bytes."""
    from oneness_tor.app import create_app

    folder = sandbox / "metadata" / "token-logos"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "WEBP.webp").write_bytes(b"RIFF" + b"w" * 8 + b"WEBPVP8 ")

    answer = create_app().test_client().get("/assets/tokens/WEBP.webp")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("image/webp")


def test_a_mint_mirrors_its_picture_and_names_it_in_the_token_document(
        sandbox, make_wallet, logos, monkeypatch):
    """Image metadata is written **at mint**: the document carries both URLs."""
    from oneness_tor.services import meme

    make_wallet("owner")
    logos.setattr(token_media, "_fetch_bytes", lambda url: (PNG, "image/png", None))
    logos.setattr(meme, "chain_tokens", lambda: {})
    logos.setattr(meme.ledger, "balance_of",
                  lambda name: {"balance_micro": 10 ** 9, "balance_tor": "10"})
    logos.setattr(meme.ledger, "send_micro",
                  lambda *a, **k: {"txid": "tx_mint", "height": 4})

    launched = meme.create_token(symbol="MINT", name="Mint", supply=500,
                                 image="https://images.test/mint.png",
                                 wallet="owner", pin_metadata=False)
    assert (sandbox / "metadata" / "token-logos" / "MINT.png").is_file()
    row = launched["launcher"]
    assert row["logo"] == "/assets/tokens/MINT.png"
    assert row["metadata"]["image"] == "https://images.test/mint.png"
    assert row["metadata"]["properties"]["image_mirror"] == "/assets/tokens/MINT.png"
    assert token_media.served_logo("MINT") == "/assets/tokens/MINT.png"


def test_a_re_mint_keeps_one_launcher_row_per_ticker(sandbox, make_wallet, logos,
                                                    monkeypatch):
    """The chain keys a token by its ticker, so the launcher store must too."""
    from oneness_tor.services import meme

    make_wallet("owner")
    logos.setattr(meme, "chain_tokens", lambda: {})
    logos.setattr(meme.ledger, "balance_of",
                  lambda name: {"balance_micro": 10 ** 9, "balance_tor": "10"})
    logos.setattr(meme.ledger, "send_micro",
                  lambda *a, **k: {"txid": "tx_remint", "height": 6})
    meme.TOKENS.save([{"id": "meme_0001", "symbol": "MINT", "supply": 1_000_000,
                       "image": "https://images.test/old.png",
                       "created_unix": 1, "created_at": "2026-01-01T00:00:00+00:00"}])

    launched = meme.create_token(symbol="MINT", supply=500, wallet="owner",
                                 image="https://images.test/mint.png",
                                 pin_metadata=False)
    rows = [row for row in meme.TOKENS.all() if row["symbol"] == "MINT"]
    assert len(rows) == 1
    assert rows[0]["supply"] == 500 and rows[0]["id"] == launched["launcher"]["id"]


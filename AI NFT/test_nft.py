"""NFT studio tests: instruction encoding, free-model filter, system prompt.

Pure units only — no network, no on-chain state.
"""
import base64
import json
import struct

import pytest
import requests
from solders.pubkey import Pubkey

from app import ai_openrouter as ai_mod
from app.solana import metadata as meta_mod
from app.solana import nft as nft_mod
from app.solana.borsh import Reader

METAPLEX = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")


def test_metadata_v3_instruction_layout():
    ix = nft_mod.create_metadata_accounts_v3_ix(
        mint=MINT, mint_authority=MINT, payer=MINT, update_authority=MINT,
        name="OneNess Bot", symbol="ONN", uri="https://files.catbox.moe/x.json",
        seller_fee_bps=500, creators=[(MINT, True, 100)],
    )
    assert bytes(ix.program_id) == bytes(METAPLEX)
    # accounts: metadata(mut), mint, mint_authority(sig), payer(sig,mut),
    # update_authority(sig), system program
    assert [bytes(m.pubkey) for m in ix.accounts][0] == bytes(meta_mod.metadata_pda(MINT))
    assert [m.is_signer for m in ix.accounts] == [False, False, True, True, True, False]
    assert [m.is_writable for m in ix.accounts] == [True, False, False, True, False, False]

    r = Reader(ix.data)
    assert r.u8() == 33                       # CreateMetadataAccountV3 tag
    assert r.take(r.u32()).decode("utf-8") == "OneNess Bot"
    assert r.take(r.u32()).decode("utf-8") == "ONN"
    assert r.take(r.u32()).decode("utf-8") == "https://files.catbox.moe/x.json"
    assert r.u16() == 500                     # seller_fee_basis_points
    assert r.u8() == 1 and r.u32() == 1       # creators Option = Some, len 1
    assert r.raw(32) == bytes(MINT)           # creator address
    assert r.u8() == 1                        # verified
    assert r.u8() == 100                      # share
    assert r.u8() == 0                        # collections: none
    assert r.u8() == 0                        # uses: none
    assert r.u8() == 1                        # is_mutable
    assert r.u8() == 0                        # collection_details: none
    assert r.remaining() == 0


def test_metadata_v3_without_creators():
    ix = nft_mod.create_metadata_accounts_v3_ix(
        mint=MINT, mint_authority=MINT, payer=MINT, update_authority=MINT,
        name="X", symbol="", uri="", seller_fee_bps=0,
    )
    r = Reader(ix.data)
    assert r.u8() == 33
    assert r.take(r.u32()).decode("utf-8") == "X"
    assert r.take(r.u32()).decode("utf-8") == ""
    assert r.take(r.u32()).decode("utf-8") == ""
    assert r.u16() == 0
    assert r.u8() == 0                        # creators Option = None


def test_borsh_str_truncates_to_abi_limit():
    raw = nft_mod._borsh_str("A" * 64, 32)
    assert struct.unpack("<I", raw[:4])[0] == 32


def test_system_prompt_injects_description_next_to_you_are():
    nft = {
        "name": "Void Walker", "symbol": "VOID", "mint": str(MINT),
        "description": "A ghost who walks the chain in neon boots.",
        "attributes": [{"trait_type": "Aura", "value": "Neon"}],
        "image": "https://files.catbox.moe/void.png",
    }
    prompt = nft_mod.build_nft_system_prompt(nft, token_id="7")
    assert prompt.startswith("You are A ghost who walks the chain in neon boots.")
    assert "Token ID / edition #: 7" in prompt
    assert "Aura: Neon" in prompt
    assert str(MINT) in prompt
    assert "files.catbox.moe/void.png" in prompt


def test_system_prompt_falls_back_to_name():
    prompt = nft_mod.build_nft_system_prompt({"name": "Void Walker", "mint": str(MINT)})
    assert prompt.startswith("You are Void Walker")


def test_metadata_json_shape():
    raw = nft_mod.build_metadata_json(
        "Sol Mint", "SMINT",
        "A 1-of-1 NFT minted on Solana devnet by the sol-mint bot",
        "https://files.catbox.moe/dgt4nk.png",
        "https://files.catbox.moe/dgt4nk.png",
        [{"trait_type": "Background", "value": "Violet-Cyan Gradient"}],
        seller_fee_bps=500,
    )
    obj = json.loads(raw)
    assert obj["name"] == "Sol Mint" and obj["symbol"] == "SMINT"
    assert obj["image"] == "https://files.catbox.moe/dgt4nk.png"
    assert obj["attributes"][0]["trait_type"] == "Background"
    assert obj["seller_fee_basis_points"] == 500


def test_decode_data_image_roundtrip_and_rejects():
    raw = b"\x89PNG-fake-bytes"
    mime, out = nft_mod.decode_data_image(
        "data:image/png;base64," + base64.b64encode(raw).decode()
    )
    assert mime == "image/png" and out == raw
    with pytest.raises(nft_mod.MintError):
        nft_mod.decode_data_image("data:text/html;base64," + base64.b64encode(b"<b>").decode())
    with pytest.raises(nft_mod.MintError):
        nft_mod.decode_data_image("not-a-data-url")


def test_resolve_uri_gateways():
    assert nft_mod.resolve_uri("ipfs://Qm123") == "https://ipfs.io/ipfs/Qm123"
    assert nft_mod.resolve_uri("ipfs://ipfs/Qm123") == "https://ipfs.io/ipfs/Qm123"
    assert nft_mod.resolve_uri("ar://abc") == "https://arweave.net/abc"
    assert nft_mod.resolve_uri("https://files.catbox.moe/x.png") == "https://files.catbox.moe/x.png"
    assert nft_mod.resolve_uri("") is None
    assert nft_mod.resolve_uri("ftp://x") is None

# @@T2@@
def _m(mid, prompt, completion, name=None, ctx=8192):
    return {
        "id": mid, "name": name or mid, "context_length": ctx,
        "pricing": {"prompt": prompt, "completion": completion},
    }


def test_filter_free_keeps_only_zero_priced_models():
    catalog = [
        _m("a/free", "0", "0"),
        _m("b/paid", "0.000002", "0.00001"),
        _m("c/semi", "0", "0.00001"),
        _m("d/broken", None, None),                        # no pricing
        _m("c/free-dup", "0.0", "0.0", name="Zed"),        # id "c/…" kept? different id
    ]
    out = ai_mod.filter_free(catalog)
    ids = [m["id"] for m in out]
    # free only, sorted by display name: "a/free" then "Zed"
    assert ids == ["a/free", "c/free-dup"]
    assert out[0]["context_length"] == 8192


def test_resolve_model_validates_against_free_catalog():
    models = [
        {"id": "x/other-free", "name": "X", "context_length": 4096},
        {"id": ai_mod.DEFAULT_FREE_MODEL, "name": "Llama", "context_length": 100},
    ]
    assert ai_mod.resolve_model_from(models, "x/other-free") == "x/other-free"
    assert ai_mod.resolve_model_from(models, "") == ai_mod.DEFAULT_FREE_MODEL
    # no catalog at all + no request → error rather than a silent paid pick
    with pytest.raises(ai_mod.AIError):
        ai_mod.resolve_model_from([], "")
    with pytest.raises(ai_mod.AIError):
        ai_mod.resolve_model_from(models, "openai/gpt-6.1-sol-pro")  # not free


def test_is_free_handles_odd_shapes():
    assert ai_mod.is_free({"id": "m", "pricing": {"prompt": "0", "completion": "0"}})
    assert not ai_mod.is_free({"id": "m"})
    assert not ai_mod.is_free({"id": "m", "pricing": {"prompt": "abc", "completion": "0"}})


def test_resolve_model_prefers_chat_models_over_code_variants():
    models = [
        {"id": "zz/north-mini-code:free", "name": "Z", "context_length": 8192},
        {"id": "x/llama-instruct:free", "name": "A", "context_length": 8192},
    ]
    # free catalog contains no hardcoded default → prefer an instruct-ish model
    assert ai_mod.resolve_model_from(models, "") == "x/llama-instruct:free"
    # fallback chain: instruct ids first, capped, never repeating the chosen one
    assert ai_mod.fallback_candidates(models, "zz/north-mini-code:free") == ["x/llama-instruct:free"]
    assert ai_mod.fallback_candidates(models[:1], "zz/north-mini-code:free") == []


def test_chat_fallback_retries_through_transient_429(monkeypatch):
    calls = []

    def fake_chat(key, url, model, msgs, **kw):
        calls.append(model)
        if model == "zz/north-mini-code:free":
            raise ai_mod.AIError("OpenRouter rate limit reached — try again shortly")
        return {"content": "hi from " + model, "model": model, "usage": {}}

    monkeypatch.setattr(ai_mod, "chat", fake_chat)
    out = ai_mod.chat_with_fallback(
        "k", "u", "zz/north-mini-code:free", [{"role": "user", "content": "x"}],
        ["google/gemma-4-31b-it:free"],
    )
    assert out["content"] == "hi from google/gemma-4-31b-it:free"
    assert calls == ["zz/north-mini-code:free", "google/gemma-4-31b-it:free"]


def test_chat_fallback_aborts_on_auth_errors(monkeypatch):
    def fake_chat(key, url, model, msgs, **kw):
        raise ai_mod.AIError("OpenRouter rejected the API key — check OPENROUTER_API_KEY")

    monkeypatch.setattr(ai_mod, "chat", fake_chat)
    with pytest.raises(ai_mod.AIError):
        ai_mod.chat_with_fallback("bad", "u", "x/llama-instruct:free", [], ["y/gem"])


# ---- nft_bundle validation (fake rpc, no network) -------------------------------

class _FakeRPC:
    def __init__(self, accounts):
        self.accounts = accounts

    def get_account_info(self, pubkey, commitment="confirmed"):
        return self.accounts.get(pubkey)


def test_nft_bundle_rejects_missing_mint():
    with pytest.raises(nft_mod.MintError):
        nft_mod.nft_bundle(_FakeRPC({}), str(MINT))


def test_nft_bundle_rejects_non_token_mint():
    # an existing account owned by the SYSTEM program → not an SPL mint
    fake = _FakeRPC({
        str(MINT): {
            "owner": "11111111111111111111111111111111",
            "data": b"\x00" * 45,
            "lamports": 1,
        },
    })
    with pytest.raises(nft_mod.MintError):
        nft_mod.nft_bundle(fake, str(MINT))


def test_nft_bundle_rejects_zero_supply():
    from app.solana.spl import TOKEN_PROGRAM_ID

    mint_data = bytearray(82)
    struct.pack_into("<Q", mint_data, 36, 0)   # supply 0
    mint_data[44] = 9                          # decimals
    fake = _FakeRPC({
        str(MINT): {"owner": str(TOKEN_PROGRAM_ID), "data": bytes(mint_data), "lamports": 1},
    })
    with pytest.raises(nft_mod.MintError):
        nft_mod.nft_bundle(fake, str(MINT))


def test_nft_bundle_flags_1_of_1(monkeypatch):
    from app.solana.spl import TOKEN_PROGRAM_ID

    monkeypatch.setattr(meta_mod, "search_tokens", lambda *a, **k: [])

    mint_data = bytearray(82)
    struct.pack_into("<Q", mint_data, 36, 1)   # supply 1
    mint_data[44] = 0                          # decimals 0 → true 1/1 NFT
    fake = _FakeRPC({
        str(MINT): {"owner": str(TOKEN_PROGRAM_ID), "data": bytes(mint_data), "lamports": 1},
    })
    bundle = nft_mod.nft_bundle(fake, str(MINT))
    assert bundle["is_nft"] is True and bundle["supply"] == 1 and bundle["decimals"] == 0


# ---- mint_nft fee plumbing (faked tx module + fake rpc; no network) --------------

from solders.keypair import Keypair  # noqa: E402

from app.solana import tx as tx_mod  # noqa: E402
from app.solana.spl import SYSTEM_PROGRAM_ID  # noqa: E402

FEE_PK = Keypair().pubkey()
NO_FEE_NEEDED = 260_580 + 890_880 + 560_000 + 15_000  # fake rents + sig headroom


class _MintFakeRPC:
    def __init__(self, balance_lamports):
        self.balance = balance_lamports

    def get_rent_lamports(self, space, commitment="confirmed"):
        if space == 165:
            return 890_880
        return 560_000 if space >= 300 else 260_580

    def get_balance(self, pubkey, commitment="confirmed"):
        return self.balance

    def send_transaction(self, tx_bytes):
        return "sigfake-raw"


def _mint_with_fee(monkeypatch, fee_lamports, balance):
    captured = {}

    def fake_build_and_sign(rpc, instructions, payer, extra_signers=None):
        captured["instructions"] = list(instructions)
        return b"tx-bytes"  # bytes() is taken on the tx in the confirm=False path

    def fake_send_and_confirm(rpc, tx):
        captured["confirmed"] = True
        return "sigfake"

    monkeypatch.setattr(tx_mod, "build_and_sign", fake_build_and_sign)
    monkeypatch.setattr(tx_mod, "send_and_confirm", fake_send_and_confirm)
    owner = Keypair()
    result = nft_mod.mint_nft(
        _MintFakeRPC(balance), owner, name="x", symbol="", uri="",
        seller_fee_bps=0, confirm=False,
        fee_wallet=str(FEE_PK) if fee_lamports else None,
        fee_lamports=fee_lamports,
    )
    return result, captured, owner



# ---- upload failover chain (catbox → pinata → 0x0.st → litterbox) ------------

class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def test_upload_failover_catbox_down_zerox(monkeypatch):
    """catbox unreachable → falls through to 0x0.st and returns its URL."""
    calls = []

    def fake_post(url, files=None, headers=None, timeout=None):
        calls.append(url)
        if url == nft_mod.CATBOX_UPLOAD_URL:
            raise requests.RequestException("connect timeout")
        return _Resp("https://0x0.st/abc123")

    monkeypatch.setattr(nft_mod.requests, "post", fake_post)
    for var in ("PINATA_JWT", "PINATA_API_KEY", "PINATA_SECRET_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert nft_mod.upload_catbox(b"x", "a.png", "image/png") == "https://0x0.st/abc123"
    assert calls == [nft_mod.CATBOX_UPLOAD_URL, nft_mod.ZEROD_UPLOAD_URL]


def test_upload_failover_litterbox_last_resort(monkeypatch):
    """catbox AND 0x0.st down → litterbox 72h leg still saves the mint."""
    calls = []

    def fake_post(url, files=None, headers=None, timeout=None):
        calls.append(url)
        if url == nft_mod.LITTERBOX_UPLOAD_URL:
            return _Resp("https://litter.catbox.moe/xyz.png")
        if url == nft_mod.ZEROD_UPLOAD_URL:
            return _Resp("500 server hiccup", status_code=500)
        raise requests.RequestException("unreachable")

    monkeypatch.setattr(nft_mod.requests, "post", fake_post)
    for var in ("PINATA_JWT", "PINATA_API_KEY", "PINATA_SECRET_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    got = nft_mod.upload_catbox(b"x", "a.png", "image/png")
    assert got == "https://litter.catbox.moe/xyz.png"
    assert calls == [nft_mod.CATBOX_UPLOAD_URL, nft_mod.ZEROD_UPLOAD_URL,
                     nft_mod.LITTERBOX_UPLOAD_URL]


def test_upload_failover_all_fail(monkeypatch):
    """Every host down → UploadError, and every host in the chain was tried."""
    calls = []

    def fake_post(url, files=None, headers=None, timeout=None):
        calls.append(url)
        raise requests.RequestException("no route to host")

    monkeypatch.setattr(nft_mod.requests, "post", fake_post)
    for var in ("PINATA_JWT", "PINATA_API_KEY", "PINATA_SECRET_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    expected = [e[0] for e in nft_mod._upload_attempts(b"x", "a.png", "image/png")]
    with pytest.raises(nft_mod.UploadError) as ei:
        nft_mod.upload_catbox(b"x", "a.png", "image/png")
    assert calls == expected
    assert expected[0] == nft_mod.CATBOX_UPLOAD_URL
    assert expected[-1] == nft_mod.LITTERBOX_UPLOAD_URL
    assert str(ei.value).startswith("image/metadata upload failed")


def test_upload_failover_pinata_jwt(monkeypatch):
    """With PINATA_JWT set, pinata sits between catbox and 0x0.st."""
    calls, hdrs = [], {}

    def fake_post(url, files=None, headers=None, timeout=None):
        calls.append(url)
        if url == nft_mod.PINATA_UPLOAD_URL:
            hdrs.update(headers or {})
            return _Resp(json.dumps({"IpfsHash": "bafyabc"}))
        raise requests.RequestException("down")

    monkeypatch.setattr(nft_mod.requests, "post", fake_post)
    monkeypatch.setenv("PINATA_JWT", "eyJ-test")
    got = nft_mod.upload_catbox(b"x", "m.json", "application/json")
    assert got == nft_mod.GATEWAY_PINATA + "bafyabc"
    assert calls == [nft_mod.CATBOX_UPLOAD_URL, nft_mod.PINATA_UPLOAD_URL]
    assert hdrs["Authorization"] == "Bearer eyJ-test"


def test_upload_failover_pinata_keypair(monkeypatch):
    """Classic key + secret pair also activates the pinata leg (v1 headers)."""
    hdrs = {}

    def fake_post(url, files=None, headers=None, timeout=None):
        if url == nft_mod.PINATA_UPLOAD_URL:
            hdrs.update(headers or {})
            return _Resp(json.dumps({"IpfsHash": "bafykey"}))
        raise requests.RequestException("down")

    monkeypatch.setattr(nft_mod.requests, "post", fake_post)
    monkeypatch.delenv("PINATA_JWT", raising=False)
    monkeypatch.setenv("PINATA_API_KEY", "k123")
    monkeypatch.setenv("PINATA_SECRET_API_KEY", "s789")
    assert nft_mod.upload_catbox(b"x", "m.json", "application/json") \
        == nft_mod.GATEWAY_PINATA + "bafykey"
    assert hdrs["pinata_api_key"] == "k123"
    assert hdrs["pinata_secret_api_key"] == "s789"


def test_upload_rejects_bad_input(monkeypatch):
    """Empty / oversize payloads refuse to even touch the network."""
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not reach any host")

    monkeypatch.setattr(nft_mod.requests, "post", boom)
    with pytest.raises(nft_mod.UploadError):
        nft_mod.upload_catbox(b"", "a.png", "image/png")
    with pytest.raises(nft_mod.UploadError):
        nft_mod.upload_catbox(
            b"x" * (nft_mod.MAX_UPLOAD_BYTES + 1), "a.png", "image/png")
def test_mint_fee_transfer_is_appended_and_reported(monkeypatch):
    result, captured, owner = _mint_with_fee(monkeypatch, 50_000_000, 10**9)
    ixs = captured["instructions"]
    assert len(ixs) == 6
    fee_ix = ixs[-1]
    assert bytes(fee_ix.program_id) == bytes(SYSTEM_PROGRAM_ID)
    assert fee_ix.data == struct.pack("<I", 2) + struct.pack("<Q", 50_000_000)
    assert [bytes(m.pubkey) for m in fee_ix.accounts] == [
        bytes(owner.pubkey()), bytes(FEE_PK),
    ]
    assert result["fee_raw"] == 50_000_000
    assert result["fee_wallet"] == str(FEE_PK)
    assert result["signature"] == "sigfake-raw"


def test_mint_without_fee_keeps_five_instructions(monkeypatch):
    result, captured, _ = _mint_with_fee(monkeypatch, 0, 10**9)
    assert len(captured["instructions"]) == 5
    assert "fee_raw" not in result


def test_mint_balance_guard_includes_the_fee(monkeypatch):
    result, _, _ = _mint_with_fee(monkeypatch, 0, NO_FEE_NEEDED)
    assert result["mint"]
    with pytest.raises(nft_mod.MintError, match="mint fee"):
        _mint_with_fee(monkeypatch, 50_000_000, NO_FEE_NEEDED)


# ---- the REAL compile path: build_and_sign is NOT mocked here ----------------

from solders.hash import Hash  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from app.solana.spl import (  # noqa: E402
    MINT_ACCOUNT_SPACE,
    TOKEN_PROGRAM_ID,
    create_account_ix,
    create_ata_idempotent_ix,
    derive_ata,
    initialize_mint_ix,
    mint_to_ix,
    system_transfer_ix,
)

GENESIS_BLOCKHASH = "11111111111111111111111111111111"  # 32 zero bytes, valid Hash


def _mint_instruction_set(owner, mint, ata, fee_pub):
    """Exactly the instruction list mint_nft assembles (including the fee ix)."""
    return [
        create_account_ix(owner, mint, 260_580, MINT_ACCOUNT_SPACE, TOKEN_PROGRAM_ID),
        initialize_mint_ix(mint, 0, owner, owner),
        create_ata_idempotent_ix(owner, owner, mint)[0],
        mint_to_ix(mint, ata, owner, 1),
        nft_mod.create_metadata_accounts_v3_ix(
            mint=mint, mint_authority=owner, payer=owner, update_authority=owner,
            name="OneNess Debug Mint", symbol="DBG",
            uri="https://files.catbox.moe/x.json",
            seller_fee_bps=500, creators=[(owner, True, 100)], is_mutable=True,
        ),
        system_transfer_ix(owner, fee_pub, 50_000_000),
    ]


def test_mint_instruction_set_compiles_and_signs_with_real_solders():
    """Real solders compile + sign of the bot's tx — no build_and_sign mock.

    Regression: CreateAccount did not flag the new mint account as a signer,
    so every live mint died at `Transaction.sign` with
    `solders.SignerError: keypair-pubkey mismatch` (uncaught → HTTP 500) —
    invisible to the mocked-RPC unit tests above.
    """
    owner, mint_kp = Keypair(), Keypair()
    mint = mint_kp.pubkey()
    ata = derive_ata(owner.pubkey(), mint)
    fee_pub = Keypair().pubkey()
    ixs = _mint_instruction_set(owner.pubkey(), mint, ata, fee_pub)
    blockhash = Hash.from_string(GENESIS_BLOCKHASH)
    msg = Message.new_with_blockhash(ixs, owner.pubkey(), blockhash)
    assert msg.header.num_required_signatures == 2   # payer + the mint account
    tx = Transaction.new_unsigned(msg)
    tx.sign([owner, mint_kp], blockhash)             # SignerError on any mismatch
    assert bytes(tx.signatures[0]) and bytes(tx.signatures[1])
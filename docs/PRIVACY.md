# Privacy: Public or Anonymous, chosen on every transaction

Protocol string: `oneness-tor/privacy/1`. Machine-readable copy:
`GET /api/privacy` (public, no session).

## The rule

Before every send the wallet asks one question.

| answer | what the chain record shows | when |
| --- | --- | --- |
| **Public** (default) | the sending wallet's address | from the first confirmation |
| **Anonymous** | `Tor-Anonymous-onion` (`privacy.anonymous_label`) | from the first confirmation |

There is no time component anywhere: the old 30-day public window is gone. The
ledger keeps every record forever, serves every record publicly, and seals
nothing into a private archive. Nothing on this node expires on a timer.

## Where it is written

The flag rides in the transaction's `external_ref`:

```text
public      -> external_ref: ""
anonymous   -> external_ref: "oneness-privacy:1|mode=anonymous"
```

`rust/torchain/src/chain.rs` already copies `external_ref` from the signed
transaction into the `TxRecord` it publishes (`tx_transfer` → `make_record`), so:

* **no chain rewrite**: no new transaction kind, no new struct field, no change
  to the state root, the block hash or the txid.
* **backwards compatible**: a public send writes `""`, exactly like every record
  mined before this protocol existed, so legacy records read back as `public`.
* **self-service only**: `external_ref` is not in the canonical signing message
  (`oneness-tor:v1|kind|from|to|amount|token|note|nonce|timestamp`), and only the
  wallet that signs a transaction can set it — it is its own address it hides.
  Nothing in this design lets anyone hide somebody else's address.

## Where it is enforced

One function, `oneness_tor.services.privacy.public_view`, masks a record for
every public projection:

| path | anonymous record (no credential) |
| --- | --- |
| `GET /api/public/records` | `from` → `Tor-Anonymous-onion` |
| `GET /api/chain/events` | `from` → `Tor-Anonymous-onion` |
| `GET /api/chain/tx/<txid>` | `from` → `Tor-Anonymous-onion` |
| `GET /api/chain/tx/<txid>` + `x-admin-token` | the real address (operator view) |
| `POST /api/private/records` (admin) | the real address |
| `GET /api/wallets/<name>/history` (wallet session) | the real address **plus** `on_chain_from`, so the owner sees what the public sees |

Each masked row carries `privacy` (`public`/`anonymous`), `anonymous` and
`masked_fields`. The transfer's 0.1 TOR platform fee is a second signed transfer
and inherits the same mode — a public fee would re-publish the sender the wallet
just hid.

## What it hides, and what it does not

Hidden: the sending address — and **only** the sending address. The receiver is
never masked: a payment has to name the wallet that receives it, that wallet
belongs to somebody else, and its owner never chose to be hidden. There is no
option to anonymise a receiver, and no config key that pretends otherwise.

Shown: the receiving address, amount, token, fee, note, timestamp, height, block,
txid, signature — and the balance sheet, because balances on this chain are public
and permanent by design (`/api/public/balances`, `/api/public/ledger`,
`/api/public/totals`).

That last line is the honest limit: this is **address masking on the public
record**, not untraceability. A patient observer who watches balances move block
by block can correlate a transfer, the ledger must know the signer to debit it,
and the node operator can read the record. Cryptographic sender anonymity (ring
signatures, shielded pools) would be `oneness-tor/privacy/2` and is not
implemented.

One more limit worth writing down: the Rust node's own HTTP port serves
`/api/public/records` **unmasked**. This repository always starts it on loopback
(`torchain serve --bind 127.0.0.1:8545`, see `core.ledger_host.bind_address`), so
the Python API is the only public face. An operator who publishes that port
publishes every address, anonymous or not.

If a future node writes a native `privacy` field on the record,
`privacy.mode_of()` reads it first and the `external_ref` carrier becomes a
fallback — that is the upgrade path to `oneness-tor/privacy/2`.

## Using it

```powershell
# the wallet page asks on every send: /wallet -> send -> privacy
curl -X POST -H "content-type: application/json" -H "x-wallet-session: <session>" `
     -d '{"to":"TOR1…onion","amount":1.5,"privacy":"anonymous"}' `
     http://127.0.0.1:8090/api/wallets/founder/send

# sign only: the mode travels inside signed_tx, so posting it later keeps it
curl -X POST ... -d '{"to":"TOR1…onion","amount":1.5,"privacy":"anonymous"}' `
     http://127.0.0.1:8090/api/wallets/founder/sign
```

Accepted spellings: `public`/`open`/`named`/`visible`, and
`anonymous`/`anon`/`private`/`hidden`/`onion`/`Tor-Anonymous-onion`.
Anything else is a `400 VALIDATION_ERROR` **before** a signature is produced — a
typo can never publish an address the sender believed was hidden.

## Configuration

```yaml
privacy:
  default_mode: public                     # ONENESS_PRIVACY_DEFAULT
  anonymous_label: Tor-Anonymous-onion     # ONENESS_PRIVACY_LABEL
```

`default_mode` applies only when an API client sends no `privacy` field; the
wallet page always sends the answer the visitor clicked. A misspelt config value
falls back to `public` rather than silently hiding every transfer.

## Tests

`tests/test_privacy_protocol.py` pins the whole path: normalisation and the
typo rejection, the `external_ref` round trip and legacy-record default, the
masking (sender only — the receiver is asserted to stay), the send and sign-only
endpoints, the three masked public read paths versus the admin token, the owner's
history labelling, and the published document (`/api/privacy`).

`tests/test_data_policy.py` and `tools/data_policy.py` are gone: they tested the
30-day window and its sweep, and neither exists any more. What replaced them is
`tests/test_privacy_protocol.py` plus the assertion in `tests/test_coin_metadata.py`
that the coin document no longer advertises a public-data window.

## Measured on the live chain (2026-11-07T00:33Z)

`tools/live_privacy_proof.py` signs one public and one anonymous transfer with
`data/wallets/founder.json`, submits both to the public origin and then reads them
back through all three masked projections. Run result: **PASS**, every check.

```text
node      https://onenesstorchain.onrender.com     height 938 -> 940
public    87c61a7984839d25d1228585d3ac68d099850dc2ccef242cf76e593c74d3662c  height 939
          from = TOR12xd7zywc3iuwcls3fqzsozkgo5ahv3gnonion   external_ref = ""
anonymous e0460afcc5348f22dd93518cec38f1247af7161399f6c1ec7f92d2d649478ae0  height 940
          from = Tor-Anonymous-onion    external_ref = "oneness-privacy:1|mode=anonymous"
          privacy = anonymous, masked_fields = ["from"]
```

Both rows are still served by `GET /api/public/records` and
`GET /api/chain/events` (the anonymous one masked, the public one not), so the
mask is not a UI trick on one endpoint. The receiver
(`TOR1zas7pwn5t7ravd3m4ez63e43ztoivq7conion`, made by the tool) is visible on both
records, exactly as the rule says it must be.

Re-run it any time (it creates the recipient wallet once, then reuses it):

```powershell
cd 1nessTORChain-main
python tools/live_privacy_proof.py --amount 0.50        # live origin
python tools/live_privacy_proof.py --dry-run            # sign two transfers, send nothing
```

The JSON report (txids, heights, every read-back row, every check) lands in
`logs/privacy-proof-<utc>.json`.

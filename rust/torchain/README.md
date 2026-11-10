# torchain — OneNess TOR native chain manager (Rust)

The **authoritative ledger** for the OneNess TOR chain. Python (`core/`,
`oneness_tor/`) is the API/AI layer; this binary owns accounts, balances, the
permanent transaction record, wallet generation, transfers, deposits,
withdrawals, token creation, AMM swaps and minting. Records never expire and the
ledger seals nothing: the sending wallet chooses **public** or **anonymous** for
every transaction, and that choice rides in the record's `external_ref`
(`oneness-tor/privacy/1`) for the Python API to honour on every public read.

Native coin: `TOR111111111111111111111111111111onion` · 8 decimals (microTOR).
Genesis mints **50,000,000 TOR** into the founder Ed25519 wallet at block 0.
Coin metadata (name `OneNess TOR`, symbol `TOR`, decimals, logo) is served by
`GET /api/coin` / `torchain coin` — see [Coin metadata & logo](#coin-metadata--logo).

## Build & run

```powershell
cd rust/torchain
cargo build --release            # binary: target/release/torchain.exe
.\target\release\torchain.exe init --founder ..\..\data\wallets\founder.json
.\target\release\torchain.exe serve --bind 127.0.0.1:8545
```

`init` prints the genesis hash, the founder/fee-receiver address and the one-time
**admin token** (only `sha3-256(token)` is stored — save the token, it unlocks
admin-only routes).

Data lives in `data/chain/` by default (`--dir` overrides):

| file | meaning |
| --- | --- |
| `state.json` | checkpoint: accounts, tokens, pools, records |
| `blocks.jsonl` | append-only blocks, each line `{block, txs}` (permanent) |

## Wallets — Phantom compatible

* Ed25519, BIP39 mnemonic, SLIP-0010 derivation `m/44'/501'/0'/0'`
  (the same path Phantom uses for Solana accounts).
* Secret key = `seed || pubkey` (64 bytes) base58 — exactly what Phantom imports.
* Address = `TOR1` + `base32(sha3-256(pubkey)[0..20])` + `onion`, so the address
  stays in the chain's own format while every transfer is Ed25519-verifiable.

```powershell
torchain wallet new --out ..\..\data\wallets\user2.json   # address/secret/mnemonic
torchain wallet from-mnemonic --mnemonic "word word ..."   # deterministic restore
torchain address --pubkey 5QoZ...                          # pubkey -> TOR1…onion
```

## Signing format (shared with the Python client)

The node verifies these exact bytes:

```
oneness-tor:v1|<kind>|<from>|<to>|<amount>|<token or TOR>|<note>|<nonce>|<timestamp>
```

* `from` = signer's `TOR1…onion`; the node rebuilds it from `pubkey_base58` and
  rejects the transaction if they disagree (`ADDRESS_MISMATCH`).
* `amount` = microTOR for the native coin, whole units for a token.
* `nonce` must equal the account's current nonce; it increments per accepted tx.
* `timestamp` must be within ±900 s of node time.
* Signature = Ed25519 over those bytes, base58 encoded.

`tools/torchain_signer.py` builds and submits this from Python.

## Commands

```
init --founder <wallet.json> [--supply 50000000] [--admin-token X]
wallet new|from-mnemonic|show        address --pubkey|--wallet
balance --address|--wallet
transfer --key <w> --to <addr> --amount 1.5 [--token SYM]
deposit  --key <founder> --to <addr> --amount 10 [--ref ext-id]
withdraw --key <founder> --to <addr> --amount 2.5 [--ref ext-id]
token-create --key <w> --token MEME --amount 1000000 [--note "name"]
pool-create  --key <w> --token MEME --amount 500000 --tor 100
swap --key <w> --token MEME --amount 5 --direction buy|sell
mint --key <founder> --token MEME --amount 1000
public [--limit N]        private --admin-token X
verify                    serve [--bind 127.0.0.1:8545]  pricing
coin                      TOR coin metadata as JSON (name, symbol, logo)
```

Fees come from the locked table (mirrored from `core/pricing.py`): transfer
0.1 TOR, token create 5 TOR, pool launch 10 TOR, mint 3 TOR — all paid to the fee
receiver (founder by default) and written to the ledger as `fee` records.

## Supply & pools

* Genesis: `total_supply = 50,000,000 TOR`, all in the founder account.
* `deposit` credits a user and raises `total_supply`; `withdraw` debits a user and
  lowers it (external bridge semantics, founder-signed only).
* Fees move TOR between accounts — supply unchanged.
* Each pool owns a deterministic account (`pool_address(symbol)`) that holds the
  TOR and token reserves, so `sum(balances) == total_supply` always holds and
  `verify` can prove it. Swaps are constant-product with a 0.30% fee.

## Coin metadata & logo

`rust/torchain/src/coin.rs` is the only place the coin's identity lives; the
Python twin is `core/coin.py` and `tests/test_coin_metadata.py` fails if the two
disagree.

| field | value |
| --- | --- |
| name | `OneNess TOR` |
| symbol | `TOR` |
| decimals | `8` (1 TOR = 100,000,000 microTOR) |
| native coin id | `TOR111111111111111111111111111111onion` |
| `image` / `logo_url` | `https://cyan-deep-magpie-676.mypinata.cloud/ipfs/bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze?pinataGatewayToken=…` |
| `logo_ipfs` | `ipfs://bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze` |
| logo CID (pinned) | `bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze` |

* `torchain coin` prints the document; `GET /api/coin` serves the same JSON, and
  `/api/chain/info` + `/api/pricing` embed the compact `coin` block — pages,
  wallets and the AI read it from the node instead of hardcoding a URL.
* The document keeps the keys wallets and marketplaces read (`name`, `symbol`,
  `description`, `image`), so it can be pinned to IPFS as the coin metadata as-is.
* The pinned file is the repo's `TOR Coin Logo.png` (2,165,013 bytes, `image/png`)
  and the gateway answers `200 OK` with `access-control-allow-origin: *`, so it
  drops straight into a page:

  ```html
  <img src="https://cyan-deep-magpie-676.mypinata.cloud/ipfs/bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze?pinataGatewayToken=fvfdMA6s8XyRJ3C2Lp6_ScBrMLuuvUoo5tKWCGpXCkPdYVsm6mW_TWXlXE9PYi7A"
       alt="TOR Coin logo" width="96" height="96">
  ```
* Point at another gateway or a re-pinned file with `TOR_LOGO_URL=https://…` /
  `TOR_LOGO_IPFS=ipfs://…` (both the Rust node and `core/coin.py` honour them).

## Privacy: chosen per transaction, not by the calendar

There is no time window and no archive to seal into. A record is written once and
stays readable: `public_records` returns every record, newest first, with nothing
filtered by age.

The privacy choice belongs to the sending wallet, per transaction:

* **public** — the send writes an empty `external_ref`, so the sender's address is
  on the record and on every explorer page.
* **anonymous** — the send writes `oneness-privacy:1|mode=anonymous` in
  `external_ref`; the Python API shows `Tor-Anonymous-onion` instead of the
  sending address on every public projection (`/api/public/records`,
  `/api/chain/events`, `/api/chain/tx/{txid}`).

`external_ref` is not part of the signed message, so this needs no new struct
field, no state-root change and no block-hash change. A node reads legacy records
(empty ref) as public. What stays visible either way: the amount, the fee, the
token, the timestamp, the block and the whole public balance sheet — address
masking is not untraceability. See `docs/PRIVACY.md`.

## HTTP API (envelope `{ok, data, error}`)

| method | path | purpose |
| --- | --- | --- |
| GET | `/health` | liveness + height |
| GET | `/api/chain/info` | chain id, height, head/genesis hash, supply, founder, fee receiver, `coin` |
| GET | `/api/coin` | TOR coin metadata: name, symbol, decimals, description, `image`/`logo_url`/`logo_ipfs` |
| GET | `/api/chain/block/{height\|hash}` | one block |
| GET | `/api/chain/blocks?from=&to=` | block range (peer catch-up) |
| GET | `/api/chain/tx/{txid}` | one record |
| GET | `/api/chain/pending` | empty: single-node PoA confirms synchronously |
| GET | `/api/chain/events` | every record, newest first |
| POST | `/api/chain/tx` | submit a signed transaction |
| GET | `/api/account/{address}` | balance, tokens, nonce, role |
| GET | `/api/public/records?limit=` | every record, newest first |
| POST | `/api/private/records` | records an older release sealed (`x-admin-token`) |
| GET | `/api/pricing`, `/api/tokens`, `/api/pools` | reference data |

Error codes map to HTTP: `VALIDATION_ERROR`/`INVALID_SIGNATURE`/
`ADDRESS_MISMATCH` → 422, `UNAUTHORIZED` → 403, `INSUFFICIENT_FUNDS` → 402,
`NONCE_MISMATCH`/`CONFLICT` → 409, `NOT_FOUND`/`UNKNOWN_TOKEN`/
`POOL_NOT_FOUND` → 404.

## Verify a chain

`torchain verify` re-hashes every block (prev-hash chain + block digest), checks
the checkpoint state root against the newest block, and asserts
`sum(balances) == total_supply`. Exit code 1 with `errors[]` if anything is off.

## Honest limits

* Single authorized proposer (PoA set of one) — peer sync, gossip and multi-node
  consensus are the next step; the block/range endpoints already exist for it.
* WASM smart contracts are not executed here; Rust contracts run in the Python
  layer's sandbox (`wasmtime`) and call this ledger for balances.
* The wallet JSON holds the secret in plaintext (0600 attempted). Move founder
  keys offline; the Python keystore encrypts a copy for the UI.
* `blocks.jsonl` is append-only but not fsynced per write; back up
  `data/chain/` and `data/wallets/founder.json`.


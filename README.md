
<img width="2172" height="724" alt="1Ness TOR Blockchain banner" src="https://github.com/user-attachments/assets/97ed63ed-4217-438c-85b1-f13eb3514a9b" />

# OneNess TOR — node, API, command brain and console

A complete TOR-chain node: a **Rust ledger** (`rust/torchain`) that owns every
balance, a **Python API** that exposes every endpoint exactly once, an **Ollama
brain** that discovers those endpoints and calls them for real, and **thirteen
pages** (dashboard, explorer, node admin, wallet, payments, contracts,
app builders (DarkWeb + Rust), meme launcher, AI-NFT Studio, Ollama console,
docs, coin, landing).

Nothing is mocked: payments are Ed25519 transactions on the ledger, contract
sources are compiled with `rustc`, the logo is the pinned IPFS file, and the
fetch-data tool returns the node's actual HTTP response.

```
browser ──► Flask (8090) ──► torchain (8545) ──► data/chain-*/{state.json,blocks.jsonl}
                 │
                 └──► Ollama (OLLAMA_HOST) ──► fetch_oneness_data ──► back into Flask
```

## Quick start (Windows PowerShell)

```powershell
# 1. build the ledger
cd rust/torchain; cargo build --release; cd ../..

# 2. genesis: 50,000,000 TOR into the founder wallet
.\rust\torchain\target\release\torchain.exe init `
    --founder data\wallets\founder.json --dir data\chain-api `
    --admin-token oneness-dev-token

# 3. the node
.\rust\torchain\target\release\torchain.exe serve --bind 127.0.0.1:8545 --dir data\chain-api

# 4. the API + pages (second terminal) — installs/serves Ollama, then boots
$env:ONENESS_CHAIN_DIR='data/chain-api'
$env:ONENESS_NODE_API='http://127.0.0.1:8545'
$env:ONENESS_ADMIN_TOKEN='oneness-dev-token'
python scripts/start.py
```

Open <http://127.0.0.1:8090>. **Only two pages belong to the node operator**:
`/admin` (the admin account, the node's admin wallet + settings) and `/ollama` (the
runtime and the model configuration). Both sign in with the admin **username +
password** created at `/admin` and send `x-admin-session` — no browser button, no
token in localStorage. Everything else is public and works with the visitor's own
account wallet: sign up at `/wallet` (a real Ed25519 wallet is generated, the
private key is shown once) and every later login fetches that same wallet. Mints,
AI-NFTs, contract deployments, meme coins, workbench projects and invoice
payments are charged to and owned by *that* wallet. The chain admin token from
step 2 is never requested by a page: this node keeps the credential itself — a
proposer creates `data/admin/chain_token` on first boot, a replica uses the
origin's token its seeded ledger already accepts — and hands it to the ledger
before `torchain serve` starts. A signed-in admin session is then enough for
node-level actions (the fee receiver, admin wallet, replication import).
`GET /api/admin/token` says where the token comes from — never the token itself.

`scripts/run_all.ps1` does steps 3 + 4 in one go. `data/chain-api/` is a
throwaway demo chain; point `ONENESS_CHAIN_DIR` at `data/chain` for the original.

### One start command — local, Render, Docker

`python scripts/start.py` is the single entrypoint (``python -m oneness_tor`` runs
the exact same code path). It resolves the bind address (`--host/--port` →
`$PORT` → `ONENESS_API_HOST` / `ONENESS_API_PORT` → `config.yaml`; a platform that
injects `$PORT` binds `0.0.0.0`), **starts the Rust ledger it is about to read**
(see below), installs Ollama when the binary is missing
(`ollama.auto_install`; winget / `OllamaSetup.exe` on Windows, `install.sh` then the
release tarball into `~/.local/ollama` on Linux — no sudo needed), starts
`ollama serve`, waits for `/api/version`, prints what really happened, and then
serves the API on a threaded WSGI server that stops on `SIGTERM`. The default model
is pulled only when asked (`--ollama-pull` or `ONENESS_OLLAMA_PULL=1`): a 7B
download is several GB and must never surprise a deploy.

| target | how |
| --- | --- |
| local (Windows / Linux / macOS) | `python scripts/start.py` |
| Render | `render.yaml` → the Docker image, `CMD ["python", "scripts/start.py"]`, health check `/api/health` |
| Docker | `docker build -t oneness-tor .` then `docker run -p 8090:8090 oneness-tor` |

**The ledger is hosted, not assumed.** When `node.api_url` is loopback the start
command runs `torchain serve --dir <chain.data_dir> --bind 127.0.0.1:8545` as a
child process (pid in the banner, stopped on `SIGTERM`, `PR_SET_PDEATHSIG` on
Linux so a crash never leaves an orphan holding the port), waits for `GET /health`
and only then serves the API. A remote `node.api_url` is never touched;
`--no-node` or `ONENESS_NODE_AUTOSTART=0` opts out. This is why the Dockerfile
compiles `rust/torchain` and puts `torchain` on `PATH`: a service that ships only
Python still serves 153 endpoints, and every chain route in them answers
`503 CHAIN_UNAVAILABLE` because no node exists in the container. `/api/health`
now reports the decision (`node_runtime` + `node_hint`), not just a boolean.

**A fresh container needs the chain.** `data/chain` is gitignored, so an empty
container has no genesis. `python scripts/export_seed.py` freezes the ledger you
already have into `seed/chain-snapshot.json` (state + every block, no key
material); the start command re-verifies every block hash, every `prev_hash` link,
the head hash and the state root, then writes `state.json` + `blocks.jsonl` and
starts the node — height, tokens and balances are the real ones, not a re-genesis.
`ONENESS_NODE_SEED` accepts a path or an `https://` URL instead (a node you
control, `GET /api/chain/sync/snapshot`, with `ONENESS_SYNC_TOKEN`). Node
authentication stays local: the seeded `admin_token_sha256` is replaced by this
node's `ONENESS_ADMIN_TOKEN`, and that hash is not part of `state_root`.

**A native (non-Docker) Render service** only runs `pip install`, so it has no
Rust toolchain. `bin/torchain` is the committed Linux build of `rust/torchain`
(the Dockerfile rebuilds it from source, and `scripts/build_render.sh` does the
same in a build step: rustup + `cargo build --release --locked` + `pip install`),
so the start command has a real ledger to run on that host too.

That artifact is the **last** place `core.ledger_host.find_binary` looks:
`ONENESS_NODE_BINARY` → `chain.binary` (the `cargo build` output) → `PATH` → `bin/torchain`.
The order is the fix for the 6 Nov outage: the live image had just compiled a
current ledger into `/usr/local/bin`, the older committed copy was picked anyway,
and the service exited 1 with `error [BAD_JSON] state.json is corrupt: missing
field 'public_until'` on a seed that no longer carries the field — every chain
route answered `503 CHAIN_UNAVAILABLE`. A binary compiled from this tree matches
it by construction; a committed one can only promise what it was built from.

> **Rebuild the shipped binary after a ledger change** (it is a prebuilt
> artifact, and hosts without a Rust toolchain have nothing else). On a Linux
> box or in Docker:
>
> ```
> docker run --rm -e CARGO_TARGET_DIR=/out \
>   -v "$PWD:/src" -v "$PWD/out:/out" -w /src rust:1.85-bookworm \
>   cargo build --release --locked --manifest-path rust/torchain/Cargo.toml
> cp out/release/torchain bin/torchain
> ```
>
> `bin/torchain` in this revision is the 6 Nov build: it serves
> `seed/chain-snapshot.json` at height 938 / head `c2dd9c88…` and no longer knows
> `public_ttl_days` or `public_until`.

`seed/` is
committed on purpose as well: it holds public chain data only — addresses,
public keys, amounts, signatures and the public records — and no key
material. It was exported by a node that still wrote `public_until`; the field is
ignored on import and never written again.

**Ollama needs memory the free plan does not have.** 512 MB cannot hold a 7B
model. Two honest options: set `ONENESS_OLLAMA_AUTO_INSTALL=0` and point
`ONENESS_OLLAMA_URL` at a model host (a ≥2 GB instance, or the machine that
already runs Ollama), or set `ONENESS_OLLAMA_URL=https://ollama.com` +
`ONENESS_OLLAMA_API_KEY` and let Ollama's own hardware answer. A container with a
token but no URL and no binary now takes the second option by itself (see
“adopted” below); with no key either, the brain answers `503 OLLAMA_UNAVAILABLE`
with that instruction, while the chain, the pages and the fetch-data tool keep
working — only the model is down.

This deployment takes the second option: `render.yaml` sets
`ONENESS_OLLAMA_URL=https://ollama.com`, `ONENESS_OLLAMA_MODEL=gpt-oss:20b` (the
local default `qwen2.5:7b-instruct` is not in ollama.com's catalogue — the
catalogue has `gpt-oss:20b`, `glm-5.3`, `kimi-k3`, `deepseek-v4.1-flash`, …),
and the token lives in `.env` (tracked: private, single-operator) or in the
service's environment. A real `OLLAMA_HOST` on a machine that runs Ollama still
wins over `ONENESS_OLLAMA_URL`, so a laptop keeps its own server; `/api/health`
and `/api/ollama/status` report `api_key: true|false` and never the token.

### One proposer, verified replicas (Render + local + onion)

The chain has **one authorized proposer** and any number of **verified replicas**.
That is what lets one Render node, one laptop and one onion node show the same
tokens without pretending there is a consensus protocol that does not exist yet.

| role | what it does |
| --- | --- |
| `origin` (default) | proposes blocks; exports the whole ledger at `GET /api/chain/sync/snapshot` |
| `replica` | pulls that snapshot, re-hashes every block **and** the state root, then imports it into its own Rust ledger (`POST /api/chain/sync/pull` → `POST /api/admin/sync/import`) |

```bash
# replica: a laptop, a second Render service, or a node behind Tor
export ONENESS_NODE_ROLE=replica
export ONENESS_SYNC_ORIGIN=https://oneness.onrender.com   # or http://<id>.onion
export ONENESS_SYNC_TOKEN=<the origin's chain admin token>
export ONENESS_SYNC_INTERVAL=60
python scripts/start.py            # pulls now, then every 60 s (--no-sync to skip)
```

The repository carries that wiring already, so a deploy needs no dashboard
clicking: **`config.fleet.yaml`** names the origin's public URL, its chain token
and that token's sha3-256 digest. A node whose own `ONENESS_ADMIN_TOKEN` hashes to
that digest **is** the origin — it proposes and never pulls from its own URL —
and every other node becomes a read-only replica of that URL, *including* a node
whose dashboard still says `ONENESS_NODE_ROLE=origin` (which is exactly how a
node ends up syncing with itself and reporting a height that never moves). An
explicit `ONENESS_SYNC_ORIGIN` / `ONENESS_SYNC_TOKEN` still wins over the file,
and `/api/chain/sync` reports `source: fleet|config` plus the resolved wiring.

The proposer runs on a laptop, so Render reaches it through a Cloudflare quick
tunnel whose hostname changes every time it restarts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/origin_tunnel.ps1
git add config.fleet.yaml; git commit -m "fleet: origin url"; git push
```

`scripts/origin_tunnel.ps1` starts the tunnel, rewrites `origin.url` in both
`config.fleet.yaml` and the tracked `.env` (which wins), and proves the new URL
really serves the ledger by fetching `GET /api/chain/sync/snapshot` with the fleet
token — exactly what a replica does before importing a byte. `-Commit` pushes the
change so Render redeploys the replicas by itself. Nothing reaches the tunnel
without that token, and no page or log ever prints it.

The replica keeps **its own** admin token (`admin_token_sha256` is node-local and
not part of `state_root`), swaps `state.json` + `blocks.jsonl` atomically, and
refuses to move backwards: a stale or hostile origin can never roll it back
(`force:true` is required to adopt a different branch at the same height). A
replica is **read-only**: `POST /api/chain/tx`, `ledger.send_micro`, the meme
launcher and the token bootstrap answer `409 SYNC_READ_ONLY`, because a local
block would be erased by the next pull. For an
`.onion` origin set `ONENESS_SYNC_PROXY=socks5://127.0.0.1:9050` and
`pip install pysocks`; an HTTPS origin needs nothing extra.

The docs advertise the URL you actually reached — `X-Forwarded-Proto/Host` on
Render, the `.onion` Host header behind Tor (forced to `http`), or
`ONENESS_PUBLIC_BASE_URL` when you pin a domain. `/api/docs` returns
`base_url` + `base_urls{current, source, onion, listen}` and every endpoint's
absolute URL.

### Publish this node as an onion service

Tor must run with a control port; the API does the rest:

```ini
# torrc — forward slashes: quoted paths with backslashes fail to parse
SocksPort 9050
ControlPort 9051
CookieAuthentication 1
CookieAuthFile "C:/<repo>/data/tor/runtime/tor-data/control_auth_cookie"
DataDirectory  "C:/<repo>/data/tor/runtime/tor-data"
```

On Windows the [Tor Expert Bundle](https://dist.torproject.org/torbrowser/)
(`tor-expert-bundle-windows-x86_64-<version>.tar.gz`) unpacks to
`data/tor/runtime/tor/tor.exe` — `tor_service.binary()` looks there, so nothing has
to be added to `PATH`.

```bash
# torrc: ControlPort 9051 + CookieAuthentication 1, then
curl -X POST http://127.0.0.1:8090/api/tor/node -H "x-admin-token: …" -d '{}'
# → {onion, url, service_id, port, reused:false}
curl http://127.0.0.1:8090/api/tor/node      # the recorded link, or published:false
```

The control line is `ADD_ONION <key|NEW:ED25519-V3> Flags=Detach Port=80,127.0.0.1:<api port>`.
`Flags=Detach` is not optional: without it Tor removes the service the moment the
API's control socket closes, so the address is recorded but never answers. Publishing
again reuses the stored private key, so the `.onion` stays the same instead of
changing on every restart.

Set `tor.publish_node: true` (or `ONENESS_TOR_PUBLISH_NODE=1`) to do it at boot.
The onion is only reported after Tor answered `ADD_ONION` — never invented.

Two more switches make that work on a host nobody can shell into (Render, Docker):

```bash
# the node starts the daemon itself: it writes data/tor/torrc (SOCKS 9050 +
# ControlPort 9051 + CookieAuthentication 1) and spawns `tor -f <that torrc>`
ONENESS_TOR_AUTOSTART=1
# and keeps the SAME address after a redeploy (a container disk is ephemeral):
# read it once from GET /api/tor/key (admin) or `python -m oneness_tor.cli key`
ONENESS_TOR_ONION_KEY=<base64 ED25519-V3 blob>
```

The CLI does all of it from a terminal, and never claims a success it did not get:

```bash
python -m oneness_tor.cli status      # tor · control port · onion · role · push timer
python -m oneness_tor.cli start       # start the daemon (honest error if tor is missing)
python -m oneness_tor.cli publish     # ADD_ONION → prints the .onion + the key to persist
python -m oneness_tor.cli key         # ONENESS_TOR_ONION_KEY=…
python -m oneness_tor.cli unpublish   # DEL_ONION
python -m oneness_tor.cli newnym      # new circuit identity
python -m oneness_tor.cli services    # every hidden service this node recorded
```

`GET /api/health` now carries `onion` and `tor {published, key_set, auto_start}`,
so a deploy that is public but has no onion says so instead of looking finished.
The whole Render story — blueprint env vars, Tor Browser steps, node 2 / node 3
replicas, automated snapshot push, the fee panel and a troubleshooting table —
is in [docs/TOR_DEPLOY.md](docs/TOR_DEPLOY.md).

#### Automated snapshot push (origin → node 2 / node 3)

The origin does not wait to be asked: on a timer it exports its own verified
snapshot, keeps a copy on disk and POSTs it to every replica, which re-hashes it
before importing. Configuration is data, not code:

```bash
# node 2 and node 3, comma separated ("url|token" when a replica has its own token)
ONENESS_SYNC_PUSH_TARGETS=https://oneness-tor-node2.onrender.com,https://oneness-tor-node3.onrender.com
ONENESS_SYNC_PUSH_INTERVAL=60        # seconds; 0 = follow sync.interval_seconds
ONENESS_SYNC_PUSH_KEEP=5             # snapshot files kept in data/snapshots
```

```bash
curl -sX POST http://127.0.0.1:8090/api/chain/sync/push -H "x-admin-token: …"
# → {origin_height, pushed, failed, saved, targets:[{url, ok, imported, reason}]}
curl -s http://127.0.0.1:8090/api/chain/sync/push -H "x-admin-token: …"
python -m oneness_tor.cli sync --push
```

A replica answers `POST /api/chain/sync/accept`, verifies, and imports — it never
pushes, and it never rolls backwards. `data/snapshots/latest.json` is a real
backup: a wiped container re-seeds from it with `ONENESS_NODE_SEED`.

#### The fee schedule is the admin's

`/admin` → **Service fees** sets the price of every service (contract create,
mint, transfer, meme launch, pool, volume bot, landing page, trending, banner,
AI-NFT mint, API keys) without restarting anything:

```
POST   /api/admin/fees  {"prices": {"meme_banner_7d": 45}}     admin
DELETE /api/admin/fees/<code>                                 reset one row
DELETE /api/admin/fees                                        reset all
GET    /api/admin/fees                                        live + defaults + history
```

The panel writes `data/admin/pricing.json`; `core.pricing` reads it on the next
request, and `/api/pricing` overlays it onto the Rust node's own table, so the
number on the page, the quote and the charge can never disagree. LOCKED Section 8
rows need `confirm_locked: true` — and every change is recorded with the
credential that made it. Same thing from the shell:

```bash
python -m oneness_tor.cli fees list
python -m oneness_tor.cli fees set mint 4.5 --confirm-locked
python -m oneness_tor.cli fees reset mint
```

#### If the onion looks dead in Tor Browser

The service is plain `http://` (v3 onions are authenticated and encrypted end to end,
so no TLS is added). Tor Browser blocks JavaScript on non-HTTPS pages at the
**Safer** and **Safest** security levels, so the shell, CSS and images render while
`/api/*` is never called: every pill and card stays on `…` and the node *looks*
down while it is healthy. Use the shield icon → **Standard** (or “Allow scripts on
this site”), then reload with Ctrl+Shift+R. `templates/base.html` ships a
`<noscript>` banner that says exactly this, so the failure is no longer silent.

Prove the service from the host without a browser — the SOCKS port resolves the
name, so no `/etc/hosts` entry is needed:

```bash
curl --socks5-hostname 127.0.0.1:9050 \
  http://<onion>/api/health        # {"status":"ok","height":33,"node_reachable":true}
curl --socks5-hostname 127.0.0.1:9050 http://<onion>/api/chain/info
curl --socks5-hostname 127.0.0.1:9050 http://<onion>/api/ollama/status
```

A flat height is not a sync fault: single-proposer PoA only appends a block when a
transaction is submitted, and `/api/chain/sync` reports `mode: single-node-poa`,
`peers: []` and `replication.configured: false` on the node that proposes.

### Top-100 token bootstrap

The cached public metadata (CoinGecko top coins by market cap, `data/metadata/token-images.json`)
already ranks the tickers. One admin call mints them as real on-chain tokens:

```bash
python tools/bootstrap_tokens.py                       # plan only
python tools/bootstrap_tokens.py --run                 # mint (5 TOR each, idempotent)
python tools/bootstrap_tokens.py --run --limit 100 --pools   # + a 10 TOR pool each
# same thing over HTTP:
curl -X POST http://127.0.0.1:8090/api/tokens/bootstrap \
  -H "x-admin-token: …" -H "content-type: application/json" \
  -d '{"limit":100,"supply":1000000,"dry_run":false}'
```

Existing tickers are skipped, every failure is reported per ticker, and replicas
show the result after their next `POST /api/chain/sync/pull`. The `/admin` page
has the same panel with plan/mint buttons.

> This bootstrap mints **one fixed supply for every ticker** (the `supply` you
> pass, `1 000 000` by default). That is a smoke test, not a market: a market cap
> computed from it is wrong the moment the real asset has another supply. Use the
> fleets below — they mint each asset's own provider-published **total supply**.

### The 120-token fleet — one wallet, one token

The bootstrap above mints from one wallet at a fixed supply. The fleet goes
further: it fetches **the top 100 assets by market cap plus the top 20 Solana
memecoins** from three key-less providers (two fallbacks each), generates **one
fresh Ed25519 wallet per asset**, funds it from the founder and mints **one token
in each** with the provider's own **total supply**. The provider's image is what
the launcher metadata carries *and* what the node's logo cache remembers, so the
token table, the launcher, the wallet page and the explorer draw one picture — and
each picture is **mirrored onto this node** (`data/metadata/token-logos/`, served at
`/assets/tokens/<SYMBOL>.<ext>`), so no page depends on CoinGecko being reachable
from the visitor's browser.

```bash
python tools/mint_fleet.py                # plan: 120 rows, fee + funding estimate
python tools/mint_fleet.py --run          # 120 wallets + 120 token_create txs
python tools/mint_fleet.py --run --start 121 --pin   # re-mint: 120 NEW wallets
python tools/mint_fleet.py --document     # rewrite docs/TOKEN_FLEET.md only

# over HTTP (dry_run is the default; "start" numbers the wallets)
curl -X POST http://127.0.0.1:8090/api/tokens/fleet \
  -H "x-admin-token: …" -H "content-type: application/json" -d '{"dry_run":false}'
```

Every mint writes image metadata **at mint time**: the provider's picture is
mirrored onto this node first, then the token document carries both URIs
(`image` = the public source, `properties.image_mirror` = this node's
`/assets/tokens/<SYMBOL>.<ext>`) and is pinned to IPFS when Pinata is configured
(`--pin`, the fleet run uses it). No logo can fail a mint that is already on chain.

| list | primary | fallback 1 | fallback 2 |
| --- | --- | --- | --- |
| top 100 assets | CoinGecko `/coins/markets` | CoinPaprika `/v1/tickers` | CoinLore `/api/tickers` |
| top 20 Solana memecoins | CoinGecko `solana-meme-coins` | GeckoTerminal Solana pools | DexScreener token profiles |

`GET /api/tokens/universe` returns the 120 assets with price, market cap, supply
and logo (and which provider answered); `GET /api/tokens/fleet` returns every
wallet with its address, base58 public key, ticker, supply, logo and txid. The run
writes `data/metadata/token-fleet.json` (machine) and `docs/TOKEN_FLEET.md`
(human). Both list only the **public** half of each key pair — the keystores stay
in `data/wallets/fleetNNN.json`, which git ignores. A ticker can exist only once
on this chain (`token_create` is keyed by ticker), so the fleet wants a ledger
where those tickers are free: an existing ticker is skipped and reported, never
re-minted.

#### Re-minting: a new generation

An on-chain supply cannot be edited (`token_create` writes it once), so correcting
a supply means minting the ticker again — into a **new wallet**, on a ledger where
the ticker is free. `--start` is that switch: `--start 121` mints
`fleet121…fleet240`, i.e. 120 brand-new Ed25519 key pairs and addresses, each with
the provider's exact total supply, and records the run as **generation 2**.

* the previous record is frozen as `data/metadata/token-fleet/gen-<n>.json`, so
  the old wallets, addresses, public keys and mint transactions stay auditable;
  `docs/TOKEN_FLEET.md` keeps a *Superseded generations* table with the pointer;
* a retry with the **same** `--start` is a resume: it merges into that generation,
  skips what is already on chain and mints only what failed (`GET
  /api/tokens/fleet` shows `plan.start`, the next free wallet number);
* a fresh ledger is the honest way to release the tickers, then
  `python scripts/export_seed.py` re-exports `seed/chain-snapshot.json` so every
  later deployment starts from the corrected supplies instead of re-minting them.

#### Every listing leads with the newest mint

The mint time is chain state (`token.created_at`), so ordering by it is identical
on every node. The public token table (`/api/tokens/public`, `/api/public/tokens`),
the launcher (`/api/meme/tokens`), the wallet page and the **Explorer** and **TOR
coin** pages all draw the 120 tokens newest-first (paid promotions still outrank
everything in the launcher), and every row carries the logo the node mirrors —
`/assets/tokens/<SYMBOL>.<ext>` — including `webp`, which is served as `image/*`.

`config.yaml` is **per node and never committed** (it carries `api.admin_token`,
plus the founder wallet path): copy `config.example.yaml` to `config.yaml` and edit
that. Env vars always win, and with no `config.yaml` at all the loader falls back to
the built-in defaults in `core/config.py`. `.env` **is** committed — this repository
is private and single-operator, so the Pinata/IPFS credentials are active on every
checkout and deploy; a real environment variable with the same name still wins.
`data/`, `logs/` and every wallet/key file are git-ignored.

## Pages

| page | what it does |
| --- | --- |
| `/` | landing: live height/supply/founder, revenue, Tor state, the pinned logo |
| `/dashboard` | **Ollama command deck**: prompt → real API calls → answer + call log |
| `/explorer` | chain info, block range, public records, account/tx/block search, verify |
| `/admin` | **node admin**: the **admin account** (username + password register/login, change username/password, rotate API key, generate a new admin wallet that becomes the fee collector, delete the account), replication (role/origin/`pull from origin`), onion publishing, the **automated sync audit** (re-hashes every block), the **top-100 token bootstrap** and the **120-token fleet** (plan/mint), integrity check, events, the privacy rule, contract deploy rights, raw signed-tx broadcast, **one model per AI surface**, endpoint coverage |
| `/wallet` | **username signup** (real Ed25519 wallet, private key shown **once**), login, **up to five wallets** per account (generate + import a Phantom key/phrase), **password-authorized private-key export** under “Safely save your private key”, one click to load a wallet’s balance, token table (logo, mint address, price, market cap, supply, holders, holdings), AI-NFTs, **receive** panel (address, public key base58+hex, QR for each) and the **whole transaction history** with hex; TOR + token send, sign-only, owned programs | 
| `/payments` | locked plans, quotes, invoices, real settlement, revenue per code |
| `/contracts` | Rust templates, `rustc` compile + 5 TOR deploy, calls, authorized nodes |
| `/workbench` | **app builders**: DarkWeb web apps + Rust programs — read/create/write/delete real files, run commands, install dependencies, prompt the builder for edits, start the app and preview it |
| `/meme` | meme launcher: on-chain token create (5 TOR), pool launch (10 TOR), real buys/sells, quotes, volume sessions, AI landing pages, promotions, DEX seams (`501`) |
| `/nft` | AI-NFT Studio: 3 TOR 1-of-1 mint, catalogue, metadata loader (TOR / Solana / URI) and the chatbox that speaks as the token |
| `/ollama` | bootstrap (install → start → first model), curated download buttons, `ollama run <model>` command bar, pull with progress, chat |
| `/docs` | every endpoint with schema + “try it” through the fetch-data tool, and **developer API keys**: price, buy from your wallet, list/revoke, `x-api-key` curl |
| `/coin` | coin metadata, logo CID, both URIs, ready `<img>` tag, price table, and the permanent token table (`price`, `pools`, `market cap`, `24h volume`, `mint address`, `owner`) |

Audio on the AI surfaces: `/dashboard` and `/ollama` read every answer aloud with a
**female** voice (`play response` **ON/OFF**, remembered per surface) and carry a
**record** button that transcribes speech into the prompt; `/nft` adds a
**male / female** switch so the token can answer in either voice. Speech uses the
browser's own `speechSynthesis` / `SpeechRecognition` (recording needs Chrome or Edge).

## Accounts: wallet signup and the admin login

Two account kinds, one store (`data/accounts/accounts.json`), no plaintext secrets.

**Wallets (public, up to five per account).** `POST /api/auth/signup {username,
password}` runs a real `torchain wallet new`, stores a scrypt password hash and
returns the wallet's **private key exactly once** (`one_time.private_key_base58`,
`shown_once: true`). Login returns a 1-hour `x-wallet-session` — the same session
the wallet page has always used — so the platform can charge that wallet for
mints, AI-NFTs, meme launches and contract deploys, and
treat it as the owner. `GET /api/auth/me` reads the account and every wallet it
owns; another account's wallet stays 403.

An account holds **up to five wallets**:

* `POST /api/auth/wallets {password, label}` — generate another real wallet,
* `POST /api/auth/wallets/import {password, secret_key_base58 | mnemonic, label}`
  — import a Phantom private key (64-byte base58) or a 12–24 word recovery
  phrase; the address is derived exactly like the chain derives it,
* `GET /api/auth/wallets` — all five with live public balances, primary flag and
  labels; clicking one on `/wallet` loads its tokens, AI-NFTs, balance, public key
  and receive QR codes,
* `POST /api/auth/wallets/<name>/export {password}` — **password-authorized
  private key export** (base58 + recovery words when the keystore has them). The
  password is re-checked on every export, the response is `Cache-Control:
  no-store`, and ten wrong guesses in five minutes answer `429 TOO_MANY_ATTEMPTS`,
* `POST /api/auth/wallets/<name>/select` rotates the session, and
  `POST /api/auth/wallets/<name>/primary` chooses the wallet a new login opens.

Every payment dropdown (meme, NFT, payments, contracts, wallet)
lists all of the account's wallets, and a session for one wallet may read/send
from its siblings — never from anybody else's.

**Admin (one account).** Visit `/admin`: with no account the page shows
registration (username + password, optional chain admin token), after that only
sign-in. The password authorizes every admin route with `x-admin-session`
(2 hours). Settings cover:

* change username, change password (closes every old session),
* **rotate API key** — mint a new `otk_…` platform key (`x-admin-token`); the
  previous key stops working immediately,
* **generate a new admin wallet** — creates a wallet, deletes the account's
  previous wallet file and moves the chain's **fee collector** to the new
  address (`POST /api/admin/fee-receiver` in the Rust node),
* **delete admin account** — removes the account and re-enables registration;
  the wallet file is kept so funds are never destroyed,
* support: **support@onenesstech.ca**.

A chain token bound at registration is encrypted at rest with AES-GCM under a key
derived from the password (scrypt, 2^15); only a live session can decrypt it. When
no token is bound, the session uses the deployment's own credential
(`data/admin/chain_token`, or the origin's token on a replica — both adopted by
the ledger at boot), so chain-level actions (sealing the archive, moving the fee
collector) work from the username/password login without anybody pasting a token.

**Fees are layered.** The Rust ledger charges **no transaction fee** — a
transfer moves exactly its amount (`BASE_TX_FEE_MICRO = 0`). The node/platform
adds its own fee schedule: 0.1 TOR per transfer, 3 TOR mint, 5 TOR contract,
plus the launcher and API-key fees. `GET /api/pricing` reports both
(`fees.base_layer_tx_fee_micro: 0` and the rows).

## The brain: schema in, real data out

1. `core/api_catalog.py` declares all **153** endpoints (name, method, path,
   params, auth, price, return shape) — the single source for the docs page, the
   OpenAPI document, the try-it forms and the system prompt.
2. `core/ollama.py::build_system_prompt` injects that schema **plus a live
   snapshot** (`chain.info`, `coin`, `pricing`, recent records) read seconds ago.
3. The model gets exactly one tool, `fetch_oneness_data{endpoint, params}`.
4. `core/tools.py::ToolBox` performs the real HTTP request and returns
   `{request, response, http_status, latency_ms}` — what the dashboard logs.
5. Models without native tool calling (e.g. `gemma3:1b`) are detected and
   switched to a JSON tool protocol automatically.

`GET /api/ollama/prompt` returns the exact prompt so you can audit it.

### The AI-NFT dynamic field

`core/nft.py` resolves real metadata from three sources: this node's catalogue
(a 3 TOR mint), a Solana Metaplex mint (the PDA is derived in pure Python —
`nft.solana_rpc` / `ONENESS_SOLANA_RPC` — then `getAccountInfo` and the
off-chain JSON), or any `https://` / `ipfs://` metadata document. The result is
injected as a prompt field that begins with the hardcoded header:

```
I AM THIS NFT
(token metadata — chain oneness-tor, source oneness-tor)
- Name: Cyan Warden ($WARDEN)
- Contract / mint: TOR1…onion
- Token ID: #1
- Description: …
- Traits: Aura: cyan
```

* `POST /api/nft/chat` uses the compact persona prompt (identity + rules, no
  tool schema) so even a 1B model answers as the token instead of reading docs.
* `POST /api/ai/command` with `{"nft": {"address": "…", "token_id": 1}}` keeps
  the full tool loop but still leads with the same block and closes with a
  “you are this NFT” reminder.
* `GET /api/nft/metadata` returns `{nft, prompt}` — the exact block, never a
  paraphrase.

## Ollama: carried by the node

* The API URL follows `OLLAMA_HOST` (so `OLLAMA_HOST=127.0.0.1:8080` works), then
  `ONENESS_OLLAMA_URL` / `ollama.base_url`, then `127.0.0.1:11434`.
* **A hosted brain, when this machine cannot hold a model.** Set
  `ONENESS_OLLAMA_URL=https://ollama.com` and `ONENESS_OLLAMA_API_KEY` (or
  `OLLAMA_API_KEY`); every call then carries `Authorization: Bearer …`, so the
  weights and the tool calls run on Ollama's hardware and this process only
  needs the URL. The same two variables point at a tunnel to your own machine.
  A hosted URL is **never** installed, started or killed from here —
  `install()`, `start_server()` and `ensure_runtime()` all refuse it and say
  why. A 401/403 becomes `503 OLLAMA_UNAVAILABLE` with the fix in the message
  (`the API key was rejected …` / `this host needs an API key …`), and the key
  is never rendered, logged or returned: `GET /api/ollama/status` and
  `GET /api/health` expose only `remote: true` and `api_key: true|false`.
* **A machine that cannot run a model is told the truth, not “install it”.**
  With no `ollama` binary and nothing answering locally, `POST /api/ollama/start`
  (and the typed `ollama serve`) name the loopback port that *did* answer, else
  `ONENESS_OLLAMA_URL=https://ollama.com` and whether the key is already set —
  `/api/ollama/install` stays in the error's `details` for a machine that can
  hold a model. The boot banner prints `Ollama -> hosted https://ollama.com — …`
  and never the “install from …” line when the URL is not this machine.
* **“Adopted”: the hosted exit, taken automatically when it is the only one.**
  If nobody chose a URL (`OLLAMA_HOST` and `ONENESS_OLLAMA_URL` unset, so
  `ollama.base_url` is still the shipped `127.0.0.1:11434`), no `ollama` binary
  exists, nothing answers on loopback, and a token *is* set, the start command
  sets `ONENESS_OLLAMA_URL=https://ollama.com` itself (`ollama.adopt_hosted()`)
  and says so: ``no local `ollama` binary and no ONENESS_OLLAMA_URL was set —
  adopted the hosted brain``. That is the 512 MB container, and it is the only
  state that is ever changed: an explicit `OLLAMA_HOST` / `ONENESS_OLLAMA_URL`, a
  non-default `ollama.base_url`, an installed binary, a loopback Ollama that does
  answer, `ONENESS_OLLAMA_AUTO_INSTALL=1`, or no token at all all leave the
  configuration exactly as it was.
* **Wrong port is called out, not guessed.** If the configured loopback address
  is silent but an Ollama answers on another local port (11434 / 8080 / 11435 /
  11436), the status error names it: `an Ollama *is* answering on
  http://127.0.0.1:8080: set ONENESS_OLLAMA_URL=… and restart`. Nothing switches
  port behind your back — read-only probe, explicit fix.
* `python -m oneness_tor` calls `ollama.ensure_runtime()` at boot: if the binary
  exists but nothing answers it starts `ollama serve` and prints what happened.
  Set `ollama.auto_install: true` in `config.yaml` if the node should install
  Ollama itself at boot; the default keeps installs explicit.
* `POST /api/ollama/bootstrap` does the whole chain in one call: **install →
  start → pull the default model**, and returns every step with its real
  outcome (`steps[]`, `next` when something failed).
* `GET /api/ollama/suggested` is the download-button catalogue (size, native
  tool support, `installed`), and `POST /api/ollama/pull` streams NDJSON
  progress (the page renders a live bar).
* `POST /api/ollama/command` runs a typed CLI line — `ollama run <model>
  [prompt]`, `pull`, `list`, `ps`, `show`, `rm`, `serve`, `help` — against the
  Ollama API the node carries. `pull` hands back the streaming endpoint instead
  of blocking. `POST /api/ollama/run` stays available for a plain generation.
* `POST /api/ollama/install` installs Ollama (`winget` on Windows, `install.sh`
  elsewhere); `DELETE /api/ollama/models/<model>` removes a model.

### One model per AI surface

Four surfaces can each use a different local model — the blockchain interface
(the command brain), the AI-NFT chat, the DarkWeb App Builder and the Rust
Program Builder. The `/admin` page renders one dropdown of **every model on this
device** per surface:

* resolution order: admin override (`data/ollama_routing.json`) →
  `ollama.models.<surface>` in `config.yaml` → `ollama.model`,
* `GET /api/ollama/routing` returns each surface with its model, where that model
  came from and whether it is installed; `POST /api/ollama/routing` saves
  `{purpose, model}` (admin) and refuses a model that is not on disk — the
  dropdown can never store a name that would fail on the next call,
* `ollama.pick_model(..., purpose=…)` is what every caller uses, so the AI-NFT
  chat, the meme landing page and both workbenches pick their surface's model.

#### Reasoning models: `think: false` for anything structured

Thinking models (`deepseek*`, `glm*`, `gpt-oss*`, `nemotron*` …) write a reasoning
trace before the answer, and that trace is billed against `num_predict`. With a
4096-token budget a `deepseek-v4.1-flash:cloud` reply came back **empty** with
`done_reason: "length"` — 5924 characters of thinking, zero characters of page.
Both builders and the meme landing-page generator therefore ask `/api/show` for
the model's `capabilities` (cached in
`ollama.supports_thinking`) and send `think: false` when it declares
`thinking`, so the budget goes to the answer. A plain model never gets the flag
(`think: true` on one is an HTTP 400).

When a reply is still cut off (`done_reason: "length"`), the builders retry once
with a bigger budget (`workbench.PLAN_BUDGETS`, `builder.BUILD_BUDGETS`); if the
plan is *still* truncated the workbench salvages every file object that closed
before the limit and reports the rest as a warning, and a page that stops before
`</html>` is refused instead of written to disk.

## App builders (workbench)

`/workbench` is two operator consoles over one real workspace
(`data/workbench/<build_id>/`):

* **DarkWeb App Builder** — multi-file web apps (`index.html`, `styles.css`,
  `app.js`, or a Node `server.js`),
* **Rust Program Builder** — a real cargo project (`Cargo.toml` + `src/main.rs`,
  a std-only HTTP server that honours `PORT`).

Everything is a real operation: read / create / write / append / delete files,
`run` a command in the project directory, `install` dependencies (`pip`, `npm`,
`cargo add`), `serve` the app on a free port, watch its real stdout/stderr, and
open the preview (the node also serves the folder at `/builds/<id>/`). The AI box
keeps a conversation: each prompt sends the whole workspace to the surface's
model, which answers with a JSON file plan (`{answer, files[], delete[]}`) that
is applied to disk — prompt again to keep editing the app.

Hard rules, enforced in code:

* **no shell** — the command line is split into argv and run with `shell=False`
  against an allow-list (`cargo`, `rustc`, `python`, `pip`, `node`, `npm`, `npx`,
  `git`, `make`, `dotnet`, `go`, `java`, `pytest`); `; | && >` are rejected,
* **no escape** — every path is validated twice (strict segment pattern, then a
  resolved-path prefix check) and the model cannot write outside the project,
* **the entry point survives** — a plan that deletes `index.html` /
  `src/main.rs` is refused (and restored if it ever disappears), with the reason
  reported in `warnings`,
* generation uses Ollama structured output (`format: "json"`), so even a 1B model
  returns parseable JSON; a fenced-code reply is used as a fallback,
* workbench AI edits are **free** (admin operator console) — **deploy** is what
  publishes, at `POST /api/workbench/projects/<id>/deploy`.

### Deploy: both builders, for real

`POST /api/workbench/projects/<id>/deploy` (admin, or the owner's wallet session):

* **DarkWeb App Builder** → Tor's control port performs `ADD_ONION` for
  `Port=80,127.0.0.1:<api port>` and points the service at `/builds/<id>/`, so the
  answer is a working `http://<serviceid>.onion/builds/<id>/` URL. When the
  control port is down the response is `TOR_DAEMON_UNAVAILABLE` with the exact
  torrc fix — never a made-up address.
* **Rust Program Builder** → runs `cargo build --release`, hashes the binary with
  sha3-256 and registers a program record (`kind: "rust_program"`) owned by the
  deploying wallet. **Only that wallet — or an admin credential — can see the
  source, state, compiler output, binary path and fee history, and only it can
  manage it** (`POST /api/contracts/<id>/call`, function `run` executes the
  compiled binary). Everyone else gets a public summary (`name`, owner address,
  status, source/binary hashes). `GET /api/contracts/mine` lists the signed-in
  wallet's programs; the wallet page renders them with a *manage* button.

Tests prove both paths: `tests/test_builders_deploy.py` speaks the Tor control
protocol over a socket for the onion flow and runs a real `cargo build` for the
program flow, including the owner-only reads and the 403 for a stranger.

## Node synchronization, audited

`GET /api/chain/sync` reports the wiring (peers, catch-up endpoint, height).
`GET /api/chain/sync/audit` proves it: it pulls every block from the Rust node,
re-computes each digest with the ledger's exact payload (sha3-256 over the
canonical JSON of `height, prev_hash, timestamp, proposer, state_root, txids`),
walks the `prev_hash` links, compares block 0 with `genesis_hash` and the newest
block with `head_hash`, checks the count against `height`, and probes every peer
in `chain.peers` for how far ahead it is. The verdict is `in-sync`, `drift` or
`node-down` — the *audit chain* button on `/admin` runs it.

## The six platforms (one node, one market engine)

Six apps read this node and share one client kit (`apps/tor-kit`): logo, dark
3D-neon design system, wallet, charts and the AI-NFT ChatBox. Full detail in
[`docs/PLATFORMS.md`](docs/PLATFORMS.md); the engine rules are in
[`docs/MARKET.md`](docs/MARKET.md).

| # | Platform | Folder | Port |
| --- | --- | --- | --- |
| 1 | TOR ONION SWAP — swaps, pools, portfolio, wallet, listings, chat, API docs | `…\zenith-main\app` | 5173 |
| 2 | TOR-SCREENER — board, per-token charts & profiles, 50 TOR banners, 100 TOR API keys | `…\dexscreener-clone-main\dexscreener-clone` | 5174 |
| 3 | DARKWEB CRYPTO — the same board + both fee receivers + your API keys | `…\axiom-clone-main` | 5175 |
| 4 | TOR PUMP — free mints, $5 000 platform pool, $25k curve, creator fees | `…\openpump-main\web` | 5176 |
| 5 | TOR CHARTS — the candle generator rendered and documented | `…\lightweight-charts-master\tor-charts` | 5177 |
| 6 | SOLARIX ENGINE — TOR at $3.60, market caps, pools, movers | `…\Solarix-main\dashboard` | 5178 |

```powershell
python tools\scaffold_apps.py --force     # shells + copy the kit into all six
python tools\make_project_logos.py        # 3D-neon SVG logos (node + each app)
python tools\write_app_readmes.py         # a README per platform folder
python tools\seed_market.py plan          # 120 TOR pools + 50 USDC books, $25M
```

`GET /api/projects` lists all six with logos, dev/onion/clearnet URLs and the fee
wallets every platform settles to (**Tor AI-NFT Wallet**).

## HTTP API

`GET /api/docs` is the machine-readable catalog; `/api/openapi.json` is generated
from it. Groups:

| group | examples |
| --- | --- |
| chain / records / tx / admin | `chain.info`, `chain.blocks`, `records.public`, `records.private`, `tx.submit` |
| account / tokens / pools | `account.get`, `tokens.list`, `pools.list` |
| coin / pricing | `coin.get`, `pricing.get` |
| auth / dev | `auth.signup`, `auth.wallets` (list/generate/import/export/select/primary), `dev.pricing`, `dev.keys`, `dev.keys_create`, `dev.call` (`/api/v1/<endpoint>`, x-api-key) |
| ipfs | `ipfs.status`, `ipfs.resolve`, `ipfs.pin` (admin) — Pinata metadata for meme tokens and AI-NFTs |
| wallet | `wallets.list`, `wallets.send`, `wallets.sign`, `wallets.create`, `wallets.receive` (pubkey + QR), `wallets.history` (whole history, hex) |
| token logos | `tokens.images` (symbol → logo, source, market-cap rank, mirrored file; cached), `tokens.images_refresh` (admin), `tokens.images_mirror` (admin — copy every logo onto this node) |
| payments | `payments.plans`, `payments.quote`, invoices create/pay, `payments.revenue` |
| contracts | templates, deploy (5 TOR), call, nodes |
| tor | `tor.status`, `tor.node` (the node's own onion), `tor.new-identity` |
| workbench | projects, files read/write/delete, `run`, `install`, `ai`, `serve` (+ `/builds/<id>/` preview) |
| meme | `meme.tools`, tokens create/list/market, pools, swap, quote, promotions, volume sessions, landing (10 TOR), `dex_*` seams |
| nft | `nft.catalogue`, `nft.mint` (3 TOR), `nft.metadata`, `nft.chat` |
| ollama / tools / docs | status, models, suggested, bootstrap, command, pull, run, chat, prompt, `tools.fetch`, `ai.command` |
| policy / tokens / public | `tokens.public` (the public table), `public.ledger`, `public.tokens`, `public.token`, `public.token_holders`, `public.balances`, `public.balance`, `public.totals`, `privacy.get`, `wallets.challenge`, `wallets.session` |
| market | `market.overview`, `market.tokens`, `market.token`, `market.tor`, `market.chart`, `market.pools`, `market.quote`, `market.swap` — the price & market-cap engine |
| projects / ads | `market.projects` (the six platforms + fee wallets), `market.ads` (rotation), `market.ads_buy` (50 TOR/week) |
| ai | `market.ai_models` (free OpenRouter models), `market.ai_status`, `market.ai_chat` (the AI-NFT ChatBox) |
| pump | `market.pump_list`, `market.pump`, `market.pump_launch` (free mint, $5 000 pool), `market.pump_claim` |

Responses always use `{ok, data, error}`. Operator routes (`/api/admin/*`, the
wallets list, archive/private records, Ollama install/routing) need the admin
session (`x-admin-session`) from `/admin` or `/ollama`; every public action needs
only the visitor's wallet session (`x-wallet-session`) from `/wallet`.

## Developer API keys (sold in the docs)

The website is one consumer of the API; **developers are another**, and they pay
for it. `/docs` has a key panel, and `GET /api/dev/keys/pricing` publishes the
terms:

* **price** — `api_key` in the locked price table (100 TOR per 30 days,
  config-overridable), charged **on-chain** from the signed-in wallet,
* **buy** — `POST /api/dev/keys {label, days}` → the key is returned **once**
  (`otd_…`); only its `sha3-256` hash and a hint are stored,
* **call** — `GET|POST /api/v1/<catalog-endpoint>` with `x-api-key`. The gateway
  forwards to the real route in-process and returns the same envelope plus
  `meta.developer_key` and `meta.request`,
* **limits** — 300 calls/minute and 50 000/day per key (config `dev_keys.*`), 429
  with `retry_after_seconds` when exceeded,
* **revoke** — `DELETE /api/dev/keys/<id>` stops it immediately; `GET
  /api/dev/keys` lists hints, expiry and usage,
* **scope** — a key reads the public API; anything that moves value still needs
  the wallet owner's session/signature, so a leaked key can never spend.

`api.require_key: true` (`ONENESS_REQUIRE_API_KEY=1`) raises the bar further and
requires a key (or an admin/wallet session) on **every** `/api` call — useful when
the API is deployed headless, off by default because the website calls the same
API from the browser.

## IPFS metadata (Pinata)

Meme-token launch metadata and AI-NFT metadata are pinned through the operator's
Pinata account, so a wallet, DEX listing or marketplace can resolve them from the
token id alone:

* credentials live in `.env` at the repo root (`PINATA_JWT`, or `PINATA_API_KEY` +
  `PINATA_API_SECRET`) plus `IPFS_GATEWAY`; `core.config.load_dotenv` loads the file
  on start, an environment variable of the same name always wins, and the
  credentials never appear in an API response,
* a private gateway needs its read token: `PINATA_GATEWAY_TOKEN` (or the token
  already inside `IPFS_GATEWAY`) is appended once as `?pinataGatewayToken=…`, so
  `<img src>` and metadata fetches work instead of answering `403`,
* every `ipfs://` URI is resolved through the configured gateway for `<img src>`
  and metadata fetches (`core/ipfs.py`, `core/nft.resolve_uri`),
* `meme.create_token` pins `oneness-token-metadata/1` (name, symbol, supply,
  owner, image, socials) unless `pin_metadata: false`; `nft_studio.mint` pins the
  NFT document and, when the image is a `data:` URL, the pixels too,
* a pin failure never fails a mint: the record says `metadata_pinned: false` and
  keeps this node's own metadata route,
* `GET /api/ipfs/status` and `GET /api/ipfs/resolve` are public;
  `POST /api/ipfs/pin` is the admin primitive.


## Privacy is asked per transaction: Public or Anonymous

Address visibility is **not** a calendar any more. Before every send the wallet
asks one question, and the answer is stamped on that one record:

| the wallet picks | what the chain record shows | when |
| --- | --- | --- |
| **Public** (default) | the sending wallet's address | from the first confirmation |
| **Anonymous** | `Tor-Anonymous-onion` | from the first confirmation |

```powershell
# the wallet page asks on every send: /wallet -> send -> privacy
POST /api/wallets/founder/send  {"to": "TOR1…onion", "amount": 1.5, "privacy": "anonymous"}
POST /api/wallets/founder/send  {"to": "TOR1…onion", "amount": 1.5, "privacy": "public"}
GET  /api/privacy               # the rule, in full, machine-readable
```

How it works — and why it needed no chain rewrite:

* protocol `oneness-tor/privacy/1` rides in the transaction's `external_ref`
  field, which the Rust ledger already copies into the record
  (`tx_transfer` → `make_record`). No new tx kind, no new struct field, no
  state-root/block-hash change: a node on the committed `torchain` binary
  accepts it today.
* a **public** send writes an empty ref, so it is byte-identical to a legacy
  transfer; every old record therefore reads back as `public`, which is what it
  was.
* `external_ref` is not part of the canonical signing message, and only the
  wallet that signs a transaction can mark it — it is its own address it hides.
* every public projection masks through one function
  (`oneness_tor.services.privacy.public_view`): `GET /api/public/records`,
  `GET /api/chain/events`, `GET /api/chain/tx/<txid>`. The chain admin token
  still reads the truth, and a transfer's 0.1 TOR platform fee inherits the same
  mode (a public fee would re-publish the sender).

A sender can hide **only its own** address. The receiver is never masked — a
payment has to name the wallet that receives it, that wallet belongs to somebody
else, and its owner never chose to be hidden. There is no recipient-masking
option anywhere in the API, the config or the UI.

What an anonymous record still shows — stated plainly, because a privacy claim
that hides its limits is a lie:

* **shown**: the receiving address, amount, token, fee, note, timestamp, height,
  block, txid, signature;
* **shown**: the balance sheet (`/api/public/balances`, `/api/public/ledger`,
  `/api/public/totals`) — balances are public ledger data by design, so an
  observer who watches balances move block by block can still correlate a
  transfer;
* **not claimed**: cryptographic untraceability. The ledger must know the signer
  to debit it, and the node operator can read the record. Ring signatures /
  shielded pools would be `oneness-tor/privacy/2`.

There is no time window left anywhere: the ledger keeps every record forever and
seals nothing. Privacy is the per-transaction choice above, and only that.

Configuration (`config.yaml`, env wins):

```yaml
privacy:
  default_mode: public                   # ONENESS_PRIVACY_DEFAULT
  anonymous_label: Tor-Anonymous-onion   # ONENESS_PRIVACY_LABEL
```

Full write-up, including the two honest limits (the public balance sheet and the
node's own loopback port) and the upgrade path to `oneness-tor/privacy/2`:
[`docs/PRIVACY.md`](docs/PRIVACY.md). Nothing retroactive: a record mined before
this protocol existed is `public`, and no flag can hide it after the fact.

## The public ledger (permanent, and nothing expires)

Every record this node keeps stays readable. There is no expiry clock, no sweep,
no private archive and no admin route that hides an old record. What a stranger
can always read is the public ledger — the token table, the per-token totals and
the balance sheet:

| endpoint | what survives |
| --- | --- |
| `GET /api/tokens/public` | per token: `symbol`, `name`, `mint_address`, `owner`, `supply`, `decimals`, `circulating`, `in_pools`, `holders`, `price_tor`, `pools` (reserves), `liquidity_tor`, `market_cap_tor`, `volume_24h_tor`, `volume_24h_swaps`, `image`, `image_source`, `image_rank`, `updated_at`, plus the top-level `totals` and `images` blocks (cache source, count, age) — every price is denominated in TOR, there is no fiat rate |
| `GET /api/public/tokens` | the same table (canonical alias) |
| `GET /api/public/tokens/<SYMBOL>` | one token: supply, circulating supply, pooled tokens, holder count, owner, decimals and its AMM pool with `price_tor` |
| `GET /api/public/tokens/<SYMBOL>/holders` | every address holding it, with amount and share of supply |
| `GET /api/public/ledger` | the whole public ledger: every account with `balance_micro` and `token_balances`, every token with its totals, every pool and the chain totals (paginated, `limit`/`offset`) |
| `GET /api/public/balances` | the public balance sheet — every address, its TOR balance and token balances |
| `GET /api/public/balances/<address>` | one address's balances (same numbers as `/api/account/<address>`) |
| `GET /api/public/totals` | chain-wide totals: supply, accounts, tokens, pools, holders, pooled tokens |
| `GET /api/tokens/images` | symbol → logo (`image`, `source`, `rank`, `coingecko_id`, `logo_file`) resolved from CoinGecko's top coins by market cap, DexScreener as fallback, cached in `data/metadata/token-images.json` for 24 h; the `logos` block says how many are mirrored on this node |
| `POST /api/tokens/images/mirror` | **admin** — copy every cached logo into `data/metadata/token-logos/`, served by the node at `/assets/tokens/<SYMBOL>.<ext>` (idempotent; `symbols`, `limit`, `force`) |

| `GET /api/policy` | **removed** — the window it described no longer exists; the privacy rule lives at `/api/privacy` |

**Balances are public data.** Every address, its TOR balance and every token
balance it holds is served without a session — that is what makes the supply
auditable:

```text
GET  /api/public/ledger                  -> every account + every token total
GET  /api/public/balances               -> the public balance sheet (?limit&offset)
GET  /api/public/balances/<address>     -> one address's balances
GET  /api/public/tokens/<SYMBOL>/holders -> every holder and its balance
GET  /api/account/<address>             -> same numbers, envelope-preserving
GET  /api/wallets/<name>/balance        -> public, no session required
```

Only **moving** value needs the wallet's key. `GET /api/wallets` (the operator's
keystore list) and the history / receive panel / send still require the signed
session:

```text
POST /api/wallets/founder/challenge     -> {nonce, message, ttl_seconds: 300}
        sign `message` with the wallet's Ed25519 key (base58)
POST /api/wallets/founder/session       -> {session, header: x-wallet-session}
GET  /api/wallets/founder/history       -> 200 with x-wallet-session (403 without)
POST /api/wallets/founder/send          -> 200 with x-wallet-session (403 without)
```

`GET /api/wallets/<name>/history` returns the wallet's **whole** history, newest
first (`limit` bounds how many records are scanned and `truncated` says when that
bound was hit). Nothing is hidden past a date: the ledger has no date to hide
past. The old enforcement machinery — `oneness_tor/services/retention.py`,
`tools/data_policy.py`, `POST /api/policy/sweep`, `GET /api/policy`, the hourly
timer and the `--ttl-days` / `archive` flags on the node — was deleted with the
window it existed to serve.

One consequence worth stating: service data (AI token landing pages, workbench
files, previews, launcher metadata, invoices) is no longer deleted on a timer
either. If you want it gone, delete it deliberately.

## Tests and live proof

```powershell
python -m pytest -q                  # offline tests (testpaths=tests in pytest.ini)
python tools/check_pages.py          # 15 pages render + every inline script parses
python tools/audit_endpoints.py      # catalog ↔ routes ↔ prompt ↔ live GET probes
python tools/live_check.py --spend   # live checks against the running node + API
python tools/live_check.py --ai gemma3:1b
python tools/live_meme_nft_check.py --model gemma3:1b   # meme launcher + AI-NFT, live
```

`tools/audit_endpoints.py` is the completeness gate: it fails if a Flask `/api`
route is missing from the catalog (or the reverse), if the Ollama system prompt
omits an endpoint or its return shape, if the `fetch_oneness_data` enum or the
OpenAPI document drift, and if any GET endpoint stops answering through the tool.
`--static` runs the offline half only.

The live check broadcasts a real transfer, pays an invoice, compiles + deploys an
escrow for 5 TOR and calls it — and fails if any step does not answer.
`live_meme_nft_check.py` creates a token (5 TOR), launches its pool (10 TOR),
buys against it and compares the on-chain reserve delta with the quote, buys a
trending slot (5 TOR), queues a volume session (10 TOR) and proves `run` answers
`501`, then mints an AI-NFT (3 TOR), loads its metadata and asks the model to
speak as it. The read-only `live_check.py` pass is **43 checks**, including the
public ledger (`/api/public/ledger`, `/api/public/balances`, `/api/public/totals`,
per-token holders), the token-logo metadata (`/api/tokens/images`), a logo source
on every token row, the absence of any fiat rate, the receive QR + public key,
the whole transaction history with hex, the IPFS gateway/`ipfs://` resolution, and the
developer-key price + the `x-api-key` gate on `/api/v1`.

## Honest limits

* **Single-proposer PoA**: `torchain` has no peer gossip yet; `/api/chain/sync`
  says so plainly and exposes the block-range endpoint peers will use, while
  `/api/chain/sync/audit` re-hashes every block it serves and probes configured
  peers — a green verdict means the hashes agree, not that a consensus protocol
  exists.
* **Contract execution**: this layer stores contract source + key/value state and
  settles fees on-chain. The Rust chain does not execute WASM.
* **Meme launcher**: `token_create` / `pool_create` / `swap` are real `torchain`
  transactions (the chain charges 5 TOR / 10 TOR itself, swaps pay 0.30% to the
  fee receiver). External DEX routes (`meme.dex_quote`, `meme.dex_swap`,
  Raydium/Jupiter venues) answer `501 DEX_NOT_DEPLOYED`; the volume bot charges
  its 10 TOR and stores the session but `run` answers `501
  ENGINE_NOT_DEPLOYED` — no fake trades, no fake candles.
* **NFTs**: the 3 TOR mint fee settles on-chain; the NFT record itself lives in
  `data/nft/nfts.json` because the Rust chain has no NFT model yet. Solana mints
  are read live (never copied) — set `ONENESS_SOLANA_RPC` if the public RPC is
  rate-limited.
* **Tor**: the control-port client (`ADD_ONION`, `NEWNYM`, cookie auth) is real,
  but Tor must run with `ControlPort 9051`; without it the API returns
  `TOR_DAEMON_UNAVAILABLE` instead of inventing an address.
* **Keys**: `data/wallets/*.json` holds plaintext secrets (the node's design).
  Username signup and the admin wallet generator keep the same keystore format,
  so the platform can sign for a logged-in user; the private key is returned
  once at signup and again only when a *new* admin wallet is generated.
  The API never serves them; only the owner should hold that folder. Balances are
  public ledger data (`GET /api/public/balances`, `/api/public/ledger`); only the
  transaction history, the receive panel and every send need the wallet session from the
  login at `/wallet` (`/api/wallets/<name>/challenge` → `/session` for a raw-key
  proof) or the operator's admin session.
* **Nothing expires**: the 30-day window, its sweep and the chain's private archive
  were removed with the per-transaction privacy rule that replaced them. Every
  record the ledger holds stays readable, and service data (landing pages, workbench
  files, previews, launcher metadata, invoices) is only deleted when you delete it.
  The one thing a sender can hide is its own address, never the receiver's.
* **Pinata gateway token** is embedded in the logo URL. Re-pin the file or set
  `TOR_LOGO_URL` before making the repo public.
* **No fiat rate on the node**: every price, balance, market cap and volume is
  denominated in **TOR** — the operator cannot type a USD rate and no USD figure is
  invented. Dollar values will come later from the on-chain DEX/marketplace oracles
  (trade volume, market cap, a price aggregator that refreshes every 15 s) once that
  layer exists; until then the API answers TOR only.
* **Speech** is the browser's: voices come from `speechSynthesis`, so the exact
  female/male voice depends on the machine (the pages name the one they picked),
  and the record button needs `SpeechRecognition` (Chrome/Edge). Browsers without
  it show that instead of a dead button that pretends to listen.
* **Token logos** are public metadata, not chain state (the ledger has no image
  field): `GET /api/tokens/images` resolves a ticker from CoinGecko's top coins by
  market cap, falls back to DexScreener, caches the map for 24 h in
  `data/metadata/token-images.json`, and every row reports its `image_source`. Each
  picture is then **mirrored onto this node** into `data/metadata/token-logos/` and
  served at `/assets/tokens/<SYMBOL>.<ext>`, which is what the pages draw — so a
  visitor over Tor, an offline box, a rate limit or a picture CoinGecko later removed
  can never blank a row. `POST /api/tokens/images/refresh` (admin) re-fetches and
  mirrors, `POST /api/tokens/images/mirror` (admin) or
  `python tools/mirror_logos.py` copies whatever is still missing, and every mint
  path (`token_fleet`, the launcher) calls `token_media.remember`, so a token minted
  later is mirrored exactly like the ones already on chain. A launcher's uploaded
  image wins, the raw URI stays in `image_uri` for
  provenance, TOR always shows the pinned IPFS logo, and when no source knows the
  ticker the page draws a deterministic identicon and says so.

## Layout

```
core/            config, pricing (+ the admin price store), validator, coin, node_client, api_catalog, tools, ollama, nft
oneness_tor/     app.py, cli.py (torctl), api/ (chain, wallet, services, fees, meme, nft, workbench, brain, pages), services/, web/
rust/torchain/   the ledger (accounts, tokens, pools, records, HTTP node)
data/            wallets, chain dirs, invoices, landing pages, contracts, meme, nft, workbench, tor, snapshots, admin (gitignored)
docs/            TOR_DEPLOY.md (Render → Tor Browser, replicas, push, fees), MARKET.md, PLATFORMS.md, TOKEN_FLEET.md
tests/           node, coin, catalog/tool, live API, NFT metadata/PDA, workbench + sync audit, push, fees, CLI, privacy protocol
tools/           signer, founder wallet, page checker, endpoint audit, live checkers
scripts/         start.py (the one start command), torctl.py, build_render.sh, origin_tunnel.ps1, run_all.ps1
```

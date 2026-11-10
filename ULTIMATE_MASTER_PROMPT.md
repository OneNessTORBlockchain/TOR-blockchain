# 🔷 ONE NESS TOR — ULTIMATE MASTER BUILD PROMPT

> **Read this file top to bottom, then read it AGAIN before you build.**
> This prompt was compiled from the full project conversation and reviewed twice. Every feature, every page, every pricing rule, every address rule, every test gate is inside. If you drop any part of Section 4 (the Ollama Command Interface), you have failed the mission.

---

## SECTION 0 — HOW TO USE THIS PROMPT

You are the lead blockchain architect and engineer for **OneNess TOR**. Work phase by phase (Section 12), show evidence at every gate (Section 11), and do not skip ahead. The project owner WILL paste your gate evidence back to you — if evidence is missing or fake, the build is rejected at that phase.

**Rules of engagement:**
1. **Build it, don't mock it.** Real nodes, real chain data, real HTTP APIs, real Tor daemon, real `.onion` addresses, real Ollama model calls. Never return fake JSON that "looks right."
2. **Test each feature** against the running backend, then **show the raw evidence** (terminal output with `[PASS]`/`[FAIL]` lines and real responses) from the running system.
3. **Show the real .onion URL** when you deploy the hidden service. Not an example string — a live address the user can paste into Tor Browser.
4. **Build step by step. No packaging, no Docker, no installers until the core works and passes all gates.**
5. If a decision is not specified here, choose the simplest robust option, implement it for real, and state the assumption in your phase report.
6. At the end of the build, re-check the Acceptance Checklist (Section 13) item by item, twice.

---

## SECTION 1 — THE CENTERPIECE (DO NOT FORGET THIS)

**OneNess TOR is an Ollama-powered AI command interface built on top of a real blockchain node.** People open the Command Deck and *ask the AI in natural language* to do anything the platform can do. The AI:

- **Discovers the live API schemas dynamically** — it introspects the node's own running API (`GET /api/schema`) at startup and on demand, so new endpoints are picked up without code changes.
- **Re-exposes every API call onto dynamic input fields** — every endpoint's required parameters are rendered as dynamic form fields in the Command Deck UI (generated from the live schema, never hardcoded).
- **Has a system prompt that sees all the requests and the request returns** — every API call the AI (or the user via the deck) makes, plus its real JSON response, is appended into the model's context so it reasons over actual results, not imagination.
- **Generates a real request from the user's natural language, sends it to the real running API, and delivers the real response to the user.** Full request pipeline, full response pipeline. **No mock data anywhere.**
- Handles tasks like: check a balance, show latest transactions, create an AI website, manage hosting, deploy a contract, mint tokens, issue an API key — **but always through verified live APIs that hit the blockchain.**
- Includes the **Command Deck in the admin dashboard**: live API call log (method, path, args, status, real response body), dynamic fields per endpoint, and a **Deploy button that triggers the Tor hidden service and returns the real onion address.**

This is the first thing you design the API layer around. Every capability of the platform must exist as a documented, schema-served, callable endpoint so the AI can drive it.

---
## SECTION 2 — MISSION

Build the complete functional full-stack blockchain node application:

| # | Surface | Requirement |
|---|---------|-------------|
| 1 | Python blockchain node core | Real chain, real blocks, real consensus, node-to-node sync |
| 2 | Smart contract engine | Rust contracts compiled safely to WASM; balances & all transactions live in contracts |
| 3 | Live API layer | Every function exposed as a real, documented, schema-served API |
| 4 | Ollama AI command interface | Natural language → live API → real results (Section 1) |
| 5 | Admin dashboard + Command Deck | Full control surface with live API call logs & deploy button |
| 6 | Wallets | Create/import, balances via contracts, send/receive |
| 7 | Payments | Verified on-chain TOR payments, receipts, subscription billing |
| 8 | Smart-contract management | List, inspect state, create (5 TOR), deploy (authorized nodes only) |
| 9 | AI website builder | 10 TOR to create → auto-deploy as Tor hidden service → real .onion |
| 10 | Tor hosting | 20 TOR per 30 days, on-chain verified subscriptions, per-site .onion |
| 11 | Live documentation portal | Real API calls from the docs page itself, "Try it" buttons hit the live node |
| 12 | Minter | Integrate the existing Python minter at **3 TOR per mint**, TOR address format |
| 13 | Node ops & privacy | per-transaction public/anonymous senders; nothing expires |
## SECTION 3 — REPO LAYOUT & STACK

```text
1Ness TOR Blockchain/
├─ oneness_tor/                  # Python backend (single deployable node)
│  ├─ core/                      # chain, blocks, consensus, keystore, storage
│  ├─ sync/                      # peer discovery, gossip, catchup, recovery
│  ├─ contracts/                 # WASM engine, built-in contracts, deployer auth
│  ├─ rust_contracts/            # Rust sources (cargo, wasm32-unknown-unknown)
│  ├─ api/                       # FastAPI live API layer + /api/schema
│  ├─ ollama_brain/              # schema introspection, prompt builder, tool loop
│  ├─ tor_service/               # stem-controlled Tor daemon, hidden services
│  ├─ builder/                   # AI website generator + hosting/subscriptions
│  ├─ minter/                    # INTEGRATE THE EXISTING PYTHON MINTER (3 TOR/mint)
│  └─ privacy/                   # public/anonymous per send, nothing expires
├─ web/                          # dashboard + all pages (pure HTML/CSS/JS, no build step)
├─ tools/                        # test harness: node A + node B spinners, gate scripts
└─ images/                       # brand assets (already in repo)
```

**Stack (locked):**
- **Python 3.11+** backend. **FastAPI + Uvicorn** for the live API. **httpx** for internal/outbound HTTP. **sqlite3** for chain/state/index storage. **stem** to control the real Tor daemon. **wasmtime-py** to execute Rust WASM contracts in a sandbox. **pydantic** models for every request/response. **cryptography** for keys and the encrypted private archive.
- **Rust** toolchain; contracts compiled to `wasm32-unknown-unknown`; compiled safely inside a sandbox with step limits and a blocked-imports list (Section 6).
- **Ollama** at `http://localhost:11434` (model configurable in `config.yaml`, default `llama3.1:8b`).
- **Frontend:** vanilla HTML/CSS/JS, no bundler. All pages share one design system (Section 10).

---

## SECTION 4 — BLOCKCHAIN CORE (BUILD FIRST)

### 4.1 Chain specification
- `chain_id`: `ONENESS-TOR-MAINNET` (override in `config.yaml`).
- Native coin: **TOR**. Internal fixed-point unit: 1 TOR = 10⁸ microTOR. All APIs accept/return decimal TOR strings with 8 decimals max.
- Hashing: SHA3-256 over canonical JSON. Signatures: Ed25519.

### 4.2 Genesis & founder wallet
- On first boot, generate `genesis.json`: timestamp, chain_id, proposer set = node's own pubkey (node #1 authorized by default), fee receiver = founder wallet, `native_coin_id`, price table (Section 8).
- Generate the **founder admin wallet** (Ed25519) on first boot, store encrypted in keystore, **print its address** to console and to `/api/admin/founder-wallet`.
- **Block 0 allocates exactly 50,000,000 TOR to the founder admin wallet** via the built-in balances contract. Nothing else is allocated at genesis; the only later supply changes come from the minter (3 TOR per mint) or per the price table.
- **Native coin identifier:** chain-level constant `TOR111111111111111111111111111111onion`. It is the *native coin identifier* — conceptually distinct from any wallet address and never a holder's account. The address validator must reject it as a transfer destination, and it must be served by `GET /api/chain/info` as `native_coin_id`.
- **Address format (all wallets, all token contracts):** `TOR1` + 30–48 chars from the base32 onion alphabet `[a-z2-7]`, ending in `onion`. Regex: `^TOR1[a-z2-7]{30,48}onion$`. One validator used by every API, page, contract, and the minter. **The minter must always mint token addresses in exactly this format.**
- The **two token address examples** (config `minter.token_addresses`, defaults below) exist so the minter never invents a different shape:
  - `TOR1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAonion`
  - `TOR1DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDonion`
  (If the owner's existing minter already has real token addresses, copy them into `config.yaml` — the format rule does not change.)

### 4.3 Blocks
- Header: `chain_id, height, prev_hash, timestamp, proposer, state_root, tx_root, block_hash` — signed by the proposer. `block_hash` = SHA3 over canonical header.
- Proposer emits a block when the mempool is non-empty and every 30s as a sync checkpoint (so empty blocks are rare but sync stays provable).

### 4.4 Consensus (Proof-of-Authority)
- Authorized node set is **itself stored on-chain in the `node_registry` contract** — every permission lives in a smart contract, not in a local file.
- Proposer for height H = `authorized_nodes[H % len(authorized_nodes)]`.
- Every node independently re-validates: proposer authorized at that height, signature valid, deterministic re-execution reproduces `state_root` + `tx_root`, and `prev_hash` matches. A block failing re-execution is rejected and the peer that sent it is penalized.
- Finality: simple and testable — a block is final when (ceil((n+1)/2)) authorized nodes hold it canonically; for a single node, self-final. Document exactly this in the docs portal.

### 4.5 Transactions = contract invocations
- Every transaction routes through a smart contract (transfers, fees, minting, hosting, websites, API keys, admin ops): `{sender, contract, method, args, nonce, signature, tx_hash}`.
- Mempool dedupes by `tx_hash`; nonce-per-sender anti-replay; invalid signature → rejected, not parked.

### 4.6 Storage & privacy (a product pillar — implement, don't describe)
- `chain.db` (SQLite, WAL): blocks, receipts, contract bytecode + state. **Permanent, private, full-fidelity.** Backed by state root per block; chain data is never destroyed.
- **No time window.** Earlier drafts filtered public surfaces on `exposed_at > now - 30 days`; that rule was implemented, then deleted on purpose. Nothing on this node expires on a timer and nothing is sealed into a hidden archive: the ledger keeps every record and serves it publicly.
- **Per-transaction privacy instead** (`oneness-tor/privacy/1`): before every send the wallet asks **Public** (its own address is on the record) or **Anonymous** (`Tor-Anonymous-onion` on the record). The choice rides in the transaction's `external_ref`, so it needs no new struct field, no state-root change and no block-hash change.
- **Only the sender's own address can be hidden.** The receiver always stays on the record: a payment has to name the wallet that receives it, and that wallet's owner never chose to be hidden. There is no recipient-masking switch.
- **Implemented surface (this repo):** `GET /api/tokens/public` and the public balance sheet are the public record — per token `symbol`, `name`, `mint_address`, `owner`, `supply`, `decimals`, `price_tor`, `pools` (reserves), `liquidity_tor`, `market_cap_tor`, `volume_24h_tor`, `volume_24h_swaps`, `updated_at`. `GET /api/privacy` publishes the per-transaction rule (what it hides, what it cannot). `oneness_tor.services.retention`, `tools/data_policy.py`, `GET /api/policy` and `POST /api/policy/sweep` were removed with the window.
- **Balances are public:** the ledger is auditable — `GET /api/public/balances`, `/api/public/ledger`, `/api/public/totals` and `/api/public/tokens/<SYMBOL>/holders` answer anyone, and `GET /api/account/<address>` follows the same rule. `GET /api/wallets` (the keystore list) still needs the admin token, and every write — send, sign, receive, the history — needs a signed session (`POST /api/wallets/<name>/challenge` → sign with the wallet's Ed25519 key → `POST /api/wallets/<name>/session` → `x-wallet-session`), or the admin token.

---

## SECTION 5 — NODE SYNCHRONIZATION (TWO REAL NODES, REAL PEERS)

This is a hard gate: **one Python process and one copy of the chain is NOT a multi-node blockchain.** Build and prove real node-to-node sync:

- Node config: `peers: ["http://127.0.0.1:8546"]` style list in `config.yaml`; each node has its own data dir and HTTP port (A=8545, B=8546 in tests).
- **Discovery & handshake:** on boot, GET `/api/node/handshake` (chain_id, height, head hash, genesis hash, protocol_version). Mismatched genesis or chain_id → refuse to sync and log it.
- **Catchup:** if a peer is ahead, pull missing blocks by ranges via `/api/chain/blocks?from=&to=`, **independently re-execute every historical transaction** to rebuild state at each height, verify `state_root`, and only then commit. Never trust a peer's state.
- **Live gossip:** after handshake, peers exchange new txs and new block announcements (`POST /api/node/broadcast/tx`, `POST /api/node/broadcast/block`). Proposer builds; non-proposers validate + append + re-broadcast.
- **Reorg policy:** on conflicting tips, nodes follow the valid chain with the most committed authorized votes; log a `REORG` event and emit it on `/api/chain/events` (SSE).
- **Recovery:** node B may be stopped for hours, restarted, and must catch up fully by re-execution (gate G4 includes restart-after-gap).
- Tools: `tools/spin_two_nodes.py` boots node A + node B with clean dirs in one command; `tools/send_tx.py` submits a signed transfer from either node. These exist to make the gates one-line runnable.

---

## SECTION 6 — SMART CONTRACT ENGINE (WASM, SAFE, TOTAL)

**Everything is a smart contract.** Balances, fees, deployment permission, node authorization, hosting subscriptions, AI-website ownership, API keys: all live in contract state. The node core is a small deterministic virtual host.

### 6.1 Engine
- `wasmtime-py` with: fuel metering (hard step limit per call), no I/O, no time, no randomness in guest code (host provides a deterministic block timestamp/height source), no filesystem, no networking.
- Blocked/absent imports: guest WASM may import ONLY the host ABI below. Anything else → contract rejected at deploy.
- Host ABI (every function callable from any contract; calls are logged): `log(text)`, `block_height()`, `block_timestamp()`, `env_var(key)`, `read_storage(key) -> bytes`, `write_storage(key, bytes)`, `get_coin_balance(account) -> u128`, `add_coin_balance(account, delta)` (system-contract-only authority enforced host-side), `emit_event(topic, data)` (flows into receipts + SSE), `sha3(bytes)`, and typed ABI helpers (i64/f64/varint) for stable storage encoding.

### 6.2 Built-in contracts (shipped with the node in Python → compiled once to WASM, or Python-host mirror with identical ABI; state ALWAYS lives in `read/write_storage` keyed by contract id)
- `tor_balances` — coin balances, the native coin `TOR111111111111111111111111111111onion`, transfers (+fee routing to fee receiver), minter-only `mint()` gated on authorized minter nodes; genesis allocates 50,000,000 TOR here.
- `node_registry` — authorized proposers (PoA set), authorized deployers (Section 8), authorized minters. Admin mutations only by founder/admin signatures.
- `site_registry` — created sites: owner wallet, generated HTML content hash, created_at, hosting expiry, onion mapping.
- `hosting_subscriptions` — 20 TOR / 30 days: `subscribe(site, owner)`, expiry ledger, `is_active(site, now)`, top-ups extend from current expiry (no stacking loss).
- `api_key_registry` — issued keys, key hash, owner, permissions scope, creation block; `revoke()`, `validate(key_hash)`.
- `admin_actions` — admin operations (fee receiver changes, ownership ops) recorded on-chain so admin activity is traceable.

### 6.3 Creating & deploying contracts
- **Creating a new contract: 5 TOR** (owner fee; listed in the UI price table). Creating = storing the Rust/WASM source + metadata on-chain (pending creation), not active in state.
- **Deploying (activating) a contract may ONLY be performed by an authorized deployer** (checked against `node_registry`). Unauthorized deploy attempts → HTTP 403 + on-chain deny event. The founder/admin account is authorized by default.
- Deploy path for user Rust contracts: `cargo build --target wasm32-unknown-unknown` inside the sandbox runner → validate guest imports against the ABI allowlist → size cap (e.g. 512 KB) → WASI disabled → install as `contracts/<address>` → activation recorded with deployer tx hash. Provide `rust_contracts/` with one example (`token_example`) proving the full create→deploy→call loop.

---

## SECTION 7 — LIVE API LAYER (THE CONTRACT WITH THE AI)

FastAPI app (`oneness_tor/api`), served by Uvicorn, real HTTP, real persistence, real errors. **The Ollama brain and every page consume ONLY these endpoints.**

### 7.1 Non-negotiables
- Every response JSON with consistent envelope: `{ok, data, error}`; errors carry `code`, `http_status`, `message`.
- **`GET /api/schema` serves the machine-readable schema of every endpoint** (path, method, summary, params with types/enums/required, examples, auth, pricing). Pydantic models generate it; the schema is ALSO written to `api_openapi.json` at boot. If an endpoint is not in `/api/schema`, the AI cannot call it — so EVERY feature must be registered here.
- CORS enabled for the dashboard origin; no auth for public read endpoints; `X-Api-Key` (checked against `api_key_registry`) or admin token for protected endpoints: config `ADMIN_TOKEN` in `config.yaml`.
- SSE at `/api/chain/events` for blocks/txs/mints/deploys/onion-updates so pages and the Command Deck can stream live updates.
Rate limiting: sane in-process limiter (e.g. token bucket per IP/API key, config), applied to public endpoints; document in schema (`rate_limit` field).

### 7.2 Endpoint groups (every one real; nothing stubbed)
- **Chain:** `GET /api/chain/info` (height, head, genesis_hash, native_coin_id, peers), `GET /api/chain/block/{height|hash}`, `GET /api/chain/blocks?from=&to=`, `GET /api/chain/tx/{tx_hash}`, `POST /api/chain/tx` (signed tx JSON), `GET /api/chain/pending`, `GET /api/chain/events`.
- **Wallets:** `POST /api/wallet/create` → real keypair + address; `POST /api/wallet/import` (key + passphrase → keystore); `GET /api/wallet/balance/{address}` (reads `tor_balances`); `POST /api/wallet/transfer` (signs + submits); `GET /api/wallet/history/{address}` (30-day rule applies).
- **Contracts:** `GET /api/contracts` (list w/ 30-day rule), `GET /api/contracts/{address}` + state read, `POST /api/contracts/create` (**5 TOR**), `POST /api/contracts/deploy` (authorized-deployer check), `POST /api/contracts/call`, `GET /api/contracts/events`.
- **Minter:** `POST /api/mint` → body `{to, token_address, amount, signature…}` → 3 TOR fee, validator-enforced TOR1…onion, receipt with tx hash; `GET /api/mint/history`; `GET /api/mint/status`.
- **Websites & hosting:** `POST /api/dev/ai-websites` (**10 TOR** → AI generation → auto-deploy Tor hidden service → real .onion), `POST /api/hosting/buy` (20 TOR / 30 days), `GET /api/hosting/sites`, `GET /api/site/{site_id}` (serve HTML), admin `POST /api/hosting/deploy` (the Command Deck deploy button).
- **Keys:** `POST /api/keys/issue`, `GET /api/keys/list`, `POST /api/keys/revoke`.
- **Docs:** `GET /api/docs` (machine list of endpoints + try-it metadata — this powers the docs portal's live "Try it" feature).
- **Node ops:** `POST /api/node/handshake` (GET), broadcast tx/block, `GET /api/node/status` (sync state, peers, public/expired counters).
- **Admin:** founder-wallet, the privacy rule, price-table read/update.

Every endpoint: Pydantic request/response models, input validation (addresses via the validator), unit price visible in docs and UI, receipts written with `exposed_at` for the 30-day rule.

---

## SECTION 8 — TOKENOMICS, ADDRESSES & LOCKED PRICING

### 8.1 Native coin & identity
- Native coin: **TOR** (1 TOR = 10⁸ microTOR). `native_coin_id`: `TOR111111111111111111111111111111onion` — surface it in `/api/chain/info`, docs, and the wallet page; reject it as a transfer destination.
- All addresses (wallets, token contracts, minted tokens) validate as `^TOR1[a-z2-7]{30,48}onion$`. Minted tokens ALWAYS use this format. Token address examples live in config (`minter.token_addresses`) with defaults `TOR1AAAA…onion` / `TOR1DDDD…onion` as specified in Section 4.2.

### 8.2 Supply
- Genesis: 50,000,000 TOR to founder admin wallet only. No block rewards. Supply growth only via mints (3 TOR fee) and config-defined emission sources, all recorded in `tor_balances` state; total supply is a queryable value (`GET /api/chain/info.total_supply`).

### 8.3 Price table (LOCKED — single source of truth `core/pricing.py`; displayed verbatim in Pricing & Services page and enforced by the fee router)
| Action | Fee | Collected by |
| --- | --- | --- |
| Create a smart contract | **5 TOR** | fee receiver (founder wallet default) |
| Mint tokens via integrated minter | **3 TOR / mint** | fee receiver |
| AI-generated website (create + first deploy) | **10 TOR** | fee receiver |
| Tor hidden-service hosting | **20 TOR / 30 days** | fee receiver |
| Coin/token transfer | **0.1 TOR** (config default) | fee receiver |
- Fees are charged by the fee router from the caller's `tor_balances` balance; insufficient balance → tx rejected with explicit `INSUFFICIENT_FEE` error.
- Pricing page + every confirmation modal pull these numbers from `GET /api/chain/info.price_table` — never hardcode them twice.

### 8.4 Permissions matrix (checked in `node_registry`, enforced host-side)
- Propose blocks: authorized proposers.
- Deploy contracts: authorized deployers (founder/admin default; UI shows badge).
- Mint tokens: authorized minters (the integrated minter's nodes).
- Admin ops (fee receiver changes, founder ops, replication import): admin/founder keys only.

---

## SECTION 9 — TOR, .ONION DEPLOYMENT, AI WEBSITES & HOSTING

### 9.1 Hidden service control plane — REAL, no mocks
- Node talks to the running `tor` daemon via **Stem** (`tor/control-port=9051`, cookie auth, SocksPort 9050). Missing Tor daemon → node still runs, but hosting features return explicit errors (`TOR_DAEMON_UNAVAILABLE`) — they never pretend to succeed.
- **`tor_service.create_hidden_service(port, key=None)`:** with the key provided it recreates the SAME `.onion`; without, it generates a new one. Secrets go to keystore, never to logs.
- Every successful website/hosting deploy **returns and prints the live `.onion` address** and registers it in `site_registry` (owner, content hash, expiry). The Command Deck shows the onion address as soon as Tor confirms the descriptor is published.

### 9.2 AI website builder (the 10 TOR flow, end to end)
1. User describes the site in the Webs page (natural language) → `POST /api/dev/ai-websites {prompt, style, pages}`.
2. Build prompt → Ollama generates **pure static HTML/CSS/JS** (single-file or few files, no external CDNs — sites are served from a Tor hidden service and must render offline-safe), validated: no script injection from templates, size cap (e.g. 2 MB), assets inlined.
3. 10 TOR charged → contract creation record in `site_registry` (content hash immutable) → Tor hidden service deployed → `.onion` returned in the API response and rendered in the UI.
4. Sites served by the node's static server only while hosting subscription active (`hosting_subscriptions.is_active`); expired hosting → onion stops being served, with a clear, honest error page (site data retained, service paused).

### 9.3 Hosting subscription (20 TOR / 30 days)
- `POST /api/hosting/buy {site_id, owner_signature}` → fee charged → expiry = max(now, current expiry) + 30 days → receipt + SSE event.
- Status page shows days remaining per site; auto-pause at expiry; renewals extend, never lose paid time.
- Admin deploy (`POST /api/hosting/deploy`) is what the Command Deck button calls; it may (re)deploy the hidden service for an owned site and reports onion + descriptor status.

### 9.4 Serving stack
- One node process serves: dashboard (localhost only), public portal pages, `/api/*`, AI-site hosting (through Tor SocksPort/hidden-service ports), docs portal. Tor traffic never mixes with dashboard traffic: hidden-service listeners map only to site content, never to the admin/API surface.

### 9.5 AI-generated assets inside the system (real model output, stored)
- The websites page, logos and images for sites: if the AI generates text or SVG/HTML/CSS, it is saved to disk (`web/assets/sites/<site-id>/`) and referenced by real files. Do not paste base64 blobs into pages.

---
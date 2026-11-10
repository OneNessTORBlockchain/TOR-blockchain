# Deploying OneNess TOR on Render — and browsing it in Tor Browser

One Render web service runs the whole node: the API, the pages, the Rust ledger,
the Tor daemon and its hidden service. Nothing else to click, no sidecar, no
manual `ADD_ONION` over SSH — and the same repository can run node 2 and node 3 as
verified replicas that receive the ledger snapshot automatically.

Everything below is what the code actually does. Where a step needs a secret
(the onion key), it says where to read it, never what it is.

---

## 1. What the deploy does by itself

`render.yaml` → one Docker service, start command `python scripts/start.py`:

| Step | What runs | Where to see it |
| --- | --- | --- |
| Rust ledger | `torchain serve` on `127.0.0.1:8545`, seeded from `seed/chain-snapshot.json` on a fresh container | `/api/health` → `node_reachable`, `node_runtime` |
| API + pages | Flask on `0.0.0.0:$PORT` | `/`, `/admin`, `/docs` |
| Tor daemon | `tor -f data/tor/torrc` (SOCKS 9050 + ControlPort 9051, cookie auth), started by the node when `ONENESS_TOR_AUTOSTART=1` | `/api/tor/status`, deploy log line `tor -> ...` |
| Hidden service | `ADD_ONION` for the API port at boot (`ONENESS_TOR_PUBLISH_NODE=1`) | `/api/health` → `onion`; deploy log `onion -> http://….onion` |
| Snapshot push | export → verify → save `data/snapshots/latest.json` → POST to every `ONENESS_SYNC_PUSH_TARGETS` replica | `/admin` → Node synchronization, `GET /api/chain/sync/push` |
| Fee schedule | `data/admin/pricing.json`, editable from `/admin` → Service fees | `GET /api/pricing`, `GET /api/admin/fees` |

The image installs `tor` (see the `Dockerfile`), so a Render Docker service needs
no extra build step. A **native** Python service does: use
`bash scripts/build_render.sh` as the build command (it compiles the Rust ledger)
and install Tor in the same step, or let the node do it — `ONENESS_TOR_AUTO_INSTALL=1`
runs the host's package manager at boot (`apt-get`, `apk`, `dnf`, `yum`, `brew`) and
prints exactly what ran:

```bash
python scripts/torctl.py install            # same call, from a shell
python scripts/torctl.py install --start    # …and start the daemon afterwards
```

On Windows there is no package manager to drive: unpack the **Tor expert bundle**
into `data/tor/runtime/tor` (the `tor.exe` inside it is run as-is) or set
`ONENESS_TOR_BINARY` to the `tor.exe` path.

---

## 2. First deploy (5 minutes)

1. **Push the repository** (private), then in Render: *New → Blueprint* → pick the
   repo. `render.yaml` defines the service.
2. **Set the secrets** in *Environment* (all optional; the node boots without
   them, it just cannot do those things):

   | Key | What it enables |
   | --- | --- |
   | `ONENESS_ADMIN_TOKEN` | **optional.** Chain-level actions (archive seal, fee collector) already work: the node creates `data/admin/chain_token` on first boot and writes its sha3 into the ledger. Set this only to pin the token you already have |
   | `ONENESS_OLLAMA_API_KEY` | the hosted brain at `ollama.com` (free plan has no RAM for a local model) |
   | `ONENESS_TOR_ONION_KEY` | keeps the **same** onion address across redeploys — `.env` already carries one, see step 4 |
   | `ONENESS_SYNC_PUSH_TARGETS` | node 2 / node 3 replica URLs, comma separated — see step 5 |
   | `PINATA_JWT` | IPFS metadata for meme launches and AI-NFTs |

   `config.fleet.yaml` and `.env` are already wired for the operator's own
   origin, so a replica deploy needs almost nothing typed. `.env` also sets
   `ONENESS_TOR_AUTOSTART=1`, `ONENESS_TOR_PUBLISH_NODE=1`,
   `ONENESS_TOR_AUTO_INSTALL=1` and the onion key, so a plain
   *Deploy* publishes the same `.onion` with **no dashboard step at all** — that
   is the "push start and publish" the deployment is meant to be.

   One thing to check once: a dashboard `ONENESS_SYNC_ORIGIN` left over from the
   Cloudflare-tunnel era **wins over `config.fleet.yaml`** and produces
   `the origin https://….trycloudflare.com is not reachable`. Delete it (or set it
   to `https://www.torblockchain.fun`); the committed fleet file then decides.

   The admin token is **not** something you paste: a proposer creates its own in
   `data/admin/chain_token` (0600, git-ignored) and the start command adopts it
   before the ledger boots — a replica instead presents the **origin's** token,
   which its seeded ledger already accepts (the committed `config.fleet.yaml`
   decides, and never mints a second secret). Either way a signed-in admin at
   `/admin` is enough for chain-level actions. Copy the file to keep the same
   credential across hosts; `GET /api/admin/token` (admin) reports where it comes
   from, never the token itself.
3. **Deploy and read the log.** You should see, in this order:

   ```
   Rust node      -> http://127.0.0.1:8545 (started by this command …)
   tor            -> tor started by this node (detached, cookie auth) (control 9051 …)
   onion          -> http://<56 chars>.onion
   snapshot push  -> N target(s) every 60s (POST /api/chain/sync/push; copies in …)
   ```

   If the `onion` line says `not published`, it also says **why** (no `tor`
   binary, control port closed, `ADD_ONION` refused). Nothing is ever printed as
   an onion that does not answer.
4. **The address is already decided.** A Render container's disk is ephemeral: the
   key behind the address lives in `data/tor/services.json`, which a redeploy
   erases. `.env` therefore carries an `ONENESS_TOR_ONION_KEY`, and that key *is*
   the address — the same 64-byte `ED25519-V3` blob Tor returns from
   `ADD_ONION NEW:ED25519-V3`. Mint a new identity (and see its `.onion`) before
   any deploy, without a daemon, a disk or a Tor install:

   ```bash
   python -m oneness_tor.cli keygen        # or: python scripts/torctl.py keygen
   # onion          : http://<56 chars>.onion
   # ONENESS_TOR_ONION_KEY=<base64 blob>
   python -m oneness_tor.cli key           # the key on record + the address it owns
   ```

   Commit that blob (`.env`), put it in the dashboard, or leave the one that is
   already there. With it set, `ADD_ONION` re-creates the **same** `.onion` after
   every deploy; without it, each deploy mints a new address. When a daemon does
   publish, the log also cross-checks Tor's `ServiceID` against the address the key
   derives and says so loudly if they disagree (a mis-copied blob then cannot
   silently produce an address no redeploy would reproduce).

   **One key = one address = one service.** Every deploy that shares the committed
   key answers the same `.onion`; if node 2 or node 3 must be browsable separately,
   give it its own (`torctl keygen` → that service's `ONENESS_TOR_ONION_KEY`).
5. **Point the replicas at it** (node 2, node 3): see the next section.
---

## 3. Browsing it in Tor Browser

1. Open **Tor Browser** (never a normal browser: `.onion` does not resolve on the
   public DNS).
2. Paste the address from `/api/health` → `onion`, or from the deploy log:

   ```
   http://<56 chars>.onion/
   ```

3. If the page loads but the buttons do nothing, the security level is blocking
   scripts — click the shield → **Standard** (or "Allow scripts on this site"),
   then reload with Ctrl+Shift+R. The page prints height, chain id, supply and Tor
   state into the HTML as it is served, so it reads correctly even with scripts
   off (that is the point of the `noscript` block in `base.html`).
4. Check the JSON directly — the same numbers, no JavaScript:

   ```
   http://<id>.onion/api/health          {"ok":true,"data":{"status":"ok","onion":"http://…onion",…}}
   http://<id>.onion/api/chain/info
   http://<id>.onion/api/pricing         (the live fee table)
   ```

5. From a terminal through Tor (optional):

   ```bash
   torsocks curl -s http://<id>.onion/api/health | jq .data.status
   ```

**Why this works on Render without opening a port:** a hidden service makes
*outbound* connections to the rendezvous points, so it needs no inbound firewall
rule and no public hostname. The Render URL and the onion URL serve the same
process; the onion is the one that does not leak the host.

---

## 4. Node 2 and node 3 (verified replicas)

One proposer, many verified mirrors. A replica keeps its **own** admin token,
stays read-only for new blocks, and only adopts a snapshot after re-hashing every
block and the state root in Python.

**The committed wiring does this without a click.** `config.fleet.yaml` carries

```yaml
origin:
  url: https://www.torblockchain.fun      # the live proposer, not a tunnel
  token: <its chain admin token>
  admin_token_sha256: <sha3-256 of that token>
```

so every deploy from this repository mirrors `https://www.torblockchain.fun` from
its first boot — nothing to type, and a dead quick-tunnel hostname cannot come
back (`ONENESS_SYNC_ORIGIN` in `.env` used to pin one, and an env var wins over
the fleet file). Two extra rules in `oneness_tor/services/node_sync.py` keep it
honest:

* a node that **holds** that admin token is the origin and never pulls from itself;
* a node whose **own public host** is `origin.url` is the origin too — from
  `ONENESS_PUBLIC_BASE_URL` (pin your domain) or the `RENDER_EXTERNAL_URL` Render
  injects. Without that, the proposer would read the committed file, see its own
  URL, and demote itself to a read-only replica of itself.

`GET /api/chain/sync` → `fleet.origin_is_this_node` and
`fleet.origin_is_this_node_by` say which rule fired (`admin token`,
`ONENESS_PUBLIC_BASE_URL`, `RENDER_EXTERNAL_URL`), and `fleet.self_hosts` lists the
hosts this process believes are its own.

**On a replica you want to pin by hand** (a second/third Render service, a laptop,
an onion-only box):

```yaml
# config.yaml (or the dashboard env vars)
sync:
  role: replica
  origin_url: https://<origin>.onrender.com   # or http://<id>.onion, with sync.proxy
  origin_token: <origin chain admin token>
  auto: true
  interval_seconds: 60
```

```bash
ONENESS_NODE_ROLE=replica
ONENESS_SYNC_ORIGIN=https://<origin>.onrender.com
ONENESS_SYNC_TOKEN=<origin chain admin token>
```

`config.fleet.yaml` already knows which node holds the origin's credential: a
node that does **not** hold it is demoted to replica even if a stale
`ONENESS_NODE_ROLE=origin` is still set. That is what stops a mirror from
"syncing with itself" and never catching up.

**Verify a replica is really current** (it must be, or it says so):

```bash
curl -s https://<replica>/api/chain/sync  | jq '.data.role, .data.local_height'
curl -s https://<replica>/api/chain/sync  | jq '.data.replication.last_pull'
curl -s https://<node2>/api/chain/info | jq '.data.height, .data.head_hash'
curl -s https://<node3>/api/chain/info | jq '.data.height, .data.head_hash'
```

All three heights and head hashes must agree. If they do not, the origin has not
pushed yet (see the next section) or the replica is a different genesis.


---

## 5. Automated snapshot push — no code, no clicking

On the **origin** only:

```yaml
sync:
  push:
    enabled: true
    interval_seconds: 60                  # 0 = follow sync.interval_seconds
    save: true                            # data/snapshots/latest.json + chain-<height>.json
    keep: 5
    targets:
      - url: https://oneness-tor-node2.onrender.com
        label: node 2
      - url: https://oneness-tor-node3.onrender.com
        label: node 3
        # token: <that replica's admin token, when it has its own>
```

or, without touching a file (that is the "no code" path):

```
ONENESS_SYNC_PUSH_TARGETS=https://oneness-tor-node2.onrender.com,https://oneness-tor-node3.onrender.com
ONENESS_SYNC_PUSH_INTERVAL=60
ONENESS_SYNC_PUSH_KEEP=5
```

What happens every interval, with no further human action:

1. the origin asks its own Rust node for the whole ledger (`/api/admin/sync/export`),
2. re-hashes every block and the state root (the same check a pull does),
3. writes `data/snapshots/chain-<height>.json` and overwrites `latest.json`,
   pruning all but `keep` copies,
4. `POST /api/chain/sync/accept` on each target with the snapshot in the body,
5. each target re-hashes it and imports it into **its** `state.json` +
   `blocks.jsonl` atomically — refusing to move backwards,
6. the per-target verdict lands in `GET /api/chain/sync/push` (`last_push`) and in
   `/admin` → Node synchronization.

Manual equivalents (same code path):

```bash
python -m oneness_tor.cli sync --push                 # push now
python -m oneness_tor.cli sync --push --targets https://node2.example,https://node3.example
python -m oneness_tor.cli sync                        # status + last push verdict
python -m oneness_tor.cli snapshot                    # verify + save locally
curl -sX POST http://127.0.0.1:8090/api/chain/sync/push -H "x-admin-token: $TOKEN" | jq .data
```

A replica never pushes (it would fight the origin): the call answers
`409 SYNC_READ_ONLY` with the reason.

**The snapshot on disk is a backup, not decoration.** A wiped Render container
re-seeds from it — no re-export, no manual step:

```
ONENESS_NODE_SEED=/app/data/snapshots/latest.json   # or an https URL to it
```

If the free plan sleeps your service, the next push wakes it: the request is a
normal HTTPS POST and Render starts the container on it.

---

## 6. Admin panel: set every service price yourself

`/admin` → **Service fees**. One row per service, its published default, the live
price and who set it.

1. Sign in at `/admin` (username/password). Chain-level rows use this node's own
   token (`data/admin/chain_token`) — nothing to paste; an `x-admin-token` header
   only overrides it for that one call.
2. Type the new price in TOR and press **save prices**.
3. LOCKED Section 8 rows (`contract_create`, `mint`, `transfer`) need **confirm
   changes to LOCKED rows** — those numbers are published, so moving one is a
   deliberate act and is recorded.
4. **reset** (per row) or **reset all** restores the published table.

Under the hood it is one JSON store and one Python table:

```
POST   /api/admin/fees          {"prices": {"meme_banner_7d": 45}, "confirm_locked": false}
DELETE /api/admin/fees/<code>   reset one row
DELETE /api/admin/fees          reset every override
GET    /api/admin/fees          live prices + defaults + history
GET    /api/pricing             what pages and customers see (public)
```

The value is written to `data/admin/pricing.json` and read by `core.pricing` on
the next request, so the fee router, `/api/pricing`, `/payments`, the meme
launcher and the AI-NFT studio all charge the new price immediately — no restart,
no file to edit, no second copy to drift. `config.yaml` → `pricing.overrides`
still works for a value that must survive a wiped `data/` directory; the panel
wins over it. Everything is also in the CLI:

```bash
python -m oneness_tor.cli fees list
python -m oneness_tor.cli fees set meme_banner_7d 45
python -m oneness_tor.cli fees set contract_create 7 --confirm-locked
python -m oneness_tor.cli fees reset meme_banner_7d
```


---

## 7. The TOR CLI (`torctl`)

```bash
python -m oneness_tor.cli status        # tor, onion, role, push timer
python -m oneness_tor.cli start         # start the Tor daemon (writes data/tor/torrc)
python -m oneness_tor.cli publish       # ADD_ONION this API → prints the .onion
python -m oneness_tor.cli unpublish     # DEL_ONION it again
python -m oneness_tor.cli key           # ONENESS_TOR_ONION_KEY=… (persist this)
python -m oneness_tor.cli newnym        # new circuit identity
python -m oneness_tor.cli services      # every hidden service this node recorded
python -m oneness_tor.cli sync [--pull|--push]
python -m oneness_tor.cli snapshot
python -m oneness_tor.cli fees …        # list / set / reset
python -m oneness_tor.cli health --base http://127.0.0.1:8090
```

`--json` on any command prints the raw document (exit code non-zero on failure),
which is what a deploy hook or a cron job should use. `python scripts/torctl.py`
is the same thing from any working directory.

---

## 8. Honest limits

* **Free plan sleeps** after inactivity: the Render URL and the onion both go
  cold until the next request; a push or a page load wakes them.
* **Ephemeral disk** on Render: the ledger, the onion key, the price store **and
  the admin account** live in `data/`. Set `ONENESS_TOR_ONION_KEY`, keep
  `ONENESS_NODE_SEED` pointing at a snapshot and back up
  `data/admin/pricing.json` if you repriced. A free service has no disk, so every
  redeploy wipes `data/accounts/accounts.json` too: `GET /api/admin/account`
  then reports `registered: false`, `/admin` shows the register form again, and
  the fee panel answers `FORBIDDEN admin credential required` until somebody
  registers (or signs in on a service whose disk survives). **Registration is
  open whenever no account exists — on a public URL that means whoever registers
  first owns the operator panel**, so register immediately after a deploy that
  wiped it, or move `data/` to a disk (`ONENESS_DATA_DIR`) so it never wipes.
* **One proposer.** `torchain` is single-node proof-of-authority: replicas mirror,
  they do not vote. Multi-proposer gossip is not implemented, and no wording in
  the UI pretends otherwise.
* **A hidden service is not anonymity for the operator.** The onion hides the
  host, not the chain: every block, balance and record is public by design.
* **This node is not an exit relay** (`ClientOnly 1` in the generated torrc).
* **Fees are platform fees**, charged as signed TOR transfers to the fee receiver;
  the base layer charges no transaction fee.

---

## 9. Troubleshooting

| Symptom | Real cause | Fix |
| --- | --- | --- |
| Deploy log: `onion -> not published: tor is not installed` | native service without Tor | use the Dockerfile image, or `apt-get install tor` in the build step, or drop the expert bundle in `data/tor/runtime/tor` |
| Deploy log: `tor -> not started: … control port 127.0.0.1:9051 never answered` | the daemon died (bad torrc, no network) | read `data/tor/tor.log`; the error carries its last 400 characters |
| `onion` changes on every deploy | the key is not in the environment | `torctl key` → `ONENESS_TOR_ONION_KEY` |
| `/api/health` says `degraded`, every chain route 503 | the Rust ledger is not running | check `node_runtime` / `node_hint`; a fresh container re-seeds from `seed/chain-snapshot.json` |
| `push snapshot` reports `no target accepted the snapshot` | replica down, wrong token, or a different genesis | `GET /api/chain/sync` on the replica; the per-target `reason` says which |
| `.onion` does not load, Render URL does | Tor is not running (it dies with the container) | `POST /api/tor/start` or restart the service; the onion is re-added at boot |
| `PRICE_LOCKED` when saving a fee | LOCKED Section 8 row without the checkbox | tick **confirm changes to LOCKED rows** (or send `confirm_locked: true`) |


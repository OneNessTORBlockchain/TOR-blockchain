# The six TOR platforms

All six read the same node (`/api/market/*`, `/api/ads`, `/api/ai/*`), ship the
same client kit (`apps/tor-kit` → `src/tor`), and carry the same footer: the Tor
onion address, `www.onenesstorblockchain.com`, `www.onenesstech.ca` and
`support@onenesstech.ca`.

| # | Project | Folder | Dev URL | Stack |
| --- | --- | --- | --- | --- |
| 1 | TOR ONION SWAP | `C:\Users\KATHY\Downloads\zenith-main\app` | http://127.0.0.1:5173 | Vite + React + TS |
| 2 | TOR-SCREENER | `C:\Users\KATHY\Downloads\dexscreener-clone-main\dexscreener-clone` | http://127.0.0.1:5174 | Vite + React + TS |
| 3 | DARKWEB CRYPTO | `C:\Users\KATHY\Downloads\axiom-clone-main` | http://127.0.0.1:5175 | Vite + React + TS |
| 4 | TOR PUMP | `C:\Users\KATHY\Downloads\openpump-main\web` | http://127.0.0.1:5176 | Vite + React + TS |
| 5 | TOR CHARTS | `C:\Users\KATHY\Downloads\lightweight-charts-master\tor-charts` | http://127.0.0.1:5177 | Vite + React + TS |
| 6 | SOLARIX ENGINE | `C:\Users\KATHY\Downloads\Solarix-main\dashboard` | http://127.0.0.1:5178 | Vite + React + TS |

The node lists the same table at `GET /api/projects`, including each project's
logo (`/assets/images/projects/<id>.svg`), its onion URL and its clearnet URL.

## Run them

```powershell
# node (API on 8090 + the Rust ledger on 8599)
cd 'C:\Users\KATHY\Videos\1Ness TOR Blockchain'
$env:ONENESS_NODE_API='http://127.0.0.1:8599'
python -m oneness_tor --port 8090 --no-ollama --no-sweep

# any app
cd 'C:\Users\KATHY\Downloads\zenith-main\app'
npm install
npm run dev        # or: npm run build && npm run preview
```

Point an app at another node with `VITE_TOR_API` (see each `.env.example`).

## What each one does

**1 · TOR ONION SWAP** — buy/sell on any of the 170 pools (TOR or USDC quote),
quote preview before signing, a liquidity board plus a real `pool_create` form,
portfolio and a full wallet (create/sign in, balance, receive QR, send, export
and import private keys), token and AI-NFT listings with their logos, the
AI-NFT ChatBox and the API docs page.

**2 · TOR-SCREENER** — the board (search, TOR/USDC filter, sort by volume,
liquidity, change or cap), one page per token with candles, both pools and a
generated profile, banner advertising at **50 TOR / week** in the permanent
rotation, and the **100 TOR / 30 days** developer-key documentation.

**3 · DARKWEB CRYPTO** — the same board on the dark palette, plus the vault: both
fee receivers (`Tor AI-NFT Wallet` and the fee receiver), the pump platform
wallet and the USDC custody wallet, and the caller's own API keys.

**4 · TOR PUMP** — free mints (the platform funds the chain fee), a $5 000
platform-owned pool per launch, the creator's first buy, a `$25 000` bonding
curve with progress bars, creator-fee claims, buy/sell on the pool, banners.

**5 · TOR CHARTS** — the candlestick generator: a featured chart, a gallery of
every listing, and the two-snippet integration guide (`GET /api/market/chart/…`
→ `<CandleChart/>`). The component is pure SVG and is copied into the screener,
DarkWeb and swap by the kit.

**6 · SOLARIX ENGINE** — TOR at $3.60 with the drift it applied, chain height,
pools, liquidity, volume, movers, newest listings and the platforms reading the
engine, plus the full token table and the model documentation.

## Shared kit

`apps/tor-kit` is copied into each app's `src/tor` by
`python tools/scaffold_apps.py`:

| File | Role |
| --- | --- |
| `api.ts` | envelope-aware client, `x-wallet-session`, typed market shapes |
| `brand.ts` | brand constants, `money/compact/pct/tor`, `asset()` |
| `ui.tsx` | shell, nav, footer (onion + clearnet + support), banner rail, token logo, toasts |
| `wallet.tsx` | sign-in/sign-up, balance, send, receive QR, key export/import |
| `chart.tsx` | `CandleChart` + `Sparkline`, pure SVG |
| `chat.tsx` | AI-NFT ChatBox with the free-model dropdown |
| `styles.css` | the dark 3D neon design system; each app overrides `--accent` |

Re-scaffold after changing the kit:

```powershell
python tools\scaffold_apps.py --force      # rewrites shells + copies the kit
```

## Logos

`python tools/make_project_logos.py` writes each 3D-neon SVG three times: the
node's `images/projects/`, the app's `public/logo.svg` and `public/favicon.svg`.
Re-run it after adding a project to `oneness_tor/services/projects.py`.

## Notes

* The starter repositories keep their original sources under `src.legacy`
  (Solarix keeps its Rust crate; only the new `dashboard/` is JavaScript).
* `dexscreener-clone` and `axiom-clone` moved from CRA/Next to Vite so all six
  apps build with one toolchain and one `npm run build`.
* Nothing in an app talks to OpenRouter directly: the ChatBox always calls the
  node, so the API key never reaches a browser or a Tor circuit.

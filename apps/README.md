# `apps/` — the shared kit and a copy of each platform page

* **`tor-kit/`** — the single source of truth for the client every TOR platform
  imports: `api.ts`, `brand.ts`, `ui.tsx`, `wallet.tsx`, `chart.tsx`,
  `chat.tsx`, `styles.css`. `python tools/scaffold_apps.py --force` copies it
  into each app's `src/tor`.
* **`<project-id>/App.tsx`** — the page for that platform, copied from its live
  folder by `python tools/snapshot_apps.py`. The apps themselves live in the
  starter repositories the operator downloaded; see
  [`docs/PLATFORMS.md`](../docs/PLATFORMS.md) for the exact folders and ports.

| id | platform | live folder | port |
| --- | --- | --- | --- |
| `tor-onion-swap` | TOR ONION SWAP | `…\zenith-main\app` | 5173 |
| `tor-screener` | TOR-SCREENER | `…\dexscreener-clone-main\dexscreener-clone` | 5174 |
| `darkweb-crypto` | DARKWEB CRYPTO | `…\axiom-clone-main` | 5175 |
| `tor-pump` | TOR PUMP | `…\openpump-main\web` | 5176 |
| `tor-charts` | TOR CHARTS | `…\lightweight-charts-master\tor-charts` | 5177 |
| `solarix-engine` | SOLARIX ENGINE | `…\Solarix-main\dashboard` | 5178 |

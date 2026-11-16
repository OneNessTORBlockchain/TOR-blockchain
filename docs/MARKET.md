# TOR price & market-cap engine

The engine answers one question for every platform: **what is this token worth
right now, and where did that number come from?** It lives in
`oneness_tor/services/market_engine.py`, reads the Rust ledger, and is served at
`/api/market/*`.

## The rules

| Number | Rule | Source |
| --- | --- | --- |
| TOR price | one tick every 30 s: a random step of $0.10–$0.50, the direction biased by activity, clamped to $3.00–$100.00 | `oneness_tor/services/tor_price.py` (`data/market/tor-price.json`) |
| TOR anchor | where a cold node starts ($3.60) | `pricing.tor_usd` in `config.yaml` |
| Activity | 60 % 24 h TOR volume + 40 % swap count, scored 0–1 | `/api/public/records` |
| Engine publish | `POST /api/market/tor/price` — the Marketcap & Price Engine's quote wins while it is fresh (60 s) | `oneness_tor/api/market.py` |
| Token price | the pool's own reserve ratio | `tor_reserve / token_reserve` on chain, or the USDC book's `usdc_reserve / token_reserve` |
| Market cap | `price × on-chain supply` | `token.supply` from the ledger |
| Liquidity | quote-side reserve × 2 | pool or book |
| 24 h volume | sum of real swap records the ledger holds (nothing expires) | `/api/public/records` |
| Candles | one per sampled price (`market.sample_seconds`, default 30 s — the tick) | `data/market/history.json` |

The old turnover drift (`0.35 × volume ÷ liquidity`, ±15 %) is still reported as
`drift` for older clients, but it no longer sets the price. The 30 s walk is the
volatility rule: step **$0.10–$0.50**, floor **$3.00**, cap **$100.00**, and the
published engine quote is authoritative until the next tick.

Nothing is imported from an oracle. When both a TOR pool and a USDC book exist,
the deeper one sets the price and the other is listed beside it
(`pools_usd` in every row).

A token with one observation charts as a flat line and says so —
`source: "one observation — flat at the pool price"`. That is deliberate: an
invented candle would be a lie about liquidity.

## Endpoints

| Endpoint | Answer |
| --- | --- |
| `GET /api/market/overview` | TOR, totals, movers, newest listings, deepest pools |
| `GET /api/market/tokens` | every token priced in USD |
| `GET /api/market/tokens/{symbol}` | one token with both pools and 24 h change |
| `GET /api/market/tor` | TOR alone: the live 30 s quote, its tick, band and activity |
| `GET /api/market/tor/price` | the bare TOR/USD quote (no board built) |
| `POST /api/market/tor/price` | the Marketcap & Price Engine publishes the authoritative quote (engine/admin credential; outside $3–$100 is 400) |
| `GET /api/market/chart/{symbol}?points=168` | candles + summary + source |
| `GET /api/market/pools?base=TOR` | 120 TOR pools and 50 USDC books, deepest first |
| `GET /api/market/quote?symbol=&side=&amount=&base=` | constant-product quote |
| `POST /api/market/swap` | settle a fill (wallet session) |

## The pools

`tools/seed_market.py` created them, all with real transactions:

```
python tools/seed_market.py plan        # prints the ladder before touching anything
python tools/seed_market.py wallets     # platform wallets + fee receiver
python tools/seed_market.py tor-pools   # 120 chain pools
python tools/seed_market.py usdc-books  # 50 node books
python tools/seed_market.py trades      # first real swaps (volume + charts)
```

* **120 TOR pools — $20 000 000 of quote-side liquidity**, deepest $1 000 000,
  shallowest $10 000. Each is a chain `pool_create` signed by the wallet that
  owns the token; the TOR side plus the chain's 10 TOR fee is funded to that
  wallet first.
* **50 USDC books — $5 000 000**, $250 000 down to $10 000. The Rust ledger has
  one pool shape (`tor_reserve`/`token_reserve`), so a USDC pair is a *book*:
  reserves are custodied by `swap-usdc-pool` and every fill settles as **two
  signed transfers** (token in → custody, USDC out → trader). The price is the
  book's own ratio and is labelled `constant product on the book's reserves
  (node)`.

Pool keys are per ticker on the chain, so a ticker can have a TOR pool and a
USDC book at once — exactly what `/api/market/pools` reports.

## Banners and the AI desk

* `GET /api/ads` — slot 0 is the permanent 1Ness TOR Blockchain banner
  (`images/1Ness TOR Blockchain banner.png`); paid slots run 7 days for **50 TOR**
  and rotate in a synchronous loop on every page.
* `POST /api/ads` — buys a slot from the caller's wallet (`meme.promote` kind
  `banner`), then attaches the headline/image/link.
* `GET /api/ai/models` — **free** OpenRouter models only (text output, no audio),
  cached 10 minutes; `source: "openrouter"` or `"bundled"` when unreachable.
* `POST /api/ai/chat` — the AI-NFT ChatBox: the key stays on the node, the TOR
  persona is prepended, `POST` needs no session (it is a read-only completion).

## TOR Pump

`oneness_tor/services/pump.py` implements the launchpad on top of the same
primitives:

1. the platform funds the chain's 5 TOR mint fee **to the creator's wallet**,
   so the mint is free for the creator;
2. the creator's `token_create` mints the supply;
3. the platform takes its share of the supply, funds its own wallet with
   `$5 000 ÷ TOR price`, and signs `pool_create` — **the platform owns the pool**;
4. the creator's first buy is a real `swap` with the creator's own TOR;
5. the curve is `market cap ÷ $25 000`; creator fees are 20% of the pool's 0.30%
   fee computed from real volume, paid as a signed transfer on claim.

Because the pool exists from block one, every launch is already on TOR-Screener
and DarkWeb Crypto — they read `/api/market/pools`.

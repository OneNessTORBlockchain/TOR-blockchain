/** TOR-SCREENER — listings, charts, auto profiles, banners and the API shop. */
import { useEffect, useMemo, useState } from "react";
import { api } from "./tor/api";
import type { Token } from "./tor/api";
import { BRAND, compact, money, pct, relative, shortAddress } from "./tor/brand";
import { CandleChart } from "./tor/chart";
import { ChatBox } from "./tor/chat";
import { BannerRail, Shell, Spinner, Stat, TokenCell, useAsync, useToast } from "./tor/ui";
import { SignInPanel, WalletPill } from "./tor/wallet";

const NAV = [
  { href: "#/", label: "Screener" },
  { href: "#/token/TOR", label: "Token" },
  { href: "#/ads", label: "Advertise" },
  { href: "#/docs", label: "API" },
];

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#/");
  const [signIn, setSignIn] = useState(false);
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#/");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const symbol = route.startsWith("#/token/") ? decodeURIComponent(route.split("/")[2] || "TOR") : "";

  return (
    <Shell title="TOR-SCREENER" tagline="EVERY POOL · EVERY CHART" logo="/logo.svg"
      nav={NAV} active={route} walletPill={<WalletPill onOpen={() => setSignIn(true)} />}>
      {signIn ? <SignInPanel onDone={() => setSignIn(false)} /> : null}
      {symbol ? <TokenPage symbol={symbol} /> : null}
      {route.startsWith("#/ads") ? <Ads /> : null}
      {route.startsWith("#/docs") ? <Docs /> : null}
      {route === "#/" || route === "" ? <Board /> : null}
    </Shell>
  );
}

function Board() {
  const market = useAsync(
    () => api.get<{ tokens: Token[]; tor: { price_usd: number; market_cap_usd: number; volume_24h_usd: number }; totals: Record<string, number> }>("/api/market/tokens"),
    [],
  );
  const [base, setBase] = useState("ALL");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"volume" | "liquidity" | "change" | "cap">("volume");

  const rows = useMemo(() => {
    const list = (market.data?.tokens || []).filter((token) => {
      const matchesBase = base === "ALL" || token.quote_assets.includes(base);
      const needle = query.trim().toUpperCase();
      const matchesQuery = !needle || token.symbol.includes(needle) || token.name.toUpperCase().includes(needle);
      return matchesBase && matchesQuery;
    });
    const key = sort === "volume" ? "volume_24h_usd" : sort === "liquidity" ? "liquidity_usd"
      : sort === "change" ? "change_24h" : "market_cap_usd";
    return [...list].sort((a, b) => Number(b[key] || 0) - Number(a[key] || 0));
  }, [market.data, base, query, sort]);

  const totals = market.data?.totals || {};

  return (
    <div className="stack">
      <BannerRail />
      <div className="grid cols-4">
        <Stat label="TOR" value={money(market.data?.tor?.price_usd)} hint={compact(market.data?.tor?.market_cap_usd)} />
        <Stat label="Listed tokens" value={totals.tokens ?? "—"} hint={`${totals.tor_pools ?? 0} TOR · ${totals.usdc_pools ?? 0} USDC pools`} />
        <Stat label="Liquidity" value={compact(totals.liquidity_usd)} hint="quote side × 2" />
        <Stat label="24 h volume" value={compact(totals.volume_24h_usd)} hint="real swaps only" />
      </div>

      <div className="panel stack">
        <div className="spread">
          <div className="row">
            <div className="tabs">
              {["ALL", "TOR", "USDC"].map((item) => (
                <button key={item} className={base === item ? "on" : ""} onClick={() => setBase(item)}>{item}</button>
              ))}
            </div>
            <input placeholder="search ticker or name" value={query} onChange={(event) => setQuery(event.target.value)}
              style={{ width: 220 }} />
          </div>
          <div className="row">
            <span className="muted tiny">sort</span>
            <select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)} style={{ width: 150 }}>
              <option value="volume">volume</option>
              <option value="liquidity">liquidity</option>
              <option value="change">24 h change</option>
              <option value="cap">market cap</option>
            </select>
          </div>
        </div>

        {market.error ? <div className="tiny bad">{market.error}</div> : null}
        {market.loading && !rows.length ? <Spinner /> : null}
        <table>
          <thead>
            <tr><th>Pair</th><th className="num">Price</th><th className="num">24 h</th><th className="num">Volume</th>
              <th className="num">Liquidity</th><th className="num">Market cap</th><th>Age</th></tr>
          </thead>
          <tbody>
            {rows.slice(0, 60).map((token) => (
              <tr key={token.symbol}>
                <td>
                  <a href={`#/token/${token.symbol}`}><TokenCell symbol={token.symbol} name={`${token.name} / ${token.quote_assets[0] || "TOR"}`} image={token.image} /></a>
                </td>
                <td className="num">{money(token.price_usd)}</td>
                <td className={`num ${(token.change_24h || 0) >= 0 ? "good" : "bad"}`}>{pct(token.change_24h)}</td>
                <td className="num">{compact(token.volume_24h_usd)}</td>
                <td className="num">{compact(token.liquidity_usd)}</td>
                <td className="num">{compact(token.market_cap_usd)}</td>
                <td className="tiny muted">{relative(token.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- one token */
function TokenPage({ symbol }: { symbol: string }) {
  const token = useAsync(() => api.get<Token>(`/api/market/tokens/${encodeURIComponent(symbol)}`), [symbol]);

  if (token.loading && !token.data) return <Spinner />;
  if (token.error) return <div className="panel tiny bad">{token.error}</div>;
  const row = token.data;
  if (!row) return null;

  return (
    <div className="stack">
      <div className="panel stack">
        <div className="spread">
          <TokenCell symbol={row.symbol} name={row.name} image={row.image} />
          <div className="row">
            <span className="pill">{row.symbol} / {row.quote_assets.join(" + ")}</span>
            <span className={`pill ${(row.change_24h || 0) >= 0 ? "on" : ""}`}>{pct(row.change_24h)} 24 h</span>
            <a className="pill" href="http://127.0.0.1:5173/#/swap" target="_blank" rel="noreferrer">trade on Onion Swap</a>
          </div>
        </div>
        <div className="grid cols-4">
          <Stat label="Price" value={money(row.price_usd)} />
          <Stat label="Market cap" value={compact(row.market_cap_usd)} />
          <Stat label="Liquidity" value={compact(row.liquidity_usd)} />
          <Stat label="Volume 24 h" value={compact(row.volume_24h_usd)} />
        </div>
        <CandleChart symbol={row.symbol} points={168} height={280} />
      </div>

      <div className="grid cols-2">
        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Pools</h2>
          {(row.pools_usd || []).map((pool) => (
            <div key={pool.base} className="panel tight spread">
              <span className="pill">{pool.base}</span>
              <span>{money(pool.price_usd)}</span>
              <span className="muted tiny">{compact(pool.liquidity_usd)} liquidity</span>
            </div>
          ))}
        </div>
        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Profile (generated with the listing)</h2>
          <div className="stack tiny" style={{ gap: 6 }}>
            <div className="spread"><span className="muted">Mint</span><span className="mono">{shortAddress(row.mint_address)}</span></div>
            <div className="spread"><span className="muted">Owner</span><span className="mono">{shortAddress(row.owner)}</span></div>
            <div className="spread"><span className="muted">Supply</span><span>{row.supply?.toLocaleString()}</span></div>
            <div className="spread"><span className="muted">Holders</span><span>{row.holders ?? "—"}</span></div>
            <div className="spread"><span className="muted">Listed</span><span>{relative(row.created_at)}</span></div>
            <div className="spread"><span className="muted">Logo</span><span>{row.image ? "mirrored on the node" : "none yet"}</span></div>
          </div>
        </div>
      </div>
      <ChatBox title={`Ask about ${row.symbol}`} hint="The model sees the same market row this page renders." compact />
    </div>
  );
}


/* ------------------------------------------------------------------- ads */
function Ads() {
  const toast = useToast();
  const rotation = useAsync(() => api.get<{ slots: { id: string; title: string; paid: boolean }[] }>("/api/ads"), []);
  const [symbol, setSymbol] = useState("");
  const [title, setTitle] = useState("");
  const [image, setImage] = useState("");
  const [link, setLink] = useState("");

  return (
    <div className="stack">
      <BannerRail />
      <div className="hero">
        <h1>Banner ads — 50 TOR a week</h1>
        <p>
          Your banner joins the synchronous rotation beside the permanent 1Ness TOR Blockchain banner.
          Payment settles to the {BRAND.walletLabel} and the slot expires by itself after seven days.
        </p>
      </div>

      <div className="grid cols-2">
        <form className="panel stack" onSubmit={async (event) => {
          event.preventDefault();
          try {
            await api.post("/api/ads", { symbol: symbol.trim().toUpperCase(), title, image, link });
            toast("banner bought — it is in the rotation now");
          } catch (problem) {
            toast(problem instanceof Error ? problem.message : "purchase failed", true);
          }
        }}>
          <h2 style={{ margin: 0 }}>Buy a slot</h2>
          <div className="field"><label>Token ticker</label>
            <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="BTC" required />
          </div>
          <div className="field"><label>Headline</label>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="BTC on TOR — deep pool, real swaps" />
          </div>
          <div className="field"><label>Image URL (optional)</label>
            <input value={image} onChange={(event) => setImage(event.target.value)} placeholder="https://…" />
          </div>
          <div className="field"><label>Link (optional)</label>
            <input value={link} onChange={(event) => setLink(event.target.value)} placeholder="https://…" />
          </div>
          <button className="primary">Pay 50 TOR for 7 days</button>
          <p className="muted tiny">Paid from your wallet session — the charge is a real chain transfer.</p>
        </form>

        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Current rotation</h2>
          {(rotation.data?.slots || []).map((slot) => (
            <div key={slot.id} className="panel tight spread">
              <span>{slot.title}</span>
              <span className={`pill ${slot.paid ? "on" : ""}`}>{slot.paid ? "paid" : "permanent"}</span>
            </div>
          ))}
          {rotation.loading ? <Spinner /> : null}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ docs */
const ENDPOINTS = [
  ["GET", "/api/market/tokens", "every token, price, cap, liquidity"],
  ["GET", "/api/market/tokens/{symbol}", "one profile with both pools"],
  ["GET", "/api/market/chart/{symbol}", "candles generated from live samples"],
  ["GET", "/api/market/pools", "all 170 pools, deepest first"],
  ["GET", "/api/market/quote", "constant-product quote"],
  ["POST", "/api/market/swap", "settle (wallet session)"],
  ["GET", "/api/ads", "the banner rotation"],
  ["POST", "/api/ads", "buy a slot — 50 TOR"],
];

function Docs() {
  return (
    <div className="stack">
      <div className="hero">
        <h1>TOR-SCREENER API</h1>
        <p>
          Reads are free and key-less. A developer key costs 100 TOR for 30 days and unlocks the
          versioned <code>/api/v1</code> surface with the limits printed in
          <code> /api/dev/keys/pricing</code>. Charts, profiles and pool data are the endpoints this
          page already renders.
        </p>
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>Method</th><th>Endpoint</th><th>What it answers</th></tr></thead>
          <tbody>
            {ENDPOINTS.map(([method, path, summary]) => (
              <tr key={path}>
                <td><span className={`pill ${method === "GET" ? "" : "on"}`}>{method}</span></td>
                <td className="mono tiny">{path}</td>
                <td className="tiny muted">{summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>New pools appear here automatically</h2>
        <p className="muted tiny">
          The board is a live read of <code>/api/market/pools</code>: every pool created on TOR Onion
          Swap, and every TOR Pump launch, is listed with a generated profile and chart on the next
          refresh — no manual step.
        </p>
      </div>
    </div>
  );
}


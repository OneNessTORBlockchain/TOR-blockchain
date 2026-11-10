/** SOLARIX — the TOR price & market-cap engine dashboard. */
import { useEffect, useState } from "react";
import { api } from "./tor/api";
import type { Token, TorRow } from "./tor/api";
import { compact, money, pct, relative } from "./tor/brand";
import { CandleChart } from "./tor/chart";
import { ChatBox } from "./tor/chat";
import { Shell, Spinner, Stat, TokenCell, useAsync } from "./tor/ui";
import { WalletPill } from "./tor/wallet";

const NAV = [
  { href: "#/", label: "Engine" },
  { href: "#/tokens", label: "Tokens" },
  { href: "#/docs", label: "Model" },
];

type Overview = {
  at: string;
  tor: TorRow;
  totals: Record<string, number>;
  movers: { gainers: Token[]; losers: Token[] };
  newest: Token[];
  deepest: Token[];
  sources: Record<string, string>;
};

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#/");
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#/");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <Shell title="SOLARIX ENGINE" tagline="TOR PRICE · MARKET CAP" logo="/logo.svg" nav={NAV}
      active={route} walletPill={<WalletPill />}>
      {route.startsWith("#/tokens") ? <Tokens /> : null}
      {route.startsWith("#/docs") ? <Model /> : null}
      {route === "#/" || route === "" ? <Engine /> : null}
    </Shell>
  );
}

function Engine() {
  const overview = useAsync(() => api.get<Overview>("/api/market/overview"), []);
  const health = useAsync(() => api.get<{ height: number; chain_id: string }>("/api/health"), []);
  const projects = useAsync(
    () => api.get<{ projects: { id: string; name: string; dev_url: string }[] }>("/api/projects"), [],
  );

  if (overview.loading && !overview.data) return <Spinner />;
  if (overview.error) return <div className="panel tiny bad">{overview.error}</div>;
  const data = overview.data;
  if (!data) return null;

  return (
    <div className="stack">
      <div className="hero">
        <div className="spread">
          <div>
            <span className="pill">TOR / USD</span>
            <h1 style={{ margin: "10px 0 4px" }}>{money(data.tor.price_usd)}</h1>
            <p>
              anchor {money(data.tor.anchor_usd)} · drift {pct(data.tor.drift.drift * 100)} ·{" "}
              {compact(data.tor.market_cap_usd)} market cap · {data.tor.holders ?? "—"} holders
            </p>
          </div>
          <div className="stack" style={{ gap: 6, alignItems: "flex-end" }}>
            <span className="pill on"><span className="dot" />chain {health.data?.chain_id || "—"} · height {health.data?.height ?? "—"}</span>
            <span className="muted tiny">engine updated {relative(data.at)}</span>
          </div>
        </div>
      </div>

      <div className="grid cols-4">
        <Stat label="Tokens priced" value={data.totals.tokens} hint="all tradable" />
        <Stat label="Pools" value={`${data.totals.tor_pools} / ${data.totals.usdc_pools}`} hint="TOR / USDC" />
        <Stat label="Liquidity" value={compact(data.totals.liquidity_usd)} hint="both sides summed" />
        <Stat label="24 h volume" value={compact(data.totals.volume_24h_usd)} hint="real swaps only" />
      </div>

      <div className="grid cols-2">
        <div className="panel stack">
          <h2 style={{ margin: 0 }}>TOR</h2>
          <CandleChart symbol="TOR" points={168} height={220} />
          <div className="stack tiny" style={{ gap: 4 }}>
            <div className="spread muted"><span>drift rule</span><span>{data.tor.drift.rule}</span></div>
            <div className="spread muted"><span>turnover ÷ liquidity</span><span>{data.tor.drift.ratio.toExponential(3)}</span></div>
            <div className="spread muted"><span>volume 24 h</span><span>{compact(data.tor.volume_24h_usd)}</span></div>
          </div>
        </div>

        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Movers</h2>
          {data.movers.gainers.slice(0, 4).map((row) => (
            <div key={`g-${row.symbol}`} className="spread tiny">
              <TokenCell symbol={row.symbol} name={row.name} image={row.image} />
              <span className="good">{pct(row.change_24h)}</span>
            </div>
          ))}
          {data.movers.losers.slice(0, 4).map((row) => (
            <div key={`l-${row.symbol}`} className="spread tiny">
              <TokenCell symbol={row.symbol} name={row.name} image={row.image} />
              <span className="bad">{pct(row.change_24h)}</span>
            </div>
          ))}
          <div className="spread muted tiny">
            <span>deepest pool</span>
            <span>{data.deepest[0]?.symbol} · {compact(data.deepest[0]?.liquidity_usd)}</span>
          </div>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Newest listings</h2>
          {data.newest.map((row) => (
            <div key={row.symbol} className="spread tiny">
              <TokenCell symbol={row.symbol} name={row.name} image={row.image} />
              <span>{money(row.price_usd)}</span>
              <span className="muted">{relative(row.created_at)}</span>
            </div>
          ))}
        </div>
        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Platforms reading this engine</h2>
          {(projects.data?.projects || []).map((project) => (
            <a key={project.id} className="spread tiny" href={project.dev_url} target="_blank" rel="noreferrer">
              <span>{project.name}</span>
              <span className="muted">open</span>
            </a>
          ))}
          <div className="spread muted tiny"><span>sources</span><span>{data.sources.prices}</span></div>
        </div>
      </div>

      <ChatBox title="Ask the engine" hint="Grounded on the same overview this page shows." compact />
    </div>
  );
}

function Tokens() {
  const [query, setQuery] = useState("");
  const market = useAsync(() => api.get<{ tokens: Token[] }>("/api/market/tokens"), []);
  const rows = (market.data?.tokens || []).filter((row) => {
    const needle = query.trim().toUpperCase();
    return !needle || row.symbol.includes(needle) || row.name.toUpperCase().includes(needle);
  });

  return (
    <div className="panel stack">
      <div className="spread">
        <h2 style={{ margin: 0 }}>Every priced token</h2>
        <input placeholder="filter" value={query} onChange={(event) => setQuery(event.target.value)} style={{ width: 220 }} />
      </div>
      {market.loading && !rows.length ? <Spinner /> : null}
      <table>
        <thead>
          <tr><th>Token</th><th className="num">Price</th><th className="num">Market cap</th>
            <th className="num">Liquidity</th><th className="num">24 h volume</th><th className="num">24 h</th><th>Quotes</th></tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => (
            <tr key={row.symbol}>
              <td><TokenCell symbol={row.symbol} name={row.name} image={row.image} /></td>
              <td className="num">{money(row.price_usd)}</td>
              <td className="num">{compact(row.market_cap_usd)}</td>
              <td className="num">{compact(row.liquidity_usd)}</td>
              <td className="num">{compact(row.volume_24h_usd)}</td>
              <td className={`num ${(row.change_24h || 0) >= 0 ? "good" : "bad"}`}>{pct(row.change_24h)}</td>
              <td>{row.quote_assets.map((item) => <span key={item} className="pill">{item}</span>)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Model() {
  return (
    <div className="stack">
      <div className="hero">
        <h1>How the number is made</h1>
        <p>
          TOR starts at $3.60 and drifts by <code>0.35 × (24 h volume ÷ pooled liquidity)</code>, capped
          at ±15%. A token's price is its own pool ratio; market cap is price × on-chain supply; volume
          is the sum of real swap records. Nothing comes from an external oracle, and every row names
          the pool it came from.
        </p>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>Endpoints</h2>
        <div className="stack tiny mono">
          <div>GET /api/market/overview</div>
          <div>GET /api/market/tokens</div>
          <div>GET /api/market/tokens/{"{symbol}"}</div>
          <div>GET /api/market/tor</div>
          <div>GET /api/market/pools</div>
          <div>GET /api/market/chart/{"{symbol}"}</div>
        </div>
        <p className="muted tiny">
          The dashboard, TOR-Screener, DarkWeb Crypto, TOR Pump and TOR Onion Swap all read these six
          endpoints — one engine, no duplicated maths.
        </p>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>Linked to the node</h2>
        <p className="muted tiny">
          Prices come from the Rust ledger's pools; balances, supply and holders from
          <code> /api/public/ledger</code>; activity from real swap records inside the 30-day public
          window. When the node is unreachable the engine answers with its own error, never a stale
          number.
        </p>
      </div>
    </div>
  );
}


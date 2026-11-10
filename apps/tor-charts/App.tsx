/** TOR CHARTS — the candlestick generator, rendered and documented. */
import { useEffect, useState } from "react";
import { api } from "./tor/api";
import type { Chart, Token } from "./tor/api";
import { compact, money, pct } from "./tor/brand";
import { CandleChart } from "./tor/chart";
import { ChatBox } from "./tor/chat";
import { BannerRail, Shell, Shell as _Shell, Spinner, Stat, TokenCell, useAsync } from "./tor/ui";
import { WalletPill } from "./tor/wallet";

const NAV = [
  { href: "#/", label: "Chart tool" },
  { href: "#/docs", label: "Integration" },
];

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#/");
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#/");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <Shell title="TOR CHARTS" tagline="CANDLES FOR EVERY TOR APP" logo="/logo.svg" nav={NAV}
      active={route} walletPill={<WalletPill />}>
      {route.startsWith("#/docs") ? <Docs /> : <Gallery />}
    </Shell>
  );
}

function Gallery() {
  const market = useAsync(() => api.get<{ tokens: Token[] }>("/api/market/tokens"), []);
  const rows = (market.data?.tokens || []).slice(0, 9);
  const [symbol, setSymbol] = useState("TOR");
  const [raw, setRaw] = useState<Chart | null>(null);

  useEffect(() => {
    api.get<Chart>(`/api/market/chart/${symbol}?points=168`).then(setRaw).catch(() => setRaw(null));
  }, [symbol]);

  return (
    <div className="stack">
      <BannerRail />
      <div className="hero">
        <h1>Candles from the AMM, not from a mock</h1>
        <p>
          The generator reads <code>/api/market/chart/{"{symbol}"}</code>: one candle per sampled pool
          price, high/low from the same observation. A token with a single sample renders as a flat
          line and says so. TOR-Screener, DarkWeb Crypto and TOR Onion Swap all render this component.
        </p>
      </div>

      <div className="grid cols-4">
        <Stat label="Charted tokens" value={rows.length ? "120" : "—"} hint="every listed token" />
        <Stat label="Interval" value="sample" hint="node-sampled prices" />
        <Stat label="API" value="public" hint="no key needed" />
        <Stat label="Points" value="168" hint="configurable to 1000" />
      </div>

      <div className="panel stack">
        <div className="spread">
          <h2 style={{ margin: 0 }}>Featured chart</h2>
          <select value={symbol} onChange={(event) => setSymbol(event.target.value)} style={{ width: 220 }}>
            <option value="TOR">TOR</option>
            {rows.map((token) => <option key={token.symbol} value={token.symbol}>{token.symbol}</option>)}
          </select>
        </div>
        <CandleChart symbol={symbol} points={168} height={300} />
        {raw ? (
          <div className="row tiny muted" style={{ justifyContent: "space-between" }}>
            <span>{raw.points} candles</span>
            <span>{raw.source}</span>
            <span>change {pct(raw.summary.change_percent)}</span>
          </div>
        ) : <Spinner />}
      </div>

      <div className="grid cols-3">
        {rows.map((token) => (
          <div key={token.symbol} className="panel stack" style={{ gap: 8 }}>
            <div className="spread">
              <TokenCell symbol={token.symbol} name={token.name} image={token.image} />
              <span className="tiny muted">{compact(token.liquidity_usd)}</span>
            </div>
            <CandleChart symbol={token.symbol} points={60} height={120} />
            <div className="spread tiny">
              <span>{money(token.price_usd)}</span>
              <span className={((token.change_24h || 0) >= 0) ? "good" : "bad"}>{pct(token.change_24h)}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Docs() {
  return (
    <div className="stack">
      <div className="hero">
        <h1>Drop the generator into any app</h1>
        <p>
          The module is one file (<code>src/tor/chart.tsx</code>) plus the kit's API client. It has no
          chart library dependency: an SVG renderer, so it works in Tor Browser with JavaScript on and
          in a slow circuit without shipping 200 KB of bundle.
        </p>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>Fetch</h2>
        <pre className="panel tight mono tiny" style={{ overflowX: "auto" }}>
{`GET /api/market/chart/BTC?points=168
{
  "symbol": "BTC",
  "candles": [{ "time": 1793224000, "open": 22339.1, "high": 22339.1, "low": 22339.1, "close": 22339.1 }],
  "summary": { "change_percent": 0.0 },
  "source": "one observation — flat at the pool price"
}`}
        </pre>
        <h2 style={{ margin: 0 }}>Render</h2>
        <pre className="panel tight mono tiny" style={{ overflowX: "auto" }}>
{`import { CandleChart } from "./tor/chart";

<CandleChart symbol="BTC" points={168} height={260} />`}
        </pre>
        <p className="muted tiny">
          The same endpoint backs the screener boards, the sweep on TOR Pump and the DarkWeb token
          pages — one chart, one source of truth.
        </p>
      </div>
      <ChatBox title="Ask the chart engine" hint="The model is told which endpoint feeds each candle." compact />
    </div>
  );
}

/**
 * TOR ONION SWAP — swaps, liquidity, listings, wallet, ChatBox and API docs.
 * Every number on this page came from the node's own market engine.
 */
import { useEffect, useState } from "react";
import { api } from "./tor/api";
import type { Quote, Token } from "./tor/api";
import { amount, compact, money, pct, shortAddress } from "./tor/brand";
import { CandleChart } from "./tor/chart";
import { ChatBox } from "./tor/chat";
import { BannerRail, Shell, Spinner, Stat, TokenCell, useAsync, useToast } from "./tor/ui";
import { SignInPanel, WalletPanel, WalletPill } from "./tor/wallet";

const NAV = [
  { href: "#/swap", label: "Swap" },
  { href: "#/pools", label: "Liquidity" },
  { href: "#/tokens", label: "Listings" },
  { href: "#/wallet", label: "Wallet" },
  { href: "#/chat", label: "AI-NFT Chat" },
  { href: "#/docs", label: "API docs" },
];

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#/swap");
  const [signIn, setSignIn] = useState(false);

  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#/swap");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <Shell title="TOR ONION SWAP" tagline="SWAP · POOLS · WALLET" logo="/logo.svg"
      nav={NAV} active={route} walletPill={<WalletPill onOpen={() => setSignIn(true)} />}>
      {signIn ? <SignInPanel onDone={() => setSignIn(false)} /> : null}
      {route.startsWith("#/pools") ? <Liquidity /> : null}
      {route.startsWith("#/tokens") ? <Listings /> : null}
      {route.startsWith("#/wallet") ? <WalletPanel /> : null}
      {route.startsWith("#/chat") ? <ChatBox title="AI-NFT ChatBox" /> : null}
      {route.startsWith("#/docs") ? <Docs /> : null}
      {route.startsWith("#/swap") || route === "#/" ? <Swap /> : null}
    </Shell>
  );
}

function Swap() {
  const toast = useToast();
  const market = useAsync(() => api.get<{ tokens: Token[]; tor: { price_usd: number } }>("/api/market/tokens"), []);
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [base, setBase] = useState("TOR");
  const [value, setValue] = useState("5");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [error, setError] = useState("");

  const tokens = market.data?.tokens || [];
  const selected = tokens.find((token) => token.symbol === symbol) || tokens[0];
  const bases = selected?.quote_assets?.length ? selected.quote_assets : ["TOR"];

  useEffect(() => { if (!symbol && tokens.length) setSymbol(tokens[0].symbol); }, [tokens, symbol]);
  useEffect(() => { if (!bases.includes(base)) setBase(bases[0]); }, [bases, base]);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    const timer = window.setTimeout(() => {
      api.get<Quote>(`/api/market/quote?symbol=${selected.symbol}&side=${side}&amount=${value || 0}&base=${base}`)
        .then((data) => live && (setQuote(data), setError("")))
        .catch((problem: Error) => live && (setQuote(null), setError(problem.message)));
    }, 260);
    return () => { live = false; window.clearTimeout(timer); };
  }, [selected, side, value, base]);

  async function execute() {
    if (!selected) return;
    try {
      await api.post("/api/market/swap", { symbol: selected.symbol, side, amount: Number(value), base });
      toast(`${side === "buy" ? "bought" : "sold"} ${selected.symbol} on the ${base} pool`);
    } catch (problem) {
      const text = problem instanceof Error ? problem.message : "swap failed";
      setError(text);
      toast(text, true);
    }
  }

  if (market.loading && !tokens.length) return <Spinner />;
  if (!selected) return <div className="panel">the node returned no tradable tokens.</div>;

  return (
    <div className="stack">
      <BannerRail />
      <div className="grid cols-4">
        <Stat label="TOR price" value={money(market.data?.tor?.price_usd)} hint="anchor $3.60 + real turnover drift" />
        <Stat label={selected.symbol} value={money(selected.price_usd)} hint={`${compact(selected.market_cap_usd)} cap`} />
        <Stat label="Liquidity" value={compact(selected.liquidity_usd)} hint={bases.join(" / ")} />
        <Stat label="24 h volume" value={compact(selected.volume_24h_usd)} hint={`${pct(selected.change_24h)} 24 h`} />
      </div>

      <div className="grid cols-2">
        <div className="panel stack">
          <div className="spread">
            <h2 style={{ margin: 0 }}>Swap</h2>
            <div className="tabs">
              <button className={side === "buy" ? "on" : ""} onClick={() => setSide("buy")}>Buy</button>
              <button className={side === "sell" ? "on" : ""} onClick={() => setSide("sell")}>Sell</button>
            </div>
          </div>
          <div className="field"><label>Token</label>
            <select value={selected.symbol} onChange={(event) => setSymbol(event.target.value)}>
              {tokens.map((token) => (
                <option key={token.symbol} value={token.symbol}>{token.symbol} — {token.name}</option>
              ))}
            </select>
          </div>
          <div className="row" style={{ gap: 14 }}>
            <div style={{ flex: 1 }}><div className="field">
              <label>Amount</label>
              <input value={value} onChange={(event) => setValue(event.target.value)} inputMode="decimal" />
            </div></div>
            <div style={{ flex: 1 }}><div className="field">
              <label>Pay in</label>
              <select value={base} onChange={(event) => setBase(event.target.value)}>
                {bases.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </div></div>
          </div>
          <div className="panel tight tiny">
            {quote ? (
              <div className="spread">
                <span className="muted">
                  receive {amount(quote.amount_out).toLocaleString(undefined, { maximumFractionDigits: 6 })} {quote.unit_out}
                </span>
                <span>{money(quote.price_usd)} · fee {quote.fee_bps / 100}%</span>
              </div>
            ) : <span className="muted">{error || "no quote yet"}</span>}
          </div>
          <button className="primary" disabled={!quote} onClick={execute}>
            {side === "buy" ? "Buy" : "Sell"} {selected.symbol}
          </button>
          <p className="muted tiny">TOR pools settle as a chain <code>swap</code>; USDC books settle as two signed transfers.</p>
        </div>

        <div className="panel stack">
          <div className="spread">
            <TokenCell symbol={selected.symbol} name={selected.name} image={selected.image} />
            <span className="pill">{bases.join(" + ")}</span>
          </div>
          <CandleChart symbol={selected.symbol} points={120} />
          <div className="row tiny muted" style={{ justifyContent: "space-between" }}>
            <span>supply {selected.supply?.toLocaleString()}</span>
            <span>holders {selected.holders ?? "—"}</span>
            <span className="mono">{shortAddress(selected.mint_address)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}


/* --------------------------------------------------------------- liquidity */
type PoolRow = {
  symbol: string; name: string; base: string; price_usd: number | null;
  liquidity_usd: number; market_cap_usd: number | null; image: string | null;
  volume_24h_usd: number; change_24h: number | null;
};

function Liquidity() {
  const toast = useToast();
  const [base, setBase] = useState("ALL");
  const pools = useAsync(
    () => api.get<{ pools: PoolRow[]; tor_pools: number; usdc_pools: number; liquidity_usd: number }>("/api/market/pools"),
    [],
  );
  const [symbol, setSymbol] = useState("");
  const [tokens, setTokens] = useState("0");
  const [torAmount, setTorAmount] = useState("10");
  const [busy, setBusy] = useState(false);

  const rows = (pools.data?.pools || []).filter((row) => base === "ALL" || row.base === base);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.post("/api/meme/pools", {
        symbol: symbol.trim().toUpperCase(),
        token_amount: Number(tokens),
        tor_amount: Number(torAmount),
      });
      toast(`pool ${symbol.toUpperCase()} seeded`);
    } catch (problem) {
      toast(problem instanceof Error ? problem.message : "pool failed", true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="hero">
        <h1>Liquidity the chain owns</h1>
        <p>
          {pools.data?.tor_pools ?? "—"} TOR pools and {pools.data?.usdc_pools ?? "—"} USDC books hold{" "}
          {compact(pools.data?.liquidity_usd)} of quote-side liquidity. A TOR pool is a chain{" "}
          <code>pool_create</code>: the reserves sit in the pool account, the 0.30% fee stays in the
          pool, and there is no remove-liquidity transaction.
        </p>
      </div>

      <div className="panel stack">
        <div className="spread">
          <h2 style={{ margin: 0 }}>Pools</h2>
          <div className="tabs">
            {["ALL", "TOR", "USDC"].map((item) => (
              <button key={item} className={base === item ? "on" : ""} onClick={() => setBase(item)}>{item}</button>
            ))}
          </div>
        </div>
        {pools.error ? <div className="tiny bad">{pools.error}</div> : null}
        <table>
          <thead>
            <tr><th>Pool</th><th>Base</th><th className="num">Price</th><th className="num">Liquidity</th>
              <th className="num">Volume 24 h</th><th className="num">24 h</th></tr>
          </thead>
          <tbody>
            {rows.slice(0, 40).map((row) => (
              <tr key={`${row.symbol}-${row.base}`}>
                <td><TokenCell symbol={row.symbol} name={row.name} image={row.image} /></td>
                <td><span className="pill">{row.base}</span></td>
                <td className="num">{money(row.price_usd)}</td>
                <td className="num">{compact(row.liquidity_usd)}</td>
                <td className="num">{compact(row.volume_24h_usd)}</td>
                <td className={`num ${(row.change_24h || 0) >= 0 ? "good" : "bad"}`}>{pct(row.change_24h)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {pools.loading ? <Spinner /> : null}
      </div>

      <form className="panel stack" onSubmit={create}>
        <h2 style={{ margin: 0 }}>Create a pool (10 TOR platform fee + your liquidity)</h2>
        <div className="grid cols-3">
          <div className="field"><label>Ticker</label>
            <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="BTC" required />
          </div>
          <div className="field"><label>Token amount</label>
            <input value={tokens} onChange={(event) => setTokens(event.target.value)} inputMode="numeric" required />
          </div>
          <div className="field"><label>TOR side</label>
            <input value={torAmount} onChange={(event) => setTorAmount(event.target.value)} inputMode="decimal" required />
          </div>
        </div>
        <button className="primary" disabled={busy}>{busy ? "seeding…" : "Create pool"}</button>
        <p className="muted tiny">
          The signing wallet must already hold the tokens; the chain moves both reserves into the pool.
        </p>
      </form>
    </div>
  );
}


/* ---------------------------------------------------------------- listings */
type NftRow = { id: string; name: string; description?: string; image?: string };

function Listings() {
  const tokens = useAsync(() => api.get<{ tokens: Token[] }>("/api/market/tokens"), []);
  const nfts = useAsync(() => api.get<{ nfts: NftRow[] }>("/api/nft/catalogue?limit=12"), []);

  return (
    <div className="stack">
      <div className="panel stack">
        <div className="spread">
          <h2 style={{ margin: 0 }}>Tokens</h2>
          <span className="pill">{(tokens.data?.tokens || []).length} listed with logos</span>
        </div>
        <table>
          <thead>
            <tr><th>Token</th><th className="num">Price</th><th className="num">Market cap</th>
              <th className="num">Liquidity</th><th className="num">24 h</th><th>Pools</th></tr>
          </thead>
          <tbody>
            {(tokens.data?.tokens || []).slice(0, 30).map((token) => (
              <tr key={token.symbol}>
                <td><TokenCell symbol={token.symbol} name={token.name} image={token.image} /></td>
                <td className="num">{money(token.price_usd)}</td>
                <td className="num">{compact(token.market_cap_usd)}</td>
                <td className="num">{compact(token.liquidity_usd)}</td>
                <td className={`num ${(token.change_24h || 0) >= 0 ? "good" : "bad"}`}>{pct(token.change_24h)}</td>
                <td>{token.quote_assets.map((item) => <span key={item} className="pill">{item}</span>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel stack">
        <div className="spread">
          <h2 style={{ margin: 0 }}>AI-NFTs</h2>
          <span className="pill">mint one for 3 TOR on the node</span>
        </div>
        <div className="grid cols-4">
          {(nfts.data?.nfts || []).map((nft) => (
            <div key={nft.id} className="panel tight stack" style={{ gap: 8 }}>
              {nft.image ? <img src={nft.image} alt={nft.name} style={{ width: "100%", borderRadius: 12 }} /> : null}
              <b>{nft.name}</b>
              <span className="muted tiny">{nft.description || nft.id}</span>
            </div>
          ))}
        </div>
        {nfts.error ? <div className="tiny muted">{nfts.error}</div> : null}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- docs */
const DOCS = [
  ["GET", "/api/market/overview", "TOR price, totals, movers, newest listings"],
  ["GET", "/api/market/tokens", "every token priced in USD with its logo"],
  ["GET", "/api/market/chart/{symbol}", "candles from real sampled AMM prices"],
  ["GET", "/api/market/pools", "120 TOR pools + 50 USDC books"],
  ["GET", "/api/market/quote", "constant-product quote on the deeper pool"],
  ["POST", "/api/market/swap", "settle a fill (wallet session required)"],
  ["GET", "/api/ads", "banner rotation (permanent 1Ness + paid slots)"],
  ["POST", "/api/ads", "buy a 7-day banner for 50 TOR"],
  ["POST", "/api/ai/chat", "AI-NFT ChatBox on free OpenRouter models"],
  ["GET", "/api/projects", "the six TOR platforms with logos and URLs"],
];

function Docs() {
  return (
    <div className="stack">
      <div className="hero">
        <h1>Build on TOR</h1>
        <p>
          One JSON API powers this swap, TOR-Screener, DarkWeb Crypto, TOR Pump and the Solarix engine.
          Reads are open; a 100 TOR developer key unlocks the versioned <code>/api/v1</code> surface.
        </p>
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>Method</th><th>Endpoint</th><th>What it answers</th></tr></thead>
          <tbody>
            {DOCS.map(([method, path, summary]) => (
              <tr key={path}>
                <td><span className={`pill ${method === "GET" ? "" : "on"}`}>{method}</span></td>
                <td className="mono tiny">{path}</td>
                <td className="tiny muted">{summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ChatBox title="Ask the API" hint="The model reads the same catalog this page documents." compact />
    </div>
  );
}


/** TOR PUMP â€” free mints, platform liquidity, the $25 000 bonding curve. */
import { useEffect, useState } from "react";
import { api } from "./tor/api";
import { BRAND, compact, money, pct, relative, shortAddress } from "./tor/brand";
import { CandleChart } from "./tor/chart";
import { ChatBox } from "./tor/chat";
import { BannerRail, Shell, Spinner, Stat, TokenCell, useAsync, useToast } from "./tor/ui";
import { SignInPanel, WalletPill } from "./tor/wallet";

const NAV = [
  { href: "#/", label: "Launches" },
  { href: "#/launch", label: "Launch a coin" },
  { href: "#/docs", label: "How it works" },
];

type Launch = {
  symbol: string;
  name: string;
  price_usd: number | null;
  market_cap_usd: number | null;
  liquidity_usd: number | null;
  volume_24h_usd: number | null;
  change_24h: number | null;
  progress: number;
  graduated: boolean;
  creator: string;
  creator_fees_due_micro: number;
  created_at: string;
  target_mcap_usd: number;
};

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#/");
  const [signIn, setSignIn] = useState(false);
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#/");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const symbol = route.startsWith("#/coin/") ? decodeURIComponent(route.split("/")[2] || "") : "";

  return (
    <Shell title="TOR PUMP" tagline="FREE MINT Â· REAL LIQUIDITY" logo="/logo.svg"
      nav={NAV} active={route} walletPill={<WalletPill onOpen={() => setSignIn(true)} />}>
      {signIn ? <SignInPanel onDone={() => setSignIn(false)} /> : null}
      {symbol ? <Coin symbol={symbol} /> : null}
      {route.startsWith("#/launch") ? <LaunchForm /> : null}
      {route.startsWith("#/docs") ? <HowItWorks /> : null}
      {route === "#/" || route === "" ? <Board /> : null}
    </Shell>
  );
}

function Board() {
  const launches = useAsync(() => api.get<{ launches: Launch[]; target_mcap_usd: number; platform_wallet: string }>("/api/pump/launches"), []);
  const rows = launches.data?.launches || [];

  return (
    <div className="stack">
      <BannerRail />
      <div className="hero">
        <h1>Launch free. The platform brings the liquidity.</h1>
        <p>
          The node authorizes the mint, pays the chain's fee to your wallet first, then seeds a{" "}
          <strong>$5 000</strong> pool it owns. You keep the creator's first buy and 20% of the pool fee,
          and the coin graduates to the screener boards at a <strong>$25 000</strong> market cap.
        </p>
        <div className="row" style={{ marginTop: 16 }}>
          <a className="btn primary" href="#/launch">Launch a coin</a>
          <span className="pill">mint fee: free</span>
          <span className="pill">platform wallet {shortAddress(launches.data?.platform_wallet)}</span>
        </div>
      </div>

      <div className="grid cols-3">
        {launches.loading && !rows.length ? <Spinner /> : null}
        {rows.map((row) => (
          <a key={row.symbol} className="panel stack" href={`#/coin/${row.symbol}`} style={{ gap: 10 }}>
            <div className="spread">
              <TokenCell symbol={row.symbol} name={row.name} />
              <span className={`pill ${row.graduated ? "on" : ""}`}>{row.graduated ? "graduated" : `${Math.round((row.progress || 0) * 100)}%`}</span>
            </div>
            <div className="skeleton" style={{ height: 6, background: "rgba(120,200,255,0.12)" }}>
              <div style={{ width: `${Math.min(100, (row.progress || 0) * 100)}%`, height: 6, borderRadius: 6, background: "linear-gradient(90deg, var(--accent), var(--accent-2))" }} />
            </div>
            <div className="row tiny muted" style={{ justifyContent: "space-between" }}>
              <span>{money(row.price_usd)}</span>
              <span>{compact(row.market_cap_usd)} cap</span>
              <span className={(row.change_24h || 0) >= 0 ? "good" : "bad"}>{pct(row.change_24h)}</span>
            </div>
          </a>
        ))}
      </div>
      {!rows.length && !launches.loading ? (
        <div className="panel muted tiny">No launch yet â€” the first one is free.</div>
      ) : null}
    </div>
  );
}

function LaunchForm() {
  const toast = useToast();
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [image, setImage] = useState("");
  const [supply, setSupply] = useState("1000000000");
  const [firstBuy, setFirstBuy] = useState("5");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <form className="panel stack" onSubmit={async (event) => {
      event.preventDefault();
      setBusy(true);
      setError("");
      try {
        await api.post("/api/pump/launch", {
          symbol: symbol.trim().toUpperCase(), name, description, image,
          supply: Number(supply), first_buy_tor: Number(firstBuy),
        }, 120_000);
        toast("launched â€” the pool is live and already on the screener");
        window.location.hash = `#/coin/${symbol.trim().toUpperCase()}`;
      } catch (problem) {
        const text = problem instanceof Error ? problem.message : "launch failed";
        setError(text);
        toast(text, true);
      } finally {
        setBusy(false);
      }
    }}>
      <h2 style={{ margin: 0 }}>Launch a coin â€” 0 TOR</h2>
      <div className="grid cols-2">
        <div className="field"><label>Ticker</label>
          <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="NEWCOIN" maxLength={10} required />
        </div>
        <div className="field"><label>Name</label>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="New Coin" />
        </div>
      </div>
      <div className="field"><label>Description</label>
        <textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>
      <div className="field"><label>Logo URL (ipfs://, https:// or data:image/)</label>
        <input value={image} onChange={(event) => setImage(event.target.value)} placeholder="https://â€¦" />
      </div>
      <div className="grid cols-2">
        <div className="field"><label>Supply</label>
          <input value={supply} onChange={(event) => setSupply(event.target.value)} inputMode="numeric" />
        </div>
        <div className="field"><label>Your first buy (TOR)</label>
          <input value={firstBuy} onChange={(event) => setFirstBuy(event.target.value)} inputMode="decimal" />
        </div>
      </div>
      {error ? <div className="tiny bad">{error}</div> : null}
      <button className="primary" disabled={busy}>{busy ? "minting + seedingâ€¦" : "Mint free + launch"}</button>
      <p className="muted tiny">
        The platform funds the chain's 5 TOR mint fee to your wallet, takes part of the supply for the
        pool, signs <code>pool_create</code> with $5 000 of its own TOR, then executes your first buy.
      </p>
    </form>
  );
}



function Coin({ symbol }: { symbol: string }) {
  const toast = useToast();
  const coin = useAsync(() => api.get<Launch>(`/api/pump/launches/${encodeURIComponent(symbol)}`), [symbol]);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [value, setValue] = useState("1");

  if (coin.loading && !coin.data) return <Spinner />;
  if (coin.error) return <div className="panel tiny bad">{coin.error}</div>;
  const row = coin.data;
  if (!row) return null;
  const fees = (row.creator_fees_due_micro || 0) / 1e8;

  return (
    <div className="stack">
      <div className="panel stack">
        <div className="spread">
          <TokenCell symbol={row.symbol} name={row.name} />
          <div className="row">
            <span className={`pill ${row.graduated ? "on" : ""}`}>
              {row.graduated ? "graduated" : `${Math.round((row.progress || 0) * 100)}% to $25k`}
            </span>
            <span className="pill">creator {shortAddress(row.creator)}</span>
            <span className="pill">listed {relative(row.created_at)}</span>
          </div>
        </div>
        <div className="grid cols-4">
          <Stat label="Price" value={money(row.price_usd)} />
          <Stat label="Market cap" value={compact(row.market_cap_usd)} hint={`target ${compact(row.target_mcap_usd)}`} />
          <Stat label="Liquidity" value={compact(row.liquidity_usd)} />
          <Stat label="Volume 24 h" value={compact(row.volume_24h_usd)} hint={pct(row.change_24h)} />
        </div>
        <CandleChart symbol={row.symbol} points={168} height={260} />
      </div>

      <div className="grid cols-2">
        <div className="panel stack">
          <div className="tabs">
            <button className={side === "buy" ? "on" : ""} onClick={() => setSide("buy")}>Buy</button>
            <button className={side === "sell" ? "on" : ""} onClick={() => setSide("sell")}>Sell</button>
          </div>
          <div className="field">
            <label>{side === "buy" ? "TOR to spend" : `${row.symbol} to sell`}</label>
            <input value={value} onChange={(event) => setValue(event.target.value)} inputMode="decimal" />
          </div>
          <button className="primary" onClick={async () => {
            try {
              await api.post("/api/market/swap", { symbol: row.symbol, side, amount: Number(value), base: "TOR" });
              toast(`${side} order settled`);
            } catch (problem) {
              toast(problem instanceof Error ? problem.message : "swap failed", true);
            }
          }}>{side === "buy" ? "Buy" : "Sell"} {row.symbol}</button>
          <p className="muted tiny">Settled by the chain's own <code>swap</code> transaction against the platform pool.</p>
        </div>

        <div className="panel stack">
          <h2 style={{ margin: 0 }}>Creator fees</h2>
          <div className="spread">
            <span className="muted tiny">20% of the pool's 0.30% fee, from real volume</span>
            <b>{fees.toFixed(6)} TOR</b>
          </div>
          <button onClick={async () => {
            try {
              await api.post(`/api/pump/launches/${row.symbol}/claim`, {});
              toast("creator fees paid");
            } catch (problem) {
              toast(problem instanceof Error ? problem.message : "claim failed", true);
            }
          }}>Claim to my wallet</button>
          <p className="muted tiny">
            Paid as a signed transfer from the platform wallet — verifiable in
            <code> /api/chain/events</code>. Only the launch creator can claim.
          </p>
        </div>
      </div>
      <ChatBox title={`Talk to ${row.symbol}`} hint="Free OpenRouter model, node-sourced market facts." compact />
    </div>
  );
}


function HowItWorks() {
  return (
    <div className="stack">
      <div className="hero">
        <h1>How TOR Pump works</h1>
        <p>
          Minting is free because the platform funds the chain fee; the pool belongs to the platform, so
          the $5 000 of liquidity is real TOR it owns and cannot pull back — the chain has no
          remove-liquidity transaction. The creator keeps the first buy and 20% of the pool fee.
        </p>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>The curve</h2>
        <p className="muted tiny">
          Market cap is the pool's own reserve ratio × on-chain supply. The bar on every card is
          <code> market cap ÷ $25 000</code>; passing it marks the launch graduated. Because the pool
          exists from block one, every launch is already on TOR-Screener and DarkWeb Crypto.
        </p>
        <div className="grid cols-3">
          <Stat label="Starting liquidity" value="$5,000" hint="platform funded" />
          <Stat label="Graduation" value="$25,000" hint="market cap" />
          <Stat label="Creator fee" value="20%" hint="of the 0.30% pool fee" />
        </div>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>API</h2>
        <div className="stack tiny mono">
          <div>GET  /api/pump/launches</div>
          <div>GET  /api/pump/launches/{"{symbol}"}</div>
          <div>POST /api/pump/launch</div>
          <div>POST /api/pump/launches/{"{symbol}"}/claim</div>
          <div>POST /api/market/swap</div>
        </div>
        <p className="muted tiny">Every one is listed in <code>/api/docs</code> with its parameters.</p>
      </div>
      <div className="panel stack">
        <h2 style={{ margin: 0 }}>Support</h2>
        <p className="muted tiny">{BRAND.support} · {BRAND.clearnet} · {BRAND.onion}</p>
      </div>
    </div>
  );
}


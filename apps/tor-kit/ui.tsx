/** Shared UI: shell, nav, banner rotation, token logo, stats, toasts. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { Banner, Project } from "./api";
import { BRAND, asset } from "./brand";

export type Crumb = { href: string; label: string };

type ShellProps = {
  title: string;
  tagline: string;
  logo: string;
  nav: Crumb[];
  active: string;
  walletPill?: ReactNode;
  children: ReactNode;
};

export function Shell({ title, tagline, logo, nav, active, walletPill, children }: ShellProps) {
  return (
    <div className="shell">
      <header className="topbar">
        <div className="wrap">
          <div className="brandmark">
            <img src={logo} alt={`${title} logo`} />
            <div>
              <b>{title}</b>
              <span>{tagline}</span>
            </div>
          </div>
          <nav className="nav">
            {nav.map((item) => (
              <a key={item.href} href={item.href} className={item.href === active ? "active" : ""}>
                {item.label}
              </a>
            ))}
          </nav>
          <div style={{ marginLeft: "auto" }} className="row">
            {walletPill}
          </div>
        </div>
      </header>
      <main className="wrap stack" style={{ padding: "24px 0 0" }}>
        {children}
      </main>
      <Footer title={title} />
    </div>
  );
}

export function Footer({ title }: { title: string }) {
  return (
    <footer>
      <div className="wrap">
        <div>
          <b>{title}</b>
          <div>Built on the {BRAND.chain} · chain id {BRAND.chainId}</div>
          <div style={{ marginTop: 6 }}>
            Platform fees settle to the <strong>{BRAND.walletLabel}</strong>.
          </div>
        </div>
        <div>
          <b>Tor network</b>
          <div className="onion">
            <a href={BRAND.onion} target="_blank" rel="noreferrer">{BRAND.onion}</a>
          </div>
          <div style={{ marginTop: 6 }}>
            <a href={BRAND.clearnet} target="_blank" rel="noreferrer">
              www.onenesstorblockchain.com
            </a>
          </div>
        </div>
        <div>
          <b>Support</b>
          <div><a href={`mailto:${BRAND.support}`}>{BRAND.support}</a></div>
          <div>
            <a href={BRAND.company} target="_blank" rel="noreferrer">
              www.onenesstech.ca
            </a>
          </div>
        </div>
      </div>
    </footer>
  );
}

/* ------------------------------------------------------------------ tokens */
export function TokenLogo({ symbol, image, size = 30 }: { symbol: string; image?: string | null; size?: number }) {
  const [failed, setFailed] = useState(false);
  const src = asset(image);
  if (!src || failed) {
    return (
      <span className="fallback" style={{ width: size, height: size, fontSize: Math.max(9, size / 3) }}>
        {symbol.slice(0, 3)}
      </span>
    );
  }
  return (
    <img
      src={src}
      alt={`${symbol} logo`}
      width={size}
      height={size}
      onError={() => setFailed(true)}
      style={{ width: size, height: size }}
    />
  );
}

export function TokenCell({ symbol, name, image }: { symbol: string; name?: string; image?: string | null }) {
  return (
    <div className="token">
      <TokenLogo symbol={symbol} image={image} />
      <div>
        <b>{symbol}</b>
        <span>{name || symbol}</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- stats */
export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="stat">
      <span>{label}</span>
      <b>{value}</b>
      {hint ? <em>{hint}</em> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ banner */
export function BannerRail({ className }: { className?: string }) {
  const [slots, setSlots] = useState<Banner[]>([]);
  const [index, setIndex] = useState(0);
  const [seconds, setSeconds] = useState(8);

  useEffect(() => {
    let live = true;
    api
      .get<{ slots: Banner[]; rotation_seconds: number }>("/api/ads")
      .then((data) => {
        if (!live) return;
        setSlots(data.slots || []);
        setSeconds(data.rotation_seconds || 8);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (slots.length < 2) return;
    const timer = window.setInterval(
      () => setIndex((current) => (current + 1) % slots.length),
      Math.max(4, seconds) * 1000,
    );
    return () => window.clearInterval(timer);
  }, [slots.length, seconds]);

  if (!slots.length) {
    return (
      <div className={`banner ${className || ""}`}>
        <div>
          <span className="tag">Sponsored rotation</span>
          <b>1Ness TOR Blockchain</b>
          <p>The permanent banner loads from the node at /api/ads.</p>
        </div>
        <div className="skeleton" style={{ height: 110 }} />
      </div>
    );
  }
  const slot = slots[index % slots.length];
  return (
    <a className={`banner ${className || ""}`} href={slot.link || BRAND.clearnet} target="_blank" rel="noreferrer">
      <div>
        <span className="tag">{slot.paid ? "Paid banner · 7 days" : "Permanent rotation"}</span>
        <b>{slot.title}</b>
        <p>{slot.subtitle}</p>
        <div className="dots">
          {slots.map((item, position) => (
            <i key={item.id} className={position === index % slots.length ? "on" : ""} />
          ))}
        </div>
      </div>
      {slot.image ? (
        <img src={asset(slot.image)} alt={slot.title} />
      ) : (
        <div className="skeleton" style={{ height: 110 }} />
      )}
    </a>
  );
}

/* ------------------------------------------------------------------ toasts */
type Toast = { id: number; message: string; bad?: boolean };
const ToastContext = createContext<(message: string, bad?: boolean) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((message: string, bad = false) => {
    const id = Date.now() + Math.random();
    setItems((current) => [...current, { id, message, bad }]);
    window.setTimeout(
      () => setItems((current) => current.filter((item) => item.id !== id)),
      6000,
    );
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      {items.map((item) => (
        <div key={item.id} className={`toast ${item.bad ? "bad" : ""}`}>
          {item.message}
        </div>
      ))}
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

/* ----------------------------------------------------------------- helpers */
export function Spinner({ label }: { label?: string }) {
  return <div className="muted tiny">{label || "loading from the node…"}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="muted tiny" style={{ padding: "18px 0" }}>{children}</div>;
}

/** Tiny data hook: keeps the node's error text instead of a generic one. */
export function useAsync<T>(run: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const memo = useMemo(() => run, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let live = true;
    setLoading(true);
    memo()
      .then((value) => {
        if (!live) return;
        setData(value);
        setError("");
      })
      .catch((problem: Error) => live && setError(problem.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [memo]);
  return { data, error, loading, setData };
}

export type ProjectDirectory = { projects: Project[] };


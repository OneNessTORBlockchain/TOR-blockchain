/**
 * TOR API client — one file every TOR platform imports.
 *
 * The node is the product: swap, screener, darkweb, pump, charts and the price
 * engine all read the same endpoints, so this client is deliberately thin and
 * honest. Every answer keeps the node's envelope shape, and a failure carries
 * the node's own error code instead of a generic message.
 */
export const API_BASE =
  (import.meta.env?.VITE_TOR_API as string | undefined)?.replace(/\/$/, "") ||
  "http://127.0.0.1:8090";

export const SESSION_KEY = "tor.wallet.session";

export type ApiError = {
  code: string;
  http_status: number;
  message: string;
  details?: Record<string, unknown> | null;
};

export type Envelope<T> = {
  ok: boolean;
  data: T | null;
  error: ApiError | null;
  meta?: Record<string, unknown>;
};

export class TorError extends Error {
  code: string;
  status: number;
  details?: Record<string, unknown> | null;

  constructor(error: ApiError) {
    super(error.message || error.code);
    this.code = error.code;
    this.status = error.http_status;
    this.details = error.details ?? null;
  }
}

export type Session = {
  wallet: string;
  address: string;
  token: string;
  header: string;
};

export function readSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    return parsed?.token ? parsed : null;
  } catch {
    return null;
  }
}

export function writeSession(session: Session | null) {
  if (!session) localStorage.removeItem(SESSION_KEY);
  else localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

function headers(extra?: HeadersInit): HeadersInit {
  const session = readSession();
  return {
    "content-type": "application/json",
    ...(session ? { [session.header || "x-wallet-session"]: session.token } : {}),
    ...(extra || {}),
  };
}

export async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    init?.timeoutMs ?? 45_000,
  );
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: headers(init?.headers),
      signal: controller.signal,
    });
    const body = (await response.json().catch(() => null)) as Envelope<T> | null;
    if (!response.ok || !body || body.ok === false) {
      const error: ApiError = body?.error || {
        code: "HTTP_" + response.status,
        http_status: response.status,
        message: `the node answered ${response.status}`,
      };
      throw new TorError(error);
    }
    return body.data as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

export const api = {
  get: <T>(path: string, timeoutMs?: number) =>
    request<T>(path, { method: "GET", timeoutMs }),
  post: <T>(path: string, body?: unknown, timeoutMs?: number) =>
    request<T>(path, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
      timeoutMs,
    }),
};

/* ------------------------------------------------------------------ shapes */

export type Pool = {
  base: "TOR" | "USDC" | string;
  price_usd: number | null;
  liquidity_usd: number;
  price_tor?: number;
  liquidity_tor?: number;
  token_reserve?: number;
  base_reserve?: number;
};

export type Token = {
  symbol: string;
  name: string;
  supply: number;
  holders: number | null;
  mint_address: string;
  owner: string;
  price_usd: number | null;
  price_tor: number | null;
  market_cap_usd: number | null;
  liquidity_usd: number;
  volume_24h_usd: number;
  change_24h: number | null;
  quote_assets: string[];
  pools_usd: Pool[];
  image: string | null;
  created_at: number | null;
  observations?: number;
};

export type TorRow = {
  symbol: string;
  price_usd: number;
  anchor_usd: number;
  market_cap_usd: number | null;
  liquidity_usd: number;
  volume_24h_usd: number;
  holders: number | null;
  image: string | null;
  drift: { drift: number; ratio: number; rule: string };
};

export type MarketSnapshot = {
  at: string;
  tor: TorRow;
  tokens: Token[];
  totals: {
    tokens: number;
    market_cap_usd: number;
    liquidity_usd: number;
    volume_24h_usd: number;
    tor_pools: number;
    usdc_pools: number;
    tradable: number;
  };
  sources: Record<string, string>;
};

export type Banner = {
  slot: number;
  id: string;
  kind: string;
  title: string;
  subtitle: string;
  image: string;
  link: string;
  paid: boolean;
};

export type Project = {
  id: string;
  name: string;
  tagline: string;
  logo: string;
  dev_url: string;
  onion_url: string;
  clearnet_url: string;
  port: number;
  features: string[];
};

export type Chart = {
  symbol: string;
  interval: string;
  points: number;
  candles: { time: number; open: number; high: number; low: number; close: number }[];
  summary: { first: number; last: number; high: number; low: number; change_percent: number | null };
  source: string;
  span: number;
};

export type Model = {
  id: string;
  name: string;
  context_length: number | null;
  description: string;
};

export type Quote = {
  symbol: string;
  base: string;
  side: "buy" | "sell";
  amount_in: number;
  unit_in: string;
  amount_out: number;
  unit_out: string;
  fee?: number;
  fee_bps: number;
  price_usd: number | null;
};

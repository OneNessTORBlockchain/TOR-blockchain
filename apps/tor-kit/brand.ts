/** Brand constants and formatters shared by every TOR platform. */
import { API_BASE } from "./api";

export const BRAND = {
  onion:
    "http://4thdw6cvoacqtxpyvxziebiif5qmiwginb7wwgktsalmevrabqubpxid.onion",
  clearnet: "https://www.onenesstorblockchain.com",
  support: "support@onenesstech.ca",
  company: "https://www.onenesstech.ca",
  walletLabel: "Tor AI-NFT Wallet",
  chain: "OneNess TOR",
  chainId: "072316",
};

/** A node-served asset (logos, banners) as an absolute URL. */
export function asset(path?: string | null): string {
  if (!path) return "";
  if (/^https?:\/\//i.test(path) || path.startsWith("data:")) return path;
  return `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
}

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

export function money(value?: number | null, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  if (abs > 0 && abs < 0.01) return `$${value.toFixed(8)}`;
  if (abs < 1) return `$${value.toFixed(4)}`;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
  }).format(value);
}

export function compact(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return usd.format(value);
}

export function tor(value?: number | null, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${new Intl.NumberFormat("en-US", {
    maximumFractionDigits: digits,
  }).format(value)} TOR`;
}

export function pct(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function shortAddress(value?: string | null): string {
  if (!value) return "—";
  if (value.length <= 16) return value;
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

export function relative(stamp?: number | string | null): string {
  if (!stamp) return "—";
  const when = typeof stamp === "number" ? stamp * 1000 : Date.parse(stamp);
  if (Number.isNaN(when)) return "—";
  const seconds = Math.round((Date.now() - when) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function amount(value: string | number): number {
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

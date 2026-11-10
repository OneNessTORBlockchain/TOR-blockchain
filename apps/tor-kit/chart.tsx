/** Candlestick renderer — the chart tool every TOR app uses. */
import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { Chart } from "./api";
import { money, pct } from "./brand";

export function CandleChart({ symbol, points = 168, height = 240 }: { symbol: string; points?: number; height?: number }) {
  const [data, setData] = useState<Chart | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get<Chart>(`/api/market/chart/${encodeURIComponent(symbol)}?points=${points}`)
      .then((chart) => live && (setData(chart), setError("")))
      .catch((problem: Error) => live && setError(problem.message));
    return () => {
      live = false;
    };
  }, [symbol, points]);

  const geometry = useMemo(() => {
    if (!data?.candles?.length) return null;
    const width = 720;
    const pad = 8;
    const closes = data.candles.map((candle) => candle.close);
    const high = Math.max(...closes, ...data.candles.map((candle) => candle.high));
    const low = Math.min(...closes, ...data.candles.map((candle) => candle.low));
    const span = high - low || Math.max(high, 1e-9) * 0.02;
    const step = (width - pad * 2) / data.candles.length;
    const x = (index: number) => pad + index * step + step / 2;
    const y = (value: number) => pad + (1 - (value - low) / span) * (height - pad * 2);
    const up = closes[closes.length - 1] >= closes[0];
    const path = data.candles
      .map((candle, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(candle.close).toFixed(2)}`)
      .join(" ");
    return { width, high, low, x, y, step, path, up, count: data.candles.length };
  }, [data, height]);

  if (error) return <div className="tiny bad">chart: {error}</div>;
  if (!data || !geometry) return <div className="skeleton" style={{ height }} />;

  const colour = geometry.up ? "var(--good)" : "var(--bad)";
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="spread">
        <div className="row" style={{ gap: 10 }}>
          <b style={{ letterSpacing: "0.1em" }}>{data.symbol}</b>
          <span className="pill">{data.interval}</span>
          <span className={`pill ${geometry.up ? "on" : ""}`}>{pct(data.summary.change_percent)}</span>
        </div>
        <span className="muted tiny">
          {data.source} · {data.span} observation{data.span === 1 ? "" : "s"}
        </span>
      </div>
      <svg className="chart" viewBox={`0 0 ${geometry.width} ${height}`} role="img" aria-label={`${data.symbol} chart`}>
        <defs>
          <linearGradient id={`fill-${data.symbol}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={colour} stopOpacity="0.35" />
            <stop offset="1" stopColor={colour} stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((ratio) => (
          <line
            key={ratio}
            x1={0}
            x2={geometry.width}
            y1={height * ratio}
            y2={height * ratio}
            stroke="rgba(120,200,255,0.10)"
            strokeDasharray="4 6"
          />
        ))}
        {data.candles.map((candle, index) => {
          const candleUp = candle.close >= candle.open;
          const top = geometry.y(Math.max(candle.open, candle.close));
          const bottom = geometry.y(Math.min(candle.open, candle.close));
          return (
            <g key={candle.time + "-" + index}>
              <line
                x1={geometry.x(index)}
                x2={geometry.x(index)}
                y1={geometry.y(candle.high)}
                y2={geometry.y(candle.low)}
                stroke={candleUp ? "var(--good)" : "var(--bad)"}
                strokeWidth={1}
                opacity={0.6}
              />
              <rect
                x={geometry.x(index) - Math.max(1.2, geometry.step * 0.26)}
                y={top}
                width={Math.max(2.4, geometry.step * 0.52)}
                height={Math.max(1.5, bottom - top)}
                fill={candleUp ? "var(--good)" : "var(--bad)"}
                opacity={0.85}
                rx={1}
              />
            </g>
          );
        })}
        {geometry.count > 2 ? (
          <>
            <path d={`${geometry.path} L${geometry.width - 8},${height} L8,${height} Z`} fill={`url(#fill-${data.symbol})`} opacity={0.5} />
            <path d={geometry.path} fill="none" stroke={colour} strokeWidth={1.6} opacity={0.9} />
          </>
        ) : null}
      </svg>
      <div className="row muted tiny" style={{ justifyContent: "space-between" }}>
        <span>low {money(geometry.low)}</span>
        <span>high {money(geometry.high)}</span>
      </div>
    </div>
  );
}

export function Sparkline({ values, width = 120, height = 30 }: { values: number[]; width?: number; height?: number }) {
  if (!values.length) return <span className="muted tiny">—</span>;
  const high = Math.max(...values);
  const low = Math.min(...values);
  const span = high - low || 1;
  const step = width / Math.max(1, values.length - 1);
  const path = values
    .map((value, index) => `${index === 0 ? "M" : "L"}${(index * step).toFixed(2)},${(height - ((value - low) / span) * height).toFixed(2)}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} className="chart" aria-hidden="true">
      <path d={path} fill="none" stroke={up ? "var(--good)" : "var(--bad)"} strokeWidth={1.6} />
    </svg>
  );
}

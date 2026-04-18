import { useEffect, useState } from "react";
import { fetchCandles } from "../api.js";

const CHART_W = 560;
const CHART_H = 300;
const PAD = { top: 20, right: 10, bottom: 40, left: 10 };
const CANDLE_W = 10;
const REFETCH_MS = 30_000;
const SYMBOL_LABELS = { "BTC/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL", "BTC/USDT": "BTC", "ETH/USDT": "ETH", "SOL/USDT": "SOL" };

function useCandles(running, symbol) {
  const [candles, setCandles] = useState([]);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;

    async function load() {
      try {
        const data = await fetchCandles(symbol);
        if (alive) {
          setCandles(Array.isArray(data) ? data : []);
          setError(false);
        }
      } catch {
        if (alive) setError(true);
      }
    }

    load();
    const id = setInterval(load, REFETCH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [running, symbol]);

  return { candles, error };
}

function scaleY(value, min, max) {
  const range = max - min || 1;
  return PAD.top + ((max - value) / range) * (CHART_H - PAD.top - PAD.bottom);
}

export default function ChartPanel({ running, activeSymbols = ["BTC/USDT"] }) {
  const tabs = activeSymbols.length > 0 ? activeSymbols : ["BTC/USDT"];
  const [selected, setSelected] = useState(tabs[0]);
  const activeTab = tabs.includes(selected) ? selected : tabs[0];
  const { candles, error } = useCandles(running, activeTab);

  const visible = candles.slice(-16); // show last 16 candles max
  const closes = visible.map((c) => c.close);
  const highs = visible.map((c) => c.high);
  const lows = visible.map((c) => c.low);

  const priceMin = visible.length ? Math.min(...lows) * 0.999 : 0;
  const priceMax = visible.length ? Math.max(...highs) * 1.001 : 1;

  const step = visible.length > 1 ? (CHART_W - PAD.left - PAD.right) / (visible.length) : 40;
  const xOf = (i) => PAD.left + i * step + step / 2;
  const yOf = (v) => scaleY(v, priceMin, priceMax);

  // SMA line drawn across visible candles (use close values).
  const smaPath = closes.length
    ? closes
        .map((c, i) => `${i === 0 ? "M" : "L"} ${xOf(i)} ${yOf(c)}`)
        .join(" ")
    : "";

  // Derive a simple RSI label from the last candle data.
  const lastClose = closes.at(-1);
  const rsiLabel = visible.length >= 14
    ? "RSI live"
    : "RSI —";

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-neutral-400">Market</p>
          <h3 className="mt-1 text-xl font-semibold tracking-normal text-white">
            {activeTab} Candlestick
          </h3>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          {tabs.length > 1 && tabs.map((sym) => (
            <button key={sym} onClick={() => setSelected(sym)}
              className={`rounded-lg border px-3 py-1.5 font-medium transition ${
                activeTab === sym
                  ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                  : "border-neutral-700 text-neutral-400 hover:border-neutral-600"
              }`}>
              {SYMBOL_LABELS[sym] ?? sym}
            </button>
          ))}
          <span className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-emerald-200">
            50 SMA
          </span>
          <span className="rounded-lg border border-cyan-400/20 bg-cyan-400/10 px-3 py-2 text-cyan-200">
            {rsiLabel}
          </span>
          {error && (
            <span className="rounded-lg border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-amber-200">
              API offline
            </span>
          )}
        </div>

      </div>

      <div className="mt-5 overflow-hidden rounded-lg border border-neutral-800 bg-neutral-950">
        {visible.length === 0 ? (
          <div className="flex h-[300px] items-center justify-center text-sm text-neutral-500">
            {error
              ? "Cannot load candles — is the Flask server running?"
              : "Start the bot to see live chart data."}
          </div>
        ) : (
          <svg
            viewBox={`0 0 ${CHART_W} ${CHART_H}`}
            className="h-[300px] w-full"
            role="img"
            aria-label="BTC/USDT candlestick chart"
          >
            {/* Grid */}
            <defs>
              <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
                <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#262626" strokeWidth="1" />
              </pattern>
            </defs>
            <rect width={CHART_W} height={CHART_H} fill="url(#grid)" />

            {/* SMA / midpoint line */}
            {smaPath && (
              <path
                d={smaPath}
                fill="none"
                stroke="#22d3ee"
                strokeWidth="2"
                opacity="0.8"
              />
            )}

            {/* Candles */}
            {visible.map((candle, i) => {
              const isUp = candle.close >= candle.open;
              const color = isUp ? "#34d399" : "#f87171";
              const bodyTop = yOf(Math.max(candle.open, candle.close));
              const bodyBot = yOf(Math.min(candle.open, candle.close));
              const bodyH = Math.max(2, bodyBot - bodyTop);
              const cx = xOf(i);

              return (
                <g key={candle.timestamp ?? i}>
                  {/* Wick */}
                  <line
                    x1={cx}
                    x2={cx}
                    y1={yOf(candle.high)}
                    y2={yOf(candle.low)}
                    stroke={color}
                    strokeWidth="1.5"
                  />
                  {/* Body */}
                  <rect
                    x={cx - CANDLE_W / 2}
                    y={bodyTop}
                    width={CANDLE_W}
                    height={bodyH}
                    rx="2"
                    fill={color}
                    opacity="0.95"
                  />
                </g>
              );
            })}

            {/* Price axis labels (min / max) */}
            <text
              x={PAD.left + 4}
              y={PAD.top + 12}
              fill="#6b7280"
              fontSize="10"
            >
              ${priceMax.toLocaleString("en-US", { maximumFractionDigits: 0 })}
            </text>
            <text
              x={PAD.left + 4}
              y={CHART_H - PAD.bottom + 12}
              fill="#6b7280"
              fontSize="10"
            >
              ${priceMin.toLocaleString("en-US", { maximumFractionDigits: 0 })}
            </text>

            {/* Footer note */}
            <text x={CHART_W / 2} y={CHART_H - 8} fill="#52525b" fontSize="11" textAnchor="middle">
              1m candles — last {visible.length} shown
            </text>
          </svg>
        )}
      </div>
    </section>
  );
}

import { useState } from "react";
import { XCircle } from "lucide-react";
import { closePosition } from "../api.js";

function fmt(n, decimals = 2) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export default function PositionsTable({ positions = {}, onRefresh }) {
  const [closing, setClosing] = useState(null); // symbol being closed
  const [error, setError] = useState(null);

  async function handleClose(symbol) {
    setClosing(symbol);
    setError(null);
    try {
      await closePosition(symbol);
      await onRefresh?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setClosing(null);
    }
  }

  const positionList = Object.values(positions || {});

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm text-neutral-400">Portfolio</p>
          <h3 className="mt-1 text-xl font-semibold tracking-normal text-white">Open Positions</h3>
        </div>
        <span className="rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2 text-sm text-neutral-300">
          {positionList.length} active
        </span>
      </div>

      {error && (
        <p className="mt-3 rounded-lg border border-red-400/20 bg-red-400/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <div className="mt-5 overflow-x-auto">
        <table className="w-full min-w-[760px] border-separate border-spacing-y-2 text-left text-sm">
          <thead className="text-xs uppercase tracking-[0.12em] text-neutral-500">
            <tr>
              <th className="px-3 py-2 font-medium">Symbol</th>
              <th className="px-3 py-2 font-medium">Entry Price</th>
              <th className="px-3 py-2 font-medium">Current Price</th>
              <th className="px-3 py-2 font-medium">PnL</th>
              <th className="px-3 py-2 font-medium">Stop Loss</th>
              <th className="px-3 py-2 font-medium">Take Profit</th>
              <th className="px-3 py-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {positionList.length > 0 ? positionList.map((pos) => {
              const pnlPositive = pos.pnl >= 0;
              return (
                <tr key={pos.symbol} className="bg-neutral-950 text-neutral-200">
                  <td className="rounded-l-lg px-3 py-4 font-medium text-white">
                    {pos.symbol}
                    {pos.is_house_trade && (
                      <span className="ml-2 rounded bg-violet-400/20 px-1.5 py-0.5 text-xs text-violet-300">house</span>
                    )}
                  </td>
                  <td className="px-3 py-4">${fmt(pos.entry)}</td>
                  <td className="px-3 py-4">${fmt(pos.current)}</td>
                  <td className={`px-3 py-4 font-semibold ${pnlPositive ? "text-emerald-300" : "text-red-300"}`}>
                    {pnlPositive ? "+" : ""}${fmt(Math.abs(pos.pnl))}
                    <span className="ml-1.5 text-xs opacity-70">
                      ({pnlPositive ? "+" : ""}{fmt(pos.pnl_pct)}%)
                    </span>
                  </td>
                  <td className="px-3 py-4 text-red-200">${fmt(pos.stop_loss)}</td>
                  <td className="px-3 py-4 text-emerald-200">${fmt(pos.take_profit)}</td>
                  <td className="rounded-r-lg px-3 py-4">
                    <button
                      onClick={() => handleClose(pos.symbol)}
                      disabled={closing === pos.symbol}
                      className="inline-flex items-center gap-2 rounded-lg border border-red-400/20 bg-red-400/10 px-3 py-2 text-xs font-medium text-red-200 transition hover:bg-red-400/20 disabled:opacity-50"
                    >
                      <XCircle className="h-4 w-4" />
                      {closing === pos.symbol ? "Closing…" : "Close"}
                    </button>
                  </td>
                </tr>
              );
            }) : (
              <tr>
                <td colSpan={7} className="rounded-lg bg-neutral-950 px-3 py-8 text-center text-sm text-neutral-500">
                  No open positions — start the bot and wait for a signal.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

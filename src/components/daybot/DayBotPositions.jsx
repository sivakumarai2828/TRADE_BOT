function fmt(n, d = 2) {
  if (n == null || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export default function DayBotPositions({ positions }) {
  const entries = Object.entries(positions ?? {});

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-5">
      <div className="mb-4 flex items-center justify-between">
        <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">Open Positions</p>
        {entries.length > 0 && (
          <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-2 py-0.5 text-xs text-emerald-300">
            {entries.length} active
          </span>
        )}
      </div>

      {entries.length === 0 ? (
        <p className="py-8 text-center text-sm text-neutral-500">No open positions</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-neutral-800 text-left text-xs uppercase tracking-[0.15em] text-neutral-500">
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Qty</th>
                <th className="pb-3 pr-4">Entry</th>
                <th className="pb-3 pr-4">Current</th>
                <th className="pb-3 pr-4">P&L</th>
                <th className="pb-3 pr-4">Stop Loss</th>
                <th className="pb-3">Take Profit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/60">
              {entries.map(([sym, pos]) => {
                const pnlPos = (pos.pnl ?? 0) >= 0;
                return (
                  <tr key={sym} className="text-neutral-200">
                    <td className="py-3 pr-4 font-medium text-white">{sym}</td>
                    <td className="py-3 pr-4">{pos.qty}</td>
                    <td className="py-3 pr-4">${fmt(pos.entry_price)}</td>
                    <td className="py-3 pr-4">${fmt(pos.current_price)}</td>
                    <td className={`py-3 pr-4 font-medium ${pnlPos ? "text-emerald-400" : "text-red-400"}`}>
                      {pnlPos ? "+" : ""}${fmt(pos.pnl)} ({pnlPos ? "+" : ""}{fmt(pos.pnl_pct)}%)
                    </td>
                    <td className="py-3 pr-4 text-red-300">${fmt(pos.stop_loss)}</td>
                    <td className="py-3 text-emerald-300">${fmt(pos.take_profit)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Badge({ action }) {
  const cls =
    action === "BUY"
      ? "border-emerald-400/40 bg-emerald-400/15 text-emerald-300"
      : action === "SELL"
      ? "border-red-400/40 bg-red-400/15 text-red-300"
      : "border-neutral-600 bg-neutral-800 text-neutral-400";
  return (
    <span className={`rounded-md border px-2 py-0.5 text-xs font-bold ${cls}`}>{action}</span>
  );
}

export default function DayBotSignals({ signals, watchlist }) {
  const entries = Object.entries(signals ?? {});

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-5">
      <p className="mb-4 text-xs uppercase tracking-[0.18em] text-neutral-500">AI Signals</p>

      {entries.length === 0 ? (
        <p className="py-6 text-center text-sm text-neutral-500">Waiting for market data…</p>
      ) : (
        <div className="space-y-3">
          {entries.map(([sym, sig]) => (
            <div key={sym} className="rounded-lg border border-neutral-800 bg-neutral-800/40 p-3">
              <div className="flex items-center justify-between">
                <span className="font-medium text-white">{sym}</span>
                <Badge action={sig.action} />
              </div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-neutral-400">
                <span>RSI <span className="text-white">{sig.rsi?.toFixed(1) ?? "—"}</span></span>
                <span>Price <span className="text-white">${sig.price?.toFixed(2) ?? "—"}</span></span>
                <span>EMA <span className="text-white">${sig.ema?.toFixed(2) ?? "—"}</span></span>
              </div>
              {sig.ai_confidence > 0 && (
                <div className="mt-2 text-xs">
                  <span className="text-neutral-500">AI confidence: </span>
                  <span className={sig.ai_confidence >= 0.65 ? "text-emerald-400" : "text-amber-400"}>
                    {(sig.ai_confidence * 100).toFixed(0)}%
                  </span>
                  {sig.ai_reason && (
                    <span className="ml-2 text-neutral-400">{sig.ai_reason}</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {watchlist?.length > 0 && (
        <div className="mt-4 border-t border-neutral-800 pt-4">
          <p className="mb-2 text-xs text-neutral-500">Watchlist ({watchlist.length})</p>
          <div className="flex flex-wrap gap-1.5">
            {watchlist.map(sym => (
              <span key={sym} className="rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-xs text-neutral-300">
                {sym}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

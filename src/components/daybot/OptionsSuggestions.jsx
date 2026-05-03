import { useEffect, useState } from "react";
import { fetchOptionsSuggestions, runOptionsPicker } from "../../api.js";

export default function OptionsSuggestions() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");

  const refresh = () => {
    setLoading(true);
    fetchOptionsSuggestions()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, []);

  const handleRunNow = async () => {
    setRunning(true);
    setRunMsg("");
    try {
      const res = await runOptionsPicker();
      setRunMsg(res.message ?? "Started…");
      setTimeout(() => { refresh(); setRunning(false); }, 35000);
    } catch (e) {
      setRunMsg("Failed: " + e.message);
      setRunning(false);
    }
  };

  const picks = data?.picks ?? [];
  const date = data?.date ?? "";

  if (loading) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 text-sm text-neutral-500">
        Loading options picks…
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">AI Options Suggestions</h3>
        <div className="flex items-center gap-2">
          {date && (
            <span className="rounded-full border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400">
              {date}
            </span>
          )}
          <button
            onClick={handleRunNow}
            disabled={running}
            className="rounded-lg border border-indigo-700 bg-indigo-900/40 px-3 py-1 text-xs font-medium text-indigo-300 hover:bg-indigo-800/60 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {running ? "Running…" : "▶ Run Now"}
          </button>
          <button
            onClick={refresh}
            disabled={loading}
            className="rounded-lg border border-neutral-700 px-2 py-1 text-xs text-neutral-400 hover:bg-neutral-800 disabled:opacity-50 transition-colors"
          >
            ↻
          </button>
        </div>
      </div>
      {runMsg && <p className="mb-2 text-xs text-indigo-400">{runMsg}</p>}

      {picks.length === 0 ? (
        <p className="text-xs text-neutral-500">
          No options picks yet — runs Mon–Fri at 9:15 AM ET after pre-market confirms watchlist.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {picks.map((p, i) => {
            const isCall = p.option_type === "call";
            const gainPct =
              p.entry_price > 0
                ? Math.round(((p.target_price - p.entry_price) / p.entry_price) * 100)
                : 0;
            return (
              <div
                key={i}
                className="rounded-lg border border-neutral-700/60 bg-neutral-800/40 p-3"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <span className="font-semibold text-white">{p.symbol}</span>
                    <span
                      className={`ml-2 rounded px-1.5 py-0.5 text-xs font-medium ${
                        isCall
                          ? "bg-emerald-400/15 text-emerald-300"
                          : "bg-rose-400/15 text-rose-300"
                      }`}
                    >
                      {p.option_type?.toUpperCase()} ${p.strike} exp {p.expiry}
                    </span>
                  </div>
                  <span
                    className={`text-xs font-semibold ${gainPct >= 0 ? "text-emerald-400" : "text-rose-400"}`}
                  >
                    +{gainPct}% target
                  </span>
                </div>

                <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-neutral-400">
                  <div>
                    <span className="text-neutral-600">Entry</span>
                    <p className="text-neutral-200">${p.entry_price?.toFixed(2)}</p>
                  </div>
                  <div>
                    <span className="text-neutral-600">Target</span>
                    <p className="text-emerald-300">${p.target_price?.toFixed(2)}</p>
                  </div>
                  <div>
                    <span className="text-neutral-600">Underlying Stop</span>
                    <p className="text-rose-300">${p.underlying_stop?.toFixed(2)}</p>
                  </div>
                </div>

                <div className="mt-2 flex gap-4 text-xs text-neutral-500">
                  <span>OI: {p.open_interest?.toLocaleString()}</span>
                  <span>IV: {p.iv != null ? `${(p.iv * 100).toFixed(0)}%` : "—"}</span>
                </div>

                {p.reason && (
                  <p className="mt-1.5 text-xs text-neutral-500 italic">{p.reason}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-3 text-xs text-neutral-600">
        Suggestions only — buy manually on Robinhood. Sell before 3:45 PM ET to avoid theta decay.
      </p>
    </div>
  );
}

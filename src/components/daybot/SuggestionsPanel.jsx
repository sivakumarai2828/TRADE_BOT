import { useAuth } from "../../AuthContext.jsx";
import { useEffect, useState } from "react";
import { fetchSuggestions, runEveningAnalysis } from "../../api.js";

const DIRECTION_COLOR = { BUY: "text-emerald-400", SELL: "text-rose-400" };

export default function SuggestionsPanel() {
  const { isOwner } = useAuth();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");

  const refresh = () => {
    setLoading(true);
    fetchSuggestions()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, []);

  const handleRunNow = async () => {
    setRunning(true);
    setRunMsg("");
    try {
      const res = await runEveningAnalysis();
      setRunMsg(res.message ?? "Analysis started…");
      setTimeout(() => { refresh(); setRunning(false); }, 65000);
    } catch (e) {
      setRunMsg("Failed: " + e.message);
      setRunning(false);
    }
  };

  const suggestions = data?.suggestions ?? [];
  const regime = data?.regime ?? "";

  if (loading) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 text-sm text-neutral-500">
        Loading suggestions…
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">AI Stock Suggestions</h3>
        <div className="flex items-center gap-2">
          {regime && (
            <span className="rounded-full border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400">
              {regime.replace(/_/g, " ")}
            </span>
          )}
          <button
            onClick={handleRunNow}
            disabled={running || !isOwner}
            title={!isOwner ? "View-only access" : undefined}
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

      {suggestions.length === 0 ? (
        <p className="text-xs text-neutral-500">
          No suggestions yet — evening analysis runs Mon–Fri at 8 PM ET.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-neutral-800 text-neutral-500">
                <th className="pb-2 text-left font-normal">Symbol</th>
                <th className="pb-2 text-left font-normal">Direction</th>
                <th className="pb-2 text-right font-normal">Entry Zone</th>
                <th className="pb-2 text-right font-normal">Stop</th>
                <th className="pb-2 text-right font-normal">Target</th>
                <th className="pb-2 text-left font-normal pl-3">Note</th>
              </tr>
            </thead>
            <tbody>
              {suggestions.map((s) => (
                <tr key={s.symbol} className="border-b border-neutral-800/50 hover:bg-neutral-800/30">
                  <td className="py-2 font-semibold text-white">{s.symbol}</td>
                  <td className={`py-2 font-semibold ${DIRECTION_COLOR[s.direction] ?? "text-neutral-300"}`}>
                    {s.direction}
                  </td>
                  <td className="py-2 text-right text-neutral-300">
                    {s.entry_low != null && s.entry_high != null
                      ? `$${s.entry_low.toFixed(2)}–$${s.entry_high.toFixed(2)}`
                      : "—"}
                  </td>
                  <td className="py-2 text-right text-rose-400">
                    {s.stop != null ? `$${s.stop.toFixed(2)}` : "—"}
                  </td>
                  <td className="py-2 text-right text-emerald-400">
                    {s.target != null ? `$${s.target.toFixed(2)}` : "—"}
                  </td>
                  <td className="py-2 pl-3 text-neutral-500 max-w-[200px] truncate">{s.note || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs text-neutral-600">
        Suggestions only — trade manually on Robinhood. Updated nightly by evening AI agent.
      </p>
    </div>
  );
}

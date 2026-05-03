import { useEffect, useState } from "react";
import { fetchIndiaSuggestions, runIndiaAnalysis } from "../../api.js";

const DIRECTION_COLOR = { BUY: "text-emerald-400", SELL: "text-rose-400" };

export default function IndiaSuggestions() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");

  const refresh = () => {
    setLoading(true);
    fetchIndiaSuggestions()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, []);

  const handleRunNow = async () => {
    setRunning(true);
    setRunMsg("");
    try {
      const res = await runIndiaAnalysis();
      setRunMsg(res.message ?? "Analysis started…");
      // Poll for results after 35s
      setTimeout(() => { refresh(); setRunning(false); }, 35000);
    } catch (e) {
      setRunMsg("Failed: " + e.message);
      setRunning(false);
    }
  };

  const suggestions = data?.suggestions ?? [];
  const regime = data?.regime ?? "";
  const analysisDate = data?.analysis_date ?? "";

  if (loading) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 text-sm text-neutral-500">
        Loading India picks…
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-white">India (NSE) Picks</h3>
          <span className="text-xs text-neutral-500">🇮🇳</span>
        </div>
        <div className="flex items-center gap-2">
          {regime && (
            <span className="rounded-full border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400">
              {regime}
            </span>
          )}
          {analysisDate && (
            <span className="text-xs text-neutral-600">{analysisDate}</span>
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
      {runMsg && (
        <p className="mb-2 text-xs text-indigo-400">{runMsg}</p>
      )}

      {suggestions.length === 0 ? (
        <p className="text-xs text-neutral-500">
          No India picks yet — evening analysis runs Mon–Fri at 4:30 PM IST.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-neutral-800 text-neutral-500">
                <th className="pb-2 text-left font-normal">Symbol</th>
                <th className="pb-2 text-left font-normal">Dir</th>
                <th className="pb-2 text-right font-normal">Entry Zone (₹)</th>
                <th className="pb-2 text-right font-normal">Stop (₹)</th>
                <th className="pb-2 text-right font-normal">Target (₹)</th>
                <th className="pb-2 text-left font-normal pl-3">Note</th>
              </tr>
            </thead>
            <tbody>
              {suggestions.map((s) => (
                <tr key={s.symbol} className="border-b border-neutral-800/50 hover:bg-neutral-800/30">
                  <td className="py-2 font-semibold text-white">{s.display || s.symbol}</td>
                  <td className={`py-2 font-semibold ${DIRECTION_COLOR[s.direction] ?? "text-neutral-300"}`}>
                    {s.direction}
                  </td>
                  <td className="py-2 text-right text-neutral-300">
                    {s.entry_low != null && s.entry_high != null
                      ? `₹${s.entry_low.toFixed(1)}–₹${s.entry_high.toFixed(1)}`
                      : "—"}
                  </td>
                  <td className="py-2 text-right text-rose-400">
                    {s.stop != null ? `₹${s.stop.toFixed(1)}` : "—"}
                  </td>
                  <td className="py-2 text-right text-emerald-400">
                    {s.target != null ? `₹${s.target.toFixed(1)}` : "—"}
                  </td>
                  <td className="py-2 pl-3 text-neutral-500 max-w-[200px] truncate">{s.note || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs text-neutral-600">
        NSE suggestions only — trade manually on Zerodha/Groww. Prices via yfinance (15-min delayed).
      </p>
    </div>
  );
}

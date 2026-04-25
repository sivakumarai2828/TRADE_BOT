import { useCallback, useEffect, useState } from "react";
import { fetchDayStatus } from "./api.js";
import DayBotControls from "./components/daybot/DayBotControls.jsx";
import DayBotLogs from "./components/daybot/DayBotLogs.jsx";
import DayBotMetrics from "./components/daybot/DayBotMetrics.jsx";
import DayBotPositions from "./components/daybot/DayBotPositions.jsx";
import DayBotSignals from "./components/daybot/DayBotSignals.jsx";
import MyPositions from "./components/daybot/MyPositions.jsx";
import OptionsSuggestions from "./components/daybot/OptionsSuggestions.jsx";
import SuggestionsPanel from "./components/daybot/SuggestionsPanel.jsx";

const POLL_MS = 5_000;

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "suggestions", label: "Stock Picks" },
  { id: "options", label: "Options Picks" },
  { id: "mypositions", label: "My Positions" },
];

export default function DayBotPage() {
  const [data, setData] = useState(null);
  const [apiError, setApiError] = useState(false);
  const [tab, setTab] = useState("overview");

  const refresh = useCallback(async () => {
    try {
      const result = await fetchDayStatus();
      setData(result);
      setApiError(false);
    } catch {
      setApiError(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 px-4 pb-8 pt-4 sm:px-6 lg:px-8">

      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Day Trading Bot</h2>
          <p className="text-xs text-neutral-500">US equities · Alpaca paper account · 1-min bars</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${data?.running ? "bg-emerald-400" : "bg-neutral-600"}`} />
          <span className="text-xs text-neutral-400">{data?.running ? "Running" : "Stopped"}</span>
          {data?.metrics?.market_open && (
            <span className="ml-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 text-xs text-emerald-300">
              Market Open
            </span>
          )}
        </div>
      </div>

      {apiError && (
        <div className="rounded-lg border border-amber-400/25 bg-amber-400/10 px-4 py-3 text-sm text-amber-200">
          Day bot API unreachable — ensure Flask is running and <code className="rounded bg-neutral-900 px-1 py-0.5 text-xs">/daybot/*</code> endpoints are reachable.
        </div>
      )}

      {/* Tab bar */}
      <div className="flex gap-1 border-b border-neutral-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-xs font-medium transition-colors ${
              tab === t.id
                ? "border-b-2 border-indigo-400 text-indigo-300"
                : "text-neutral-500 hover:text-neutral-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Overview tab — original layout */}
      {tab === "overview" && (
        <>
          <DayBotMetrics metrics={data?.metrics} />
          <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(300px,0.6fr)]">
            <DayBotPositions positions={data?.positions} />
            <DayBotSignals signals={data?.signals} watchlist={data?.watchlist} />
          </section>
          <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(300px,0.6fr)]">
            <DayBotLogs logs={data?.logs} />
            <DayBotControls running={data?.running} metrics={data?.metrics} onRefresh={refresh} />
          </section>
        </>
      )}

      {tab === "suggestions" && <SuggestionsPanel />}
      {tab === "options" && <OptionsSuggestions />}
      {tab === "mypositions" && <MyPositions />}
    </main>
  );
}

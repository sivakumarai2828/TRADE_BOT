import { useCallback, useEffect, useState } from "react";
import { fetchHealth, fetchStatus } from "./api.js";

import BotControls from "./components/BotControls.jsx";
import ChartPanel from "./components/ChartPanel.jsx";
import LogsPanel from "./components/LogsPanel.jsx";
import MetricsCards from "./components/MetricsCards.jsx";
import PositionsTable from "./components/PositionsTable.jsx";
import Sidebar from "./components/Sidebar.jsx";
import SignalPanel from "./components/SignalPanel.jsx";
import Topbar from "./components/Topbar.jsx";
import DayBotPage from "./DayBotPage.jsx";

const POLL_MS = 3_000;

export default function App() {
  const [activePage, setActivePage] = useState("crypto");
  const [data, setData] = useState(null);
  const [apiError, setApiError] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [tick, setTick] = useState(0);
  const [health, setHealth] = useState(null); // {ok, uptime_s, scheduler}

  const refresh = useCallback(async () => {
    try {
      const result = await fetchStatus();
      setData(result);
      setApiError(false);
      setLastUpdated(Date.now());
    } catch {
      setApiError(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Health check every 15 seconds
  useEffect(() => {
    const check = async () => {
      try { setHealth(await fetchHealth()); }
      catch { setHealth(null); }
    };
    check();
    const id = setInterval(check, 15_000);
    return () => clearInterval(id);
  }, []);

  const secondsAgo = lastUpdated
    ? Math.floor((Date.now() - lastUpdated) / 1000)
    : null;

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <Sidebar
        exchangeName={data?.exchange_name}
        paperMode={data?.paper_mode}
        activePage={activePage}
        onNavigate={setActivePage}
      />

      <div className="min-h-screen lg:pl-72">
        <Topbar
          running={data?.running}
          apiError={apiError}
          paperMode={data?.paper_mode}
          secondsAgo={secondsAgo}
          health={health}
        />

        {activePage === "daybot" ? (
          <DayBotPage />
        ) : (
          <>
            {apiError && (
              <div className="mx-auto max-w-[1600px] px-4 pt-3 sm:px-6 lg:px-8">
                <div className="rounded-lg border border-amber-400/25 bg-amber-400/10 px-4 py-3 text-sm text-amber-200">
                  Backend API unreachable — start the Flask server with{" "}
                  <code className="rounded bg-neutral-900 px-1 py-0.5 text-xs text-amber-100">
                    python3 api.py
                  </code>{" "}
                  then the dashboard will update automatically.
                </div>
              </div>
            )}

            <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 px-4 pb-8 pt-4 sm:px-6 lg:px-8">
              <MetricsCards metrics={data?.metrics} analytics={data?.analytics} />

              <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.7fr)_minmax(340px,0.7fr)]">
                <ChartPanel running={data?.running} activeSymbols={data?.settings?.active_symbols} />
                <SignalPanel signals={data?.signals} activeSymbols={data?.settings?.active_symbols} />
              </section>

              <section className="grid grid-cols-1 gap-5 2xl:grid-cols-[minmax(0,1.4fr)_minmax(360px,0.6fr)]">
                <PositionsTable positions={data?.positions} onRefresh={refresh} />
                <div className="grid grid-cols-1 gap-5 xl:grid-cols-2 2xl:grid-cols-1">
                  <BotControls
                    running={data?.running}
                    settings={data?.settings}
                    onRefresh={refresh}
                  />
                  <LogsPanel logs={data?.logs} />
                </div>
              </section>
            </main>
          </>
        )}
      </div>
    </div>
  );
}

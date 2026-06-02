import { useCallback, useEffect, useState } from "react";
import { fetchOptionsBotStatus, startOptionsBot, stopOptionsBot } from "./api.js";

const POLL_MS = 5_000;

function Badge({ children, color = "neutral" }) {
  const colors = {
    positive: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
    negative: "border-red-400/30 bg-red-400/10 text-red-300",
    neutral: "border-neutral-600 bg-neutral-800 text-neutral-300",
    warning: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  };
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${colors[color]}`}>
      {children}
    </span>
  );
}

function ModeChip({ mode }) {
  const map = {
    SAFE: "neutral",
    AGGRESSIVE: "positive",
    SHIELD: "warning",
  };
  return <Badge color={map[mode] ?? "neutral"}>{mode}</Badge>;
}

function MetricCard({ label, value, sub, color }) {
  const colors = {
    positive: "text-emerald-400",
    negative: "text-red-400",
    neutral: "text-white",
  };
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
      <p className="text-xs uppercase tracking-widest text-neutral-500">{label}</p>
      <p className={`mt-2 text-2xl font-bold ${colors[color ?? "neutral"]}`}>{value}</p>
      {sub && <p className="mt-1 text-xs text-neutral-500">{sub}</p>}
    </div>
  );
}

function PositionsTable({ positions }) {
  const entries = Object.values(positions ?? {});
  if (!entries.length) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-neutral-800 bg-neutral-900 p-10 text-sm text-neutral-500">
        No open options positions
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800 bg-neutral-900">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-xs uppercase tracking-widest text-neutral-500">
            {["Symbol", "Type", "Strike", "Expiry", "Entry $", "Current $", "SL", "TP", "PnL", "Entry"].map((h) => (
              <th key={h} className="px-4 py-3 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((p) => {
            const pnlColor = p.pnl >= 0 ? "text-emerald-400" : "text-red-400";
            return (
              <tr key={p.contract_symbol} className="border-b border-neutral-800/60 hover:bg-neutral-800/40">
                <td className="px-4 py-3 font-semibold text-white">{p.symbol}</td>
                <td className="px-4 py-3">
                  <Badge color={p.option_type === "call" ? "positive" : "negative"}>
                    {p.option_type.toUpperCase()}
                  </Badge>
                </td>
                <td className="px-4 py-3 text-neutral-200">${p.strike}</td>
                <td className="px-4 py-3 text-neutral-400">{p.expiry}</td>
                <td className="px-4 py-3 text-neutral-200">${p.entry_premium?.toFixed(2)}</td>
                <td className="px-4 py-3 text-white font-medium">${p.current_premium?.toFixed(2)}</td>
                <td className="px-4 py-3 text-red-400">${p.sl_price?.toFixed(2)}</td>
                <td className="px-4 py-3 text-emerald-400">${p.tp_price?.toFixed(2)}</td>
                <td className={`px-4 py-3 font-semibold ${pnlColor}`}>
                  ${p.pnl?.toFixed(2)} ({p.pnl_pct?.toFixed(1)}%)
                </td>
                <td className="px-4 py-3 text-neutral-500 text-xs">{p.entry_time}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LogsPanel({ logs }) {
  const toneClass = {
    positive: "text-emerald-400",
    negative: "text-red-400",
    neutral: "text-neutral-400",
    warning: "text-amber-400",
  };
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">Activity</h3>
      <div className="space-y-1.5 max-h-72 overflow-y-auto">
        {(logs ?? []).map((l, i) => (
          <div key={i} className="flex gap-2 text-xs">
            <span className="w-16 shrink-0 text-neutral-600">{l.time}</span>
            <span className="w-24 shrink-0 font-medium text-neutral-400">[{l.type}]</span>
            <span className={toneClass[l.tone] ?? "text-neutral-400"}>{l.message}</span>
          </div>
        ))}
        {!logs?.length && <p className="text-xs text-neutral-600">No activity yet</p>}
      </div>
    </div>
  );
}

export default function OptionsBotPage() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchOptionsBotStatus();
      setData(result);
    } catch {
      // silently ignore
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const toggle = async () => {
    setBusy(true);
    try {
      if (data?.running) await stopOptionsBot();
      else await startOptionsBot();
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const m = data?.metrics ?? {};
  const running = data?.running ?? false;
  const pnlColor = (m.daily_pnl ?? 0) >= 0 ? "positive" : "negative";
  const wrColor = (m.win_rate ?? 0) >= 50 ? "positive" : (m.win_rate ?? 0) >= 40 ? "neutral" : "negative";

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 px-4 pb-8 pt-4 sm:px-6 lg:px-8">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Options Bot</h2>
          <p className="text-xs text-neutral-500">
            SPY / QQQ weekly options · Paper trading · RSI + EMA signal · SL 50% · TP 100%
          </p>
        </div>
        <div className="flex items-center gap-3">
          {m.mode && <ModeChip mode={m.mode} />}
          {m.daily_loss_halted && <Badge color="negative">HALTED</Badge>}
          <button
            onClick={toggle}
            disabled={busy}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
              running
                ? "border border-red-400/30 bg-red-400/10 text-red-300 hover:bg-red-400/20"
                : "border border-emerald-400/30 bg-emerald-400/10 text-emerald-300 hover:bg-emerald-400/20"
            } disabled:opacity-50`}
          >
            {busy ? "..." : running ? "Stop Bot" : "Start Bot"}
          </button>
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <MetricCard
          label="Balance"
          value={m.balance != null ? `$${m.balance.toFixed(2)}` : "—"}
          sub="paper account"
        />
        <MetricCard
          label="Today PnL"
          value={`$${(m.daily_pnl ?? 0).toFixed(2)}`}
          sub={`${(m.wins_today ?? 0)}W / ${(m.losses_today ?? 0)}L`}
          color={pnlColor}
        />
        <MetricCard
          label="Win Rate"
          value={`${(m.win_rate ?? 0).toFixed(0)}%`}
          sub={`${m.total_trades ?? 0} total trades`}
          color={wrColor}
        />
        <MetricCard
          label="Open Contracts"
          value={Object.keys(data?.positions ?? {}).length}
          sub={`max ${2} concurrent`}
        />
      </div>

      {/* Positions */}
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Open Positions
        </h3>
        <PositionsTable positions={data?.positions} />
      </div>

      {/* Logs */}
      <LogsPanel logs={data?.logs} />

      {/* Strategy info */}
      <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">Strategy</h3>
        <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          {[
            { label: "Symbols", value: "SPY, QQQ" },
            { label: "Budget/contract", value: "$150" },
            { label: "Stop Loss", value: "−50% premium" },
            { label: "Take Profit", value: "+100% premium" },
            { label: "Expiry", value: "4–10 DTE weekly" },
            { label: "Max open", value: "2 contracts" },
            { label: "Signal", value: "RSI(14) + EMA50" },
            { label: "Confirm", value: "OpenRouter Llama" },
          ].map(({ label, value }) => (
            <div key={label}>
              <p className="text-xs text-neutral-500">{label}</p>
              <p className="mt-0.5 font-medium text-white">{value}</p>
            </div>
          ))}
        </div>
      </div>
    </main>
  );
}

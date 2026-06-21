import { useCallback, useEffect, useState } from "react";
import {
  fetchUserPositionsLive,
  addUserPosition,
  closeUserPosition,
  fetchSuggestions,
} from "./api.js";

const POLL_MS = 15_000;
const GOAL_KEY = "live_weekly_goal";

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

function ProgressBar({ pct, color }) {
  const width = Math.max(0, Math.min(100, pct ?? 0));
  const bar = color === "negative" ? "bg-red-400" : "bg-emerald-400";
  return (
    <div className="h-1.5 w-full rounded-full bg-neutral-800">
      <div className={`h-1.5 rounded-full ${bar}`} style={{ width: `${width}%` }} />
    </div>
  );
}

const fmt = (n, d = 2) => (n == null ? "—" : Number(n).toFixed(d));
const fmtMoney = (n) => (n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`);

// ── 9 AM picks ───────────────────────────────────────────────────────────────
function PicksPanel({ picks, regime }) {
  if (!picks?.length) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5 text-sm text-neutral-500">
        No picks yet — the engine publishes them by ~9:00 AM ET. Check back in the morning.
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Today's Picks
        </h3>
        {regime && <Badge color={regime === "trending_up" ? "positive" : regime === "trending_down" ? "negative" : "neutral"}>{regime}</Badge>}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-neutral-800 text-left text-xs uppercase tracking-widest text-neutral-500">
              {["Symbol", "Dir", "Entry zone", "Stop", "Target", "Note"].map((h) => (
                <th key={h} className="px-3 py-2 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {picks.map((p) => (
              <tr key={p.symbol} className="border-b border-neutral-800/60">
                <td className="px-3 py-2 font-semibold text-white">{p.symbol}</td>
                <td className="px-3 py-2">
                  <Badge color={p.direction === "BUY" ? "positive" : "negative"}>{p.direction}</Badge>
                </td>
                <td className="px-3 py-2 text-neutral-300">
                  {p.entry_low != null ? `$${fmt(p.entry_low)}–$${fmt(p.entry_high)}` : "—"}
                </td>
                <td className="px-3 py-2 text-red-300">{p.stop != null ? `$${fmt(p.stop)}` : "—"}</td>
                <td className="px-3 py-2 text-emerald-300">{p.target != null ? `$${fmt(p.target)}` : "—"}</td>
                <td className="px-3 py-2 text-neutral-500 text-xs">{p.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Log a buy ─────────────────────────────────────────────────────────────────
const EMPTY = { symbol: "", qty: "", entry: "", stopPct: "5", targetDollar: "100", notes: "" };

function LogForm({ onAdded }) {
  const [f, setF] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const set = (k) => (e) => setF((prev) => ({ ...prev, [k]: e.target.value }));

  const entry = parseFloat(f.entry) || 0;
  const qty = parseFloat(f.qty) || 0;
  const stopPct = parseFloat(f.stopPct) || 0;
  const targetDollar = parseFloat(f.targetDollar) || 0;
  const stopPrice = entry > 0 ? entry * (1 - stopPct / 100) : 0;
  const targetPrice = entry > 0 && qty > 0 ? entry + targetDollar / qty : 0;
  const cost = entry * qty;

  const submit = async () => {
    setErr("");
    if (!f.symbol || !qty || !entry) {
      setErr("Symbol, shares and entry price are required.");
      return;
    }
    setBusy(true);
    try {
      await addUserPosition({
        symbol: f.symbol.trim().toUpperCase(),
        side: "BUY",
        asset_type: "stock",
        qty,
        entry_price: entry,
        stop_price: stopPrice > 0 ? Number(stopPrice.toFixed(2)) : null,
        target_price: targetPrice > 0 ? Number(targetPrice.toFixed(2)) : null,
        notes: f.notes,
      });
      setF(EMPTY);
      onAdded?.();
    } catch (e) {
      setErr(e.message || "Failed to log position.");
    } finally {
      setBusy(false);
    }
  };

  const Input = ({ label, value, onChange, placeholder, type = "text" }) => (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-neutral-500">{label}</span>
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        className="rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-white outline-none focus:border-emerald-400/50"
      />
    </label>
  );

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
      <h3 className="mb-4 text-xs font-semibold uppercase tracking-widest text-neutral-500">
        Log a Buy
      </h3>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Input label="Symbol" value={f.symbol} onChange={set("symbol")} placeholder="AMD" />
        <Input label="Shares" value={f.qty} onChange={set("qty")} placeholder="5" type="number" />
        <Input label="Entry price $" value={f.entry} onChange={set("entry")} placeholder="150.00" type="number" />
        <Input label="Stop loss %" value={f.stopPct} onChange={set("stopPct")} placeholder="5" type="number" />
        <Input label="Profit target $" value={f.targetDollar} onChange={set("targetDollar")} placeholder="100" type="number" />
        <Input label="Notes" value={f.notes} onChange={set("notes")} placeholder="optional" />
      </div>

      {entry > 0 && qty > 0 && (
        <div className="mt-3 grid grid-cols-3 gap-3 rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs">
          <div>
            <p className="text-neutral-500">Cost</p>
            <p className="font-medium text-white">${cost.toFixed(2)}</p>
          </div>
          <div>
            <p className="text-neutral-500">Stop @ (−{stopPct}%)</p>
            <p className="font-medium text-red-300">${stopPrice.toFixed(2)}</p>
          </div>
          <div>
            <p className="text-neutral-500">Target @ (+${targetDollar})</p>
            <p className="font-medium text-emerald-300">${targetPrice.toFixed(2)}</p>
          </div>
        </div>
      )}

      {err && <p className="mt-3 text-xs text-red-400">{err}</p>}

      <button
        onClick={submit}
        disabled={busy}
        className="mt-4 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-4 py-2 text-sm font-medium text-emerald-300 transition hover:bg-emerald-400/20 disabled:opacity-50"
      >
        {busy ? "Saving…" : "Log Position"}
      </button>
    </div>
  );
}

// ── Open positions ──────────────────────────────────────────────────────────
function OpenPositions({ positions, onClose }) {
  if (!positions?.length) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-neutral-800 bg-neutral-900 p-10 text-sm text-neutral-500">
        No open positions. Log a buy above and it'll be monitored 24/7.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800 bg-neutral-900">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-xs uppercase tracking-widest text-neutral-500">
            {["Symbol", "Qty", "Entry", "Current", "Unreal. PnL", "To Target", "To Stop", "Days", ""].map((h) => (
              <th key={h} className="px-3 py-3 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const pnl = p.unrealized_pnl;
            const pnlColor = pnl == null ? "text-neutral-400" : pnl >= 0 ? "text-emerald-400" : "text-red-400";
            return (
              <tr key={p.id} className="border-b border-neutral-800/60 hover:bg-neutral-800/40">
                <td className="px-3 py-3 font-semibold text-white">{p.symbol}</td>
                <td className="px-3 py-3 text-neutral-300">{fmt(p.qty, 0)}</td>
                <td className="px-3 py-3 text-neutral-300">${fmt(p.entry_price)}</td>
                <td className="px-3 py-3 font-medium text-white">${fmt(p.current_price)}</td>
                <td className={`px-3 py-3 font-semibold ${pnlColor}`}>
                  {fmtMoney(pnl)}
                  {p.unrealized_pnl_pct != null && (
                    <span className="ml-1 text-xs opacity-70">({p.unrealized_pnl_pct > 0 ? "+" : ""}{fmt(p.unrealized_pnl_pct, 1)}%)</span>
                  )}
                </td>
                <td className="px-3 py-3 w-28">
                  <div className="flex items-center gap-2">
                    <ProgressBar pct={p.pct_to_target} />
                    <span className="text-[10px] text-neutral-500">{p.pct_to_target != null ? `${p.pct_to_target}%` : ""}</span>
                  </div>
                  <p className="mt-0.5 text-[10px] text-emerald-300/70">${fmt(p.target_price)}</p>
                </td>
                <td className="px-3 py-3 w-28">
                  <div className="flex items-center gap-2">
                    <ProgressBar pct={p.pct_to_stop} color="negative" />
                    <span className="text-[10px] text-neutral-500">{p.pct_to_stop != null ? `${p.pct_to_stop}%` : ""}</span>
                  </div>
                  <p className="mt-0.5 text-[10px] text-red-300/70">${fmt(p.stop_price)}</p>
                </td>
                <td className="px-3 py-3 text-neutral-400">{p.days_held ?? "—"}</td>
                <td className="px-3 py-3">
                  <button
                    onClick={() => onClose(p)}
                    className="rounded-md border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:border-red-400/40 hover:text-red-300"
                  >
                    Close
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function LiveTradingPage() {
  const [live, setLive] = useState(null);
  const [picks, setPicks] = useState(null);
  const [goal, setGoal] = useState(() => Number(localStorage.getItem(GOAL_KEY)) || 50);

  const refresh = useCallback(async () => {
    try { setLive(await fetchUserPositionsLive()); } catch { /* ignore */ }
    try { setPicks(await fetchSuggestions()); } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const setGoalPersist = (v) => {
    setGoal(v);
    localStorage.setItem(GOAL_KEY, String(v));
  };

  const close = async (p) => {
    const px = window.prompt(`Exit price for ${p.symbol}? (current ~$${fmt(p.current_price)})`, p.current_price ? fmt(p.current_price) : "");
    if (px == null) return;
    const exitPrice = parseFloat(px);
    if (!exitPrice) return;
    try {
      await closeUserPosition(p.id, exitPrice, "manual");
      await refresh();
    } catch (e) {
      window.alert(e.message || "Close failed");
    }
  };

  const s = live?.summary ?? {};
  const total = s.total_pnl ?? 0;
  const goalPct = goal > 0 ? Math.max(0, Math.min(100, (total / goal) * 100)) : 0;

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 px-4 pb-8 pt-4 sm:px-6 lg:px-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Live Trading</h2>
          <p className="text-xs text-neutral-500">
            Manual Robinhood trades · You buy/sell · Bot monitors 24/7 · Alerts via dashboard + Telegram
          </p>
        </div>
        <Badge color="warning">ALERT-ONLY · NO AUTO-TRADE</Badge>
      </div>

      {/* Weekly tracker */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <MetricCard
          label="Week Total PnL"
          value={fmtMoney(s.total_pnl)}
          sub={`realized ${fmtMoney(s.realized_pnl)} + open ${fmtMoney(s.unrealized_pnl)}`}
          color={total >= 0 ? "positive" : "negative"}
        />
        <MetricCard
          label="Closed (wk)"
          value={`${s.wins ?? 0}/${s.closed_trades ?? 0}`}
          sub="wins / trades"
        />
        <MetricCard label="Open" value={s.open_count ?? 0} sub="positions monitored" />
        <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
          <div className="flex items-center justify-between">
            <p className="text-xs uppercase tracking-widest text-neutral-500">Weekly goal</p>
            <input
              type="number"
              value={goal}
              onChange={(e) => setGoalPersist(Number(e.target.value))}
              className="w-16 rounded border border-neutral-700 bg-neutral-950 px-1.5 py-0.5 text-right text-sm text-white outline-none"
            />
          </div>
          <p className="mt-2 text-sm font-medium text-white">{fmtMoney(total)} / ${goal}</p>
          <div className="mt-2"><ProgressBar pct={goalPct} color={total >= 0 ? "positive" : "negative"} /></div>
        </div>
      </div>

      {/* Picks */}
      <PicksPanel picks={picks?.suggestions} regime={picks?.regime} />

      {/* Log + positions */}
      <LogForm onAdded={refresh} />

      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Open Positions
        </h3>
        <OpenPositions positions={live?.positions} onClose={close} />
      </div>

      {/* How it works */}
      <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">How it works</h3>
        <ul className="space-y-1.5 text-sm text-neutral-400">
          <li>• ~9:00 AM ET — engine publishes picks above (entry zone / stop / target).</li>
          <li>• You buy in Robinhood, then log it here (or ask Claude to log it via MCP).</li>
          <li>• Every 5 min during market hours the bot checks each position vs your stop & target.</li>
          <li>• Hit target or stop → alert in dashboard + Telegram. You decide to sell.</li>
          <li>• The bot never places trades. It only watches and alerts.</li>
        </ul>
      </div>
    </main>
  );
}

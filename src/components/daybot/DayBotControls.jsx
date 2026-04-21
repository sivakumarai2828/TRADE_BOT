import { useState } from "react";
import { startDayBot, stopDayBot, updateDaySettings } from "../../api.js";

const MODES = [
  { value: "compound", label: "Compound %", desc: "Sizes off current portfolio — profits compound" },
  { value: "fixed", label: "Fixed %", desc: "Always same % of starting portfolio" },
  { value: "house_money", label: "House Money", desc: "Trades only accumulated profit, never principal" },
];

export default function DayBotControls({ running, metrics, onRefresh }) {
  const m = metrics ?? {};
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [posSize, setPosSize] = useState((m.position_size_pct ?? 0.05) * 100);
  const [shieldStreak, setShieldStreak] = useState(2);

  async function handleStart() {
    setLoading(true); setError(null);
    try { await startDayBot(); await onRefresh(); }
    catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }

  async function handleStop() {
    setLoading(true); setError(null);
    try { await stopDayBot(); await onRefresh(); }
    catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }

  async function handleMode(mode) {
    try { await updateDaySettings({ trade_mode: mode }); await onRefresh(); }
    catch (e) { setError(e.message); }
  }

  async function applySize() {
    try {
      await updateDaySettings({ position_size_pct: posSize / 100 });
      await onRefresh();
    } catch (e) { setError(e.message); }
  }

  async function applyShield() {
    try {
      await updateDaySettings({ shield_loss_streak: shieldStreak });
      await onRefresh();
    } catch (e) { setError(e.message); }
  }

  const currentMode = m.trade_mode ?? "compound";

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-5 space-y-5">
      <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">Bot Controls</p>

      {/* Start / Stop */}
      <div className="flex gap-3">
        <button
          onClick={handleStart}
          disabled={running || loading}
          className="flex-1 rounded-lg bg-emerald-500 py-2.5 text-sm font-medium text-white hover:bg-emerald-400 disabled:opacity-40 transition"
        >
          {loading && !running ? "Starting…" : "▶ Start Bot"}
        </button>
        <button
          onClick={handleStop}
          disabled={!running || loading}
          className="flex-1 rounded-lg bg-neutral-700 py-2.5 text-sm font-medium text-white hover:bg-neutral-600 disabled:opacity-40 transition"
        >
          {loading && running ? "Stopping…" : "■ Stop Bot"}
        </button>
      </div>

      {error && (
        <p className="rounded-lg border border-red-400/25 bg-red-400/10 px-3 py-2 text-xs text-red-300">{error}</p>
      )}

      {/* Trade Mode */}
      <div>
        <p className="mb-2 text-xs text-neutral-400">Trade Mode</p>
        <div className="space-y-2">
          {MODES.map(({ value, label, desc }) => (
            <button
              key={value}
              onClick={() => handleMode(value)}
              className={`w-full rounded-lg border px-3 py-2.5 text-left transition ${
                currentMode === value
                  ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-200"
                  : "border-neutral-700 bg-neutral-800/50 text-neutral-300 hover:border-neutral-600"
              }`}
            >
              <p className="text-sm font-medium">{label}</p>
              <p className="mt-0.5 text-xs text-neutral-400">{desc}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Position Size */}
      <div>
        <p className="mb-2 text-xs text-neutral-400">Position Size — <span className="text-white">{posSize.toFixed(1)}%</span> per trade</p>
        <div className="flex gap-2">
          <input
            type="range" min="1" max="20" step="0.5"
            value={posSize}
            onChange={e => setPosSize(Number(e.target.value))}
            className="flex-1 accent-emerald-400"
          />
          <button
            onClick={applySize}
            className="rounded-lg bg-neutral-700 px-3 py-1.5 text-xs text-white hover:bg-neutral-600 transition"
          >
            Apply
          </button>
        </div>
      </div>

      {/* Shield Threshold */}
      <div>
        <p className="mb-2 text-xs text-neutral-400">Shield triggers after <span className="text-white">{shieldStreak}</span> consecutive loss{shieldStreak > 1 ? "es" : ""}</p>
        <div className="flex gap-2">
          <input
            type="range" min="1" max="5" step="1"
            value={shieldStreak}
            onChange={e => setShieldStreak(Number(e.target.value))}
            className="flex-1 accent-amber-400"
          />
          <button
            onClick={applyShield}
            className="rounded-lg bg-neutral-700 px-3 py-1.5 text-xs text-white hover:bg-neutral-600 transition"
          >
            Apply
          </button>
        </div>
      </div>

      {/* Status row */}
      <div className="flex items-center justify-between rounded-lg border border-neutral-800 bg-neutral-800/50 px-3 py-2">
        <span className="text-xs text-neutral-400">Status</span>
        <span className={`text-xs font-medium ${running ? "text-emerald-400" : "text-neutral-500"}`}>
          {running ? "● Running" : "○ Stopped"}
        </span>
      </div>

      {m.daily_loss_halted && (
        <div className="rounded-lg border border-red-400/25 bg-red-400/10 px-3 py-2 text-xs text-red-300">
          ⛔ Daily loss limit hit — trading halted for today
        </div>
      )}
    </div>
  );
}

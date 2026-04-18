import { useEffect, useState } from "react";
import { Pause, Play, SlidersHorizontal, TrendingUp } from "lucide-react";
import { startBot, stopBot, updateSettings } from "../api.js";

const BASE = import.meta.env.VITE_API_URL ?? "";

async function apiDeposit(amount) {
  const res = await fetch(`${BASE}/deposit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount }),
  });
  return res.json();
}

export default function BotControls({ running, settings, onRefresh }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);

  const [tradeSize, setTradeSize] = useState(50);
  const [tradeSizeMode, setTradeSizeMode] = useState("fixed");
  const [tradeSizePct, setTradeSizePct] = useState(50);
  const [stopLoss, setStopLoss] = useState(2);
  const [takeProfit, setTakeProfit] = useState(5);
  const [frequency, setFrequency] = useState(60);
  const [autoMode, setAutoMode] = useState(true);
  const [rsiOversold, setRsiOversold] = useState(30);
  const [rsiOverbought, setRsiOverbought] = useState(70);
  const [depositAmount, setDepositAmount] = useState(100);
  const [houseProfitThreshold, setHouseProfitThreshold] = useState(2);
  const [houseTakeProfit, setHouseTakeProfit] = useState(15);
  const [houseStopLoss, setHouseStopLoss] = useState(50);
  const [activeSymbols, setActiveSymbols] = useState(["BTC/USD"]);
  const [shieldEnabled, setShieldEnabled] = useState(true);
  const [shieldLossStreak, setShieldLossStreak] = useState(5);
  const [shieldWinrateMin, setShieldWinrateMin] = useState(40);
  const [shieldDrawdown, setShieldDrawdown] = useState(10);
  const [shieldRecovery, setShieldRecovery] = useState(55);

  const ALL_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"];
  const SYMBOL_LABELS = { "BTC/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL" };

  useEffect(() => {
    if (!settings) return;
    setTradeSize(settings.trade_size_usdt ?? 50);
    setTradeSizeMode(settings.trade_size_mode ?? "fixed");
    setTradeSizePct(settings.trade_size_pct ?? 50);
    setStopLoss(settings.stop_loss_pct ?? 2);
    setTakeProfit(settings.take_profit_pct ?? 5);
    setFrequency(settings.polling_seconds ?? 60);
    setAutoMode(settings.auto_mode ?? true);
    setRsiOversold(settings.rsi_oversold ?? 30);
    setRsiOverbought(settings.rsi_overbought ?? 70);
    setHouseProfitThreshold(settings.house_profit_threshold ?? 2);
    setHouseTakeProfit(settings.house_take_profit_pct ?? 15);
    setHouseStopLoss(settings.house_stop_loss_pct ?? 50);
    setActiveSymbols(settings.active_symbols ?? ["BTC/USD"]);
    setShieldEnabled(settings.shield_enabled ?? true);
    setShieldLossStreak(settings.shield_loss_streak ?? 5);
    setShieldWinrateMin(settings.shield_winrate_min ?? 40);
    setShieldDrawdown(settings.shield_drawdown_pct ?? 10);
    setShieldRecovery(settings.shield_recovery_winrate ?? 55);
  }, [settings]);

  function toggleSymbol(sym) {
    setActiveSymbols((prev) => {
      const next = prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym];
      const safe = next.length > 0 ? next : [sym];
      handleSettingsChange({ active_symbols: safe });
      return safe;
    });
  }

  async function handleStart() {
    setBusy(true); setError(null); setSuccessMsg(null);
    try {
      await startBot({
        trade_size_usdt: Number(tradeSize),
        trade_size_mode: tradeSizeMode,
        trade_size_pct: Number(tradeSizePct),
        stop_loss_pct: Number(stopLoss),
        take_profit_pct: Number(takeProfit),
        polling_seconds: Number(frequency),
        auto_mode: autoMode,
        rsi_oversold: Number(rsiOversold),
        rsi_overbought: Number(rsiOverbought),
        house_profit_threshold: Number(houseProfitThreshold),
        house_take_profit_pct: Number(houseTakeProfit),
        house_stop_loss_pct: Number(houseStopLoss),
        active_symbols: activeSymbols,
      });
      await onRefresh?.();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function handleStop() {
    setBusy(true); setError(null); setSuccessMsg(null);
    try { await stopBot(); await onRefresh?.(); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function handleDeposit() {
    setBusy(true); setError(null); setSuccessMsg(null);
    try {
      const res = await apiDeposit(Number(depositAmount));
      if (res.ok) {
        setSuccessMsg(`Paper balance set to $${Number(depositAmount).toLocaleString()}`);
        await onRefresh?.();
      } else {
        setError(res.message);
      }
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function handleSettingsChange(patch) {
    try { await updateSettings(patch); }
    catch { /* best-effort */ }
  }

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm text-neutral-400">Automation</p>
          <h3 className="mt-1 text-xl font-semibold tracking-normal text-white">Bot Controls</h3>
        </div>
        <div className="rounded-lg border border-neutral-700 bg-neutral-950 p-2 text-neutral-300">
          <SlidersHorizontal className="h-5 w-5" />
        </div>
      </div>

      {error && (
        <p className="mt-3 rounded-lg border border-red-400/20 bg-red-400/10 px-3 py-2 text-sm text-red-300">{error}</p>
      )}
      {successMsg && (
        <p className="mt-3 rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-sm text-emerald-300">{successMsg}</p>
      )}

      {/* Start / Stop */}
      <div className="mt-5 grid grid-cols-2 gap-3">
        <button onClick={handleStart} disabled={busy || running}
          className="inline-flex items-center justify-center gap-2 rounded-lg bg-emerald-400 px-4 py-3 text-sm font-semibold text-neutral-950 transition hover:bg-emerald-300 disabled:opacity-40">
          <Play className="h-4 w-4" />
          {busy && !running ? "Starting…" : "Start Bot"}
        </button>
        <button onClick={handleStop} disabled={busy || !running}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-red-400/20 bg-red-400/10 px-4 py-3 text-sm font-semibold text-red-200 transition hover:bg-red-400/20 disabled:opacity-40">
          <Pause className="h-4 w-4" />
          {busy && running ? "Stopping…" : "Stop Bot"}
        </button>
      </div>

      {/* Auto mode */}
      <label className="mt-5 flex cursor-pointer items-center justify-between rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        <span>
          <span className="block text-sm font-medium text-white">Auto Mode</span>
          <span className="mt-1 block text-xs text-neutral-500">Execute only confirmed signals</span>
        </span>
        <input type="checkbox" checked={autoMode}
          onChange={(e) => { setAutoMode(e.target.checked); handleSettingsChange({ auto_mode: e.target.checked }); }}
          className="h-5 w-5 accent-emerald-400" />
      </label>

      {/* ── Active Trading Pairs ── */}
      <div className="mt-5 rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        <p className="mb-3 text-xs uppercase tracking-[0.14em] text-neutral-500">Active Trading Pairs</p>
        <div className="flex gap-2">
          {ALL_SYMBOLS.map((sym) => {
            const on = activeSymbols.includes(sym);
            return (
              <button key={sym} onClick={() => toggleSymbol(sym)}
                className={`flex-1 rounded-lg border py-2 text-sm font-medium transition ${
                  on
                    ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                    : "border-neutral-700 text-neutral-500 hover:border-neutral-600 hover:text-neutral-400"
                }`}>
                {SYMBOL_LABELS[sym]}
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-neutral-600">
          {activeSymbols.length === 1
            ? `Trading ${activeSymbols[0]} only.`
            : `Trading ${activeSymbols.length} pairs — ${activeSymbols.map(s => SYMBOL_LABELS[s]).join(", ")}. More pairs = more signals.`}
        </p>
      </div>

      {/* ── Paper Deposit ── */}
      <div className="mt-5 rounded-lg border border-emerald-400/15 bg-emerald-400/5 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-emerald-200">
          <TrendingUp className="h-4 w-4" />
          Paper Deposit (simulate starting balance)
        </div>
        <div className="mt-3 flex gap-2">
          <input type="number" min={1} value={depositAmount}
            onChange={(e) => setDepositAmount(e.target.value)}
            className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-white outline-none focus:border-emerald-400" />
          <button onClick={handleDeposit} disabled={busy}
            className="shrink-0 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-neutral-950 hover:bg-emerald-400 disabled:opacity-40">
            Set
          </button>
        </div>
        <p className="mt-2 text-xs text-neutral-500">Enter any amount — e.g. $100. Resets paper balance.</p>
      </div>

      {/* ── Trade Size Mode ── */}
      <div className="mt-5 rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        <p className="mb-3 text-xs uppercase tracking-[0.14em] text-neutral-500">Trade Size Mode</p>

        <div className="grid grid-cols-3 gap-2">
          <button onClick={() => { setTradeSizeMode("fixed"); handleSettingsChange({ trade_size_mode: "fixed" }); }}
            className={`rounded-lg border px-3 py-2 text-sm font-medium transition ${
              tradeSizeMode === "fixed"
                ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                : "border-neutral-700 text-neutral-400 hover:border-neutral-600"}`}>
            Fixed $
          </button>
          <button onClick={() => { setTradeSizeMode("percent"); handleSettingsChange({ trade_size_mode: "percent" }); }}
            className={`rounded-lg border px-3 py-2 text-sm font-medium transition ${
              tradeSizeMode === "percent"
                ? "border-amber-400/30 bg-amber-400/10 text-amber-200"
                : "border-neutral-700 text-neutral-400 hover:border-neutral-600"}`}>
            % Compound
          </button>
          <button onClick={() => { setTradeSizeMode("house_money"); handleSettingsChange({ trade_size_mode: "house_money" }); }}
            className={`rounded-lg border px-3 py-2 text-sm font-medium transition ${
              tradeSizeMode === "house_money"
                ? "border-violet-400/30 bg-violet-400/10 text-violet-200"
                : "border-neutral-700 text-neutral-400 hover:border-neutral-600"}`}>
            House $
          </button>
        </div>

        <div className="mt-3">
          {tradeSizeMode === "fixed" ? (
            <label className="space-y-2">
              <span className="text-sm text-neutral-400">Fixed amount per trade ($)</span>
              <input type="number" min={1} max={10000} value={tradeSize}
                onChange={(e) => setTradeSize(e.target.value)}
                onBlur={() => handleSettingsChange({ trade_size_usdt: Number(tradeSize) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-white outline-none focus:border-emerald-400" />
              <p className="text-xs text-neutral-600">Same dollar amount every trade. Profits sit in balance.</p>
            </label>
          ) : tradeSizeMode === "percent" ? (
            <label className="space-y-2">
              <span className="text-sm text-neutral-400">% of balance per trade</span>
              <input type="number" min={1} max={100} step={1} value={tradeSizePct}
                onChange={(e) => setTradeSizePct(e.target.value)}
                onBlur={() => handleSettingsChange({ trade_size_pct: Number(tradeSizePct) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-amber-300 outline-none focus:border-amber-400" />
              <p className="text-xs text-neutral-600">Profits compound into next trade automatically.</p>
            </label>
          ) : (
            <p className="text-xs text-neutral-500">
              Normal trades use fixed size. Profits build a pool — once the pool hits your threshold below, an aggressive trade fires using <span className="text-violet-300">only the profit</span>. Your principal stays untouched.
            </p>
          )}
        </div>
      </div>

      {/* House Money Settings */}
      {tradeSizeMode === "house_money" && (
        <div className="mt-4 rounded-lg border border-violet-400/20 bg-violet-400/5 p-4 space-y-4">
          <p className="text-xs uppercase tracking-[0.14em] text-violet-400">House Money Settings</p>

          <div className="rounded-lg border border-violet-400/10 bg-neutral-950 px-3 py-2 text-xs text-neutral-400 leading-relaxed">
            Example: You deposit <span className="text-white font-medium">$100</span>. Normal trades run as fixed $. Every profit (say <span className="text-emerald-300 font-medium">$2</span>) flows into the profit pool. Once the pool hits your threshold (<span className="text-violet-300 font-medium">$2</span>), the bot bets that entire <span className="text-violet-300 font-medium">$2</span> on an aggressive trade (15% TP / 50% SL). Win or lose — your original <span className="text-white font-medium">$100 principal is never touched</span>.
          </div>

          <label className="space-y-2 block">
            <span className="text-sm text-neutral-400">Profit pool trigger ($)</span>
            <input type="number" min={0.5} step={0.5} value={houseProfitThreshold}
              onChange={(e) => setHouseProfitThreshold(e.target.value)}
              onBlur={() => handleSettingsChange({ house_profit_threshold: Number(houseProfitThreshold) })}
              className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-violet-300 outline-none focus:border-violet-400" />
            <p className="text-xs text-neutral-600">Fire house trade when profit pool reaches this amount.</p>
          </label>

          <div className="grid grid-cols-2 gap-4">
            <label className="space-y-2 block">
              <span className="text-sm text-neutral-400">Aggressive TP (%)</span>
              <input type="number" min={5} max={100} step={1} value={houseTakeProfit}
                onChange={(e) => setHouseTakeProfit(e.target.value)}
                onBlur={() => handleSettingsChange({ house_take_profit_pct: Number(houseTakeProfit) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-emerald-300 outline-none focus:border-emerald-400" />
            </label>
            <label className="space-y-2 block">
              <span className="text-sm text-neutral-400">Wide SL (%)</span>
              <input type="number" min={5} max={100} step={1} value={houseStopLoss}
                onChange={(e) => setHouseStopLoss(e.target.value)}
                onBlur={() => handleSettingsChange({ house_stop_loss_pct: Number(houseStopLoss) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-red-300 outline-none focus:border-red-400" />
            </label>
          </div>
          <p className="text-xs text-neutral-600">High TP + wide SL = let the house money ride or lose it all — principal safe either way.</p>
        </div>
      )}

      {/* Risk settings */}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="space-y-2">
          <span className="text-sm text-neutral-400">Stop-loss (%)</span>
          <input type="number" min={0.1} max={10} step={0.1} value={stopLoss}
            onChange={(e) => setStopLoss(e.target.value)}
            onBlur={() => handleSettingsChange({ stop_loss_pct: Number(stopLoss) })}
            className="w-full rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-3 text-sm text-white outline-none focus:border-emerald-400" />
        </label>
        <label className="space-y-2">
          <span className="text-sm text-neutral-400">Take-profit (%)</span>
          <input type="number" min={0.1} max={50} step={0.1} value={takeProfit}
            onChange={(e) => setTakeProfit(e.target.value)}
            onBlur={() => handleSettingsChange({ take_profit_pct: Number(takeProfit) })}
            className="w-full rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-3 text-sm text-white outline-none focus:border-emerald-400" />
        </label>
        <label className="space-y-2">
          <span className="text-sm text-neutral-400">Frequency</span>
          <select value={frequency}
            onChange={(e) => { setFrequency(Number(e.target.value)); handleSettingsChange({ polling_seconds: Number(e.target.value) }); }}
            className="w-full rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-3 text-sm text-white outline-none focus:border-emerald-400">
            <option value={60}>1 min</option>
            <option value={300}>5 min</option>
            <option value={900}>15 min</option>
          </select>
        </label>
      </div>

      {/* RSI thresholds */}
      <div className="mt-4 rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        <p className="mb-3 text-xs uppercase tracking-[0.14em] text-neutral-500">RSI Thresholds</p>
        <div className="grid grid-cols-2 gap-4">
          <label className="space-y-2">
            <span className="text-sm text-neutral-400">Buy below (RSI)</span>
            <input type="number" min={10} max={90} step={1} value={rsiOversold}
              onChange={(e) => setRsiOversold(e.target.value)}
              onBlur={() => handleSettingsChange({ rsi_oversold: Number(rsiOversold) })}
              className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-emerald-300 outline-none focus:border-emerald-400" />
          </label>
          <label className="space-y-2">
            <span className="text-sm text-neutral-400">Sell above (RSI)</span>
            <input type="number" min={10} max={90} step={1} value={rsiOverbought}
              onChange={(e) => setRsiOverbought(e.target.value)}
              onBlur={() => handleSettingsChange({ rsi_overbought: Number(rsiOverbought) })}
              className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-3 text-sm text-red-300 outline-none focus:border-red-400" />
          </label>
        </div>
        <p className="mt-2 text-xs text-neutral-600">Default: buy &lt; 30, sell &gt; 70</p>
      </div>

      {/* Auto-Shield */}
      <div className="mt-4 rounded-lg border border-amber-400/20 bg-amber-400/5 p-4">
        <div className="flex items-center justify-between">
          <p className="text-xs uppercase tracking-[0.14em] text-amber-400">Auto-Shield</p>
          <input type="checkbox" checked={shieldEnabled}
            onChange={(e) => { setShieldEnabled(e.target.checked); handleSettingsChange({ shield_enabled: e.target.checked }); }}
            className="h-5 w-5 accent-amber-400" />
        </div>
        <p className="mt-1 text-xs text-neutral-500">
          Auto-switches to House Money when losses pile up. Switches back when market recovers.
        </p>

        {shieldEnabled && (
          <div className="mt-3 grid grid-cols-2 gap-3">
            <label className="space-y-1.5 block">
              <span className="text-xs text-neutral-400">Trigger after N losses</span>
              <input type="number" min={2} max={20} step={1} value={shieldLossStreak}
                onChange={(e) => setShieldLossStreak(e.target.value)}
                onBlur={() => handleSettingsChange({ shield_loss_streak: Number(shieldLossStreak) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-amber-300 outline-none focus:border-amber-400" />
            </label>
            <label className="space-y-1.5 block">
              <span className="text-xs text-neutral-400">Trigger if win rate below (%)</span>
              <input type="number" min={10} max={60} step={1} value={shieldWinrateMin}
                onChange={(e) => setShieldWinrateMin(e.target.value)}
                onBlur={() => handleSettingsChange({ shield_winrate_min: Number(shieldWinrateMin) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-amber-300 outline-none focus:border-amber-400" />
            </label>
            <label className="space-y-1.5 block">
              <span className="text-xs text-neutral-400">Trigger if balance drops (%)</span>
              <input type="number" min={2} max={50} step={1} value={shieldDrawdown}
                onChange={(e) => setShieldDrawdown(e.target.value)}
                onBlur={() => handleSettingsChange({ shield_drawdown_pct: Number(shieldDrawdown) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-amber-300 outline-none focus:border-amber-400" />
            </label>
            <label className="space-y-1.5 block">
              <span className="text-xs text-neutral-400">Recover when win rate above (%)</span>
              <input type="number" min={40} max={80} step={1} value={shieldRecovery}
                onChange={(e) => setShieldRecovery(e.target.value)}
                onBlur={() => handleSettingsChange({ shield_recovery_winrate: Number(shieldRecovery) })}
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-emerald-300 outline-none focus:border-emerald-400" />
            </label>
          </div>
        )}
      </div>
    </section>
  );
}

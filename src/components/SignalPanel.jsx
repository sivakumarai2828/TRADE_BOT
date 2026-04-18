import { useState } from "react";
import { Bot, CheckCircle2, TrendingDown, TrendingUp } from "lucide-react";

function actionTheme(action) {
  if (action === "BUY")
    return {
      wrapper: "border-emerald-400/20 bg-emerald-400/10",
      label: "text-emerald-200",
      value: "text-emerald-300",
      icon: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300",
    };
  if (action === "SELL")
    return {
      wrapper: "border-red-400/20 bg-red-400/10",
      label: "text-red-200",
      value: "text-red-300",
      icon: "border-red-400/20 bg-red-400/10 text-red-300",
    };
  return {
    wrapper: "border-neutral-700 bg-neutral-800",
    label: "text-neutral-400",
    value: "text-neutral-200",
    icon: "border-neutral-700 bg-neutral-800 text-neutral-300",
  };
}

const SYMBOL_LABELS = { "BTC/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL", "BTC/USDT": "BTC", "ETH/USDT": "ETH", "SOL/USDT": "SOL" };

export default function SignalPanel({ signals = {}, activeSymbols = ["BTC/USDT"] }) {
  const tabs = activeSymbols.length > 0 ? activeSymbols : ["BTC/USDT"];
  const [selected, setSelected] = useState(tabs[0]);
  const activeTab = tabs.includes(selected) ? selected : tabs[0];

  const signal = signals?.[activeTab];
  const action = signal?.action ?? "HOLD";
  const theme = actionTheme(action);
  const isUptrend = signal?.trend === "Uptrend";

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm text-neutral-400">AI Signal</p>
          <h3 className="mt-1 text-xl font-semibold tracking-normal text-white">Market Analysis</h3>
        </div>
        <div className={`rounded-lg border p-2 ${theme.icon}`}>
          <Bot className="h-5 w-5" />
        </div>
      </div>

      {/* Symbol tabs */}
      {tabs.length > 1 && (
        <div className="mt-4 flex gap-2">
          {tabs.map((sym) => (
            <button key={sym} onClick={() => setSelected(sym)}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
                activeTab === sym
                  ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                  : "border-neutral-700 text-neutral-400 hover:border-neutral-600"
              }`}>
              {SYMBOL_LABELS[sym] ?? sym}
            </button>
          ))}
        </div>
      )}

      {/* Action + confidence */}
      <div className={`mt-4 rounded-lg border p-5 ${theme.wrapper}`}>
        <p className={`text-sm ${theme.label}`}>{activeTab} Signal</p>
        <div className="mt-3 flex items-end justify-between gap-4">
          <span className={`text-4xl font-bold tracking-normal ${theme.value}`}>{action}</span>
          <span className="rounded-lg bg-neutral-950 px-3 py-2 text-sm text-neutral-200">
            {signal ? `${signal.confidence}% confidence` : "—"}
          </span>
        </div>
      </div>

      {/* RSI + Trend */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-4">
          <p className="text-sm text-neutral-400">RSI (14)</p>
          <p className={`mt-2 text-2xl font-semibold ${
            signal?.rsi < 30 ? "text-emerald-300" : signal?.rsi > 70 ? "text-red-300" : "text-white"
          }`}>
            {signal ? signal.rsi.toFixed(1) : "—"}
          </p>
        </div>
        <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-4">
          <p className="text-sm text-neutral-400">Trend</p>
          <p className={`mt-2 flex items-center gap-2 text-lg font-semibold ${
            isUptrend ? "text-emerald-300" : signal?.trend === "Downtrend" ? "text-red-300" : "text-neutral-300"
          }`}>
            {isUptrend ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
            {signal?.trend ?? "—"}
          </p>
        </div>
      </div>

      {signal && (
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-4">
            <p className="text-sm text-neutral-400">Price</p>
            <p className="mt-2 text-lg font-semibold text-white">
              ${signal.price.toLocaleString("en-US", { maximumFractionDigits: 2 })}
            </p>
          </div>
          <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-4">
            <p className="text-sm text-neutral-400">50 SMA</p>
            <p className="mt-2 text-lg font-semibold text-cyan-300">
              ${signal.sma.toLocaleString("en-US", { maximumFractionDigits: 2 })}
            </p>
          </div>
        </div>
      )}

      <div className="mt-4 rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-white">
          <CheckCircle2 className={`h-4 w-4 ${theme.value}`} />
          Decision note
        </div>
        <p className="mt-3 text-sm leading-6 text-neutral-300">
          {signal?.explanation ?? "Waiting for first bot cycle…"}
        </p>
      </div>
    </section>
  );
}

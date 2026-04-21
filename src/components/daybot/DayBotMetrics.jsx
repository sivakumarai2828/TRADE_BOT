import { Activity, ShieldCheck, TrendingUp, Wallet } from "lucide-react";

function fmt(n, decimals = 2) {
  if (n == null || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function Card({ title, icon: Icon, iconClass, value, sub, subClass }) {
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-5">
      <div className="flex items-center justify-between">
        <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">{title}</p>
        <div className={`flex h-8 w-8 items-center justify-center rounded-lg border ${iconClass}`}>
          <Icon className="h-4 w-4" />
        </div>
      </div>
      <p className="mt-3 text-2xl font-semibold text-white">{value}</p>
      {sub && <p className={`mt-1 text-xs ${subClass ?? "text-neutral-400"}`}>{sub}</p>}
    </div>
  );
}

export default function DayBotMetrics({ metrics }) {
  const m = metrics ?? {};
  const pnlPos = (m.daily_pnl ?? 0) >= 0;
  const shieldOn = m.shield_active;

  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <Card
        title="Portfolio Value"
        icon={Wallet}
        iconClass="border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
        value={`$${fmt(m.portfolio_value)}`}
        sub={`Cash: $${fmt(m.cash)}`}
      />
      <Card
        title="Daily P&L"
        icon={TrendingUp}
        iconClass={pnlPos ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300" : "border-red-400/30 bg-red-400/10 text-red-300"}
        value={`${pnlPos ? "+" : ""}$${fmt(m.daily_pnl)}`}
        sub={`${pnlPos ? "+" : ""}${fmt(m.daily_pnl_pct)}% today`}
        subClass={pnlPos ? "text-emerald-400" : "text-red-400"}
      />
      <Card
        title="Trades Today"
        icon={Activity}
        iconClass="border-blue-400/30 bg-blue-400/10 text-blue-300"
        value={m.trades_today ?? 0}
        sub={`${m.wins_today ?? 0}W / ${m.losses_today ?? 0}L`}
      />
      <Card
        title="Auto-Shield"
        icon={ShieldCheck}
        iconClass={shieldOn ? "border-amber-400/30 bg-amber-400/10 text-amber-300" : "border-neutral-700 bg-neutral-800 text-neutral-400"}
        value={shieldOn ? "ACTIVE" : "OFF"}
        sub={shieldOn ? `Mode: ${m.trade_mode ?? "—"} | size ${((m.position_size_pct ?? 0.05) * 100).toFixed(0)}%` : `Mode: ${m.trade_mode ?? "compound"} | size ${((m.position_size_pct ?? 0.05) * 100).toFixed(0)}%`}
        subClass={shieldOn ? "text-amber-400" : "text-neutral-400"}
      />
    </div>
  );
}

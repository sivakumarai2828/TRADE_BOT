import { ArrowDownRight, ArrowUpRight, ShieldCheck, ShieldAlert, Wallet, TrendingUp } from "lucide-react";

function fmt(n, decimals = 2) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function buildCards(metrics) {
  if (!metrics) {
    return [
      { label: "Account Balance", value: "—", detail: "Waiting for API…", tone: "neutral", icon: Wallet },
      { label: "Total PnL", value: "—", detail: "", tone: "neutral", icon: ArrowUpRight },
      { label: "Win Rate", value: "—", detail: "", tone: "neutral", icon: TrendingUp },
      { label: "Auto-Shield", value: "—", detail: "", tone: "neutral", icon: ShieldCheck },
    ];
  }

  const pnlPositive = metrics.pnl >= 0;
  const winRate = metrics.win_rate ?? 0;
  const totalTrades = metrics.total_trades ?? 0;
  const shieldActive = metrics.shield_active ?? false;
  const consecutive = metrics.consecutive_losses ?? 0;

  return [
    {
      label: "Account Balance",
      value: `$${fmt(metrics.balance)}`,
      detail: metrics.balance_detail || "Paper trading",
      tone: "positive",
      icon: Wallet,
    },
    {
      label: "Total PnL",
      value: `${pnlPositive ? "+" : ""}$${fmt(Math.abs(metrics.pnl))}`,
      detail: `${pnlPositive ? "+" : ""}${fmt(metrics.pnl_pct)}% all time`,
      tone: pnlPositive ? "positive" : "negative",
      icon: ArrowUpRight,
    },
    {
      label: "Win Rate",
      value: totalTrades > 0 ? `${fmt(winRate, 1)}%` : "—",
      detail: totalTrades > 0
        ? `${metrics.win_count}W / ${metrics.loss_count}L — ${totalTrades} trades`
        : "No trades yet",
      tone: winRate >= 55 ? "positive" : winRate >= 40 ? "warning" : totalTrades > 0 ? "negative" : "neutral",
      icon: TrendingUp,
    },
    {
      label: "Auto-Shield",
      value: shieldActive ? "ACTIVE" : "Standby",
      detail: shieldActive
        ? "Switched to House Money — principal protected"
        : consecutive > 0
          ? `${consecutive} loss streak — watching`
          : "Monitoring win rate & losses",
      tone: shieldActive ? "warning" : "neutral",
      icon: shieldActive ? ShieldAlert : ShieldCheck,
    },
  ];
}

function toneClasses(tone) {
  if (tone === "positive") return "text-emerald-300 bg-emerald-400/10 border-emerald-400/20";
  if (tone === "negative") return "text-red-300 bg-red-400/10 border-red-400/20";
  if (tone === "warning") return "text-amber-300 bg-amber-400/10 border-amber-400/20";
  return "text-neutral-300 bg-neutral-800 border-neutral-700";
}

function detailColor(tone) {
  if (tone === "positive") return "text-emerald-300";
  if (tone === "negative") return "text-red-300";
  if (tone === "warning") return "text-amber-300";
  return "text-neutral-400";
}

export default function MetricsCards({ metrics }) {
  const cards = buildCards(metrics);

  return (
    <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <article
            key={card.label}
            className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm text-neutral-400">{card.label}</p>
                <p className="mt-3 text-2xl font-semibold tracking-normal text-white">
                  {card.value}
                </p>
              </div>
              <div className={`rounded-lg border p-2 ${toneClasses(card.tone)}`}>
                <Icon className="h-5 w-5" />
              </div>
            </div>
            {card.detail && (
              <p className={`mt-4 text-sm ${detailColor(card.tone)}`}>{card.detail}</p>
            )}
          </article>
        );
      })}
    </section>
  );
}

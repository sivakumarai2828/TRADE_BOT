import {
  Activity,
  Bot,
  Gauge,
  LineChart,
  Settings,
  TrendingUp,
  WalletCards,
} from "lucide-react";

const navItems = [
  { label: "Crypto Bot", icon: Gauge, page: "crypto" },
  { label: "Day Bot", icon: TrendingUp, page: "daybot" },
  { label: "Trading Bot", icon: Bot, page: null },
  { label: "Signals", icon: Activity, page: null },
  { label: "Positions", icon: WalletCards, page: null },
  { label: "Settings", icon: Settings, page: null },
];

export default function Sidebar({ exchangeName, paperMode, activePage, onNavigate }) {
  const modeLabel = paperMode === false ? "Live trading" : "Paper trading";
  const modeNote =
    paperMode === false
      ? "Real orders are being placed."
      : "Simulated orders — no real money at risk.";

  return (
    <aside className="fixed inset-y-0 left-0 z-40 hidden w-72 border-r border-neutral-800 bg-neutral-950/95 px-4 py-5 shadow-premium lg:block">
      <div className="flex items-center gap-3 px-2">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-emerald-400/30 bg-emerald-400/10">
          <LineChart className="h-5 w-5 text-emerald-300" />
        </div>
        <div>
          <p className="text-sm text-neutral-400">Claude Strategy</p>
          <h1 className="text-lg font-semibold tracking-normal text-white">AI Trading Bot</h1>
        </div>
      </div>

      <nav className="mt-8 space-y-2">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = item.page && item.page === activePage;
          const isDisabled = !item.page;
          return (
            <button
              key={item.label}
              onClick={() => item.page && onNavigate(item.page)}
              disabled={isDisabled}
              className={`flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-sm transition ${
                isActive
                  ? "border border-emerald-400/25 bg-emerald-400/10 text-emerald-200"
                  : isDisabled
                  ? "cursor-not-allowed text-neutral-600"
                  : "text-neutral-400 hover:bg-neutral-900 hover:text-white"
              }`}
            >
              <Icon className="h-5 w-5" />
              <span>{item.label}</span>
              {item.page === "daybot" && (
                <span className="ml-auto rounded-full border border-blue-400/30 bg-blue-400/10 px-1.5 py-0.5 text-[10px] text-blue-300">
                  NEW
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="absolute bottom-5 left-4 right-4 space-y-2">
        <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
          <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">Exchange</p>
          <p className="mt-2 text-sm font-medium text-white capitalize">
            {exchangeName ?? "Not connected"}
          </p>
        </div>
        <div
          className={`rounded-lg border p-4 ${
            paperMode === false
              ? "border-emerald-400/20 bg-emerald-400/10"
              : "border-amber-400/20 bg-amber-400/10"
          }`}
        >
          <p className={`text-xs uppercase tracking-[0.18em] ${
            paperMode === false ? "text-emerald-400" : "text-amber-400"
          }`}>
            Mode
          </p>
          <p className={`mt-2 text-sm font-medium ${
            paperMode === false ? "text-emerald-200" : "text-amber-200"
          }`}>
            {modeLabel}
          </p>
          <p className="mt-1 text-xs leading-5 text-neutral-400">{modeNote}</p>
        </div>
      </div>
    </aside>
  );
}

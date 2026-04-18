import {
  Activity,
  Bot,
  Gauge,
  LineChart,
  Settings,
  WalletCards,
} from "lucide-react";

const navItems = [
  { label: "Dashboard", icon: Gauge, active: true },
  { label: "Trading Bot", icon: Bot },
  { label: "Signals", icon: Activity },
  { label: "Positions", icon: WalletCards },
  { label: "Settings", icon: Settings },
];

export default function Sidebar({ exchangeName, paperMode }) {
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
          return (
            <button
              key={item.label}
              className={`flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-sm transition ${
                item.active
                  ? "border border-emerald-400/25 bg-emerald-400/10 text-emerald-200"
                  : "text-neutral-400 hover:bg-neutral-900 hover:text-white"
              }`}
            >
              <Icon className="h-5 w-5" />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="absolute bottom-5 left-4 right-4 space-y-2">
        {/* Exchange info */}
        <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
          <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">Exchange</p>
          <p className="mt-2 text-sm font-medium text-white capitalize">
            {exchangeName ?? "Not connected"}
          </p>
        </div>
        {/* Mode badge */}
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

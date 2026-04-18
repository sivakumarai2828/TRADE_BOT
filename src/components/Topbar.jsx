import { Bell, CircleUserRound, Menu, Power, WifiOff } from "lucide-react";

export default function Topbar({ running, apiError, paperMode, secondsAgo }) {
  return (
    <header className="sticky top-0 z-30 border-b border-neutral-800 bg-neutral-950/92 px-4 py-4 backdrop-blur sm:px-6 lg:px-8">
      <div className="mx-auto flex w-full max-w-[1600px] items-center justify-between gap-4">

        <div className="flex min-w-0 items-center gap-3">
          <button className="flex h-10 w-10 items-center justify-center rounded-lg border border-neutral-800 bg-neutral-900 text-neutral-300 lg:hidden">
            <Menu className="h-5 w-5" />
          </button>
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.18em] text-neutral-500">Dashboard</p>
            <h2 className="truncate text-xl font-semibold tracking-normal text-white sm:text-2xl">
              AI Trading Bot
            </h2>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">

          {/* Last updated pulse */}
          {!apiError && secondsAgo !== null && (
            <div className="hidden items-center gap-2 rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-xs text-neutral-500 sm:flex">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-50" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
              </span>
              {secondsAgo === 0 ? "just now" : `${secondsAgo}s ago`}
            </div>
          )}

          {/* Paper badge */}
          {paperMode && !apiError && (
            <div className="hidden items-center gap-2 rounded-lg border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-sm text-amber-200 sm:flex">
              <span className="text-xs font-semibold uppercase tracking-wide">Paper</span>
            </div>
          )}

          {/* Bot status */}
          {apiError ? (
            <div className="hidden items-center gap-2 rounded-lg border border-red-400/25 bg-red-400/10 px-3 py-2 text-sm text-red-300 sm:flex">
              <WifiOff className="h-4 w-4" />
              <span>API offline</span>
            </div>
          ) : running ? (
            <div className="hidden items-center gap-2 rounded-lg border border-emerald-400/25 bg-emerald-400/10 px-3 py-2 text-sm text-emerald-200 sm:flex">
              <Power className="h-4 w-4" />
              <span>Running</span>
            </div>
          ) : (
            <div className="hidden items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-400 sm:flex">
              <Power className="h-4 w-4" />
              <span>Stopped</span>
            </div>
          )}

          <button className="flex h-10 w-10 items-center justify-center rounded-lg border border-neutral-800 bg-neutral-900 text-neutral-300">
            <Bell className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-3 rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2">
            <CircleUserRound className="h-5 w-5 text-neutral-300" />
            <span className="hidden text-sm text-neutral-200 sm:inline">Trader</span>
          </div>
        </div>

      </div>
    </header>
  );
}

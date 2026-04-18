import { AlertTriangle, CheckCircle2, Clock3 } from "lucide-react";

function logTone(tone) {
  if (tone === "positive") return "border-emerald-400/20 bg-emerald-400/10 text-emerald-300";
  if (tone === "negative") return "border-red-400/20 bg-red-400/10 text-red-300";
  if (tone === "warning") return "border-amber-400/20 bg-amber-400/10 text-amber-300";
  return "border-neutral-700 bg-neutral-800 text-neutral-300";
}

function LogIcon({ tone }) {
  if (tone === "negative" || tone === "warning") return <AlertTriangle className="h-4 w-4" />;
  if (tone === "positive") return <CheckCircle2 className="h-4 w-4" />;
  return <Clock3 className="h-4 w-4" />;
}

const PLACEHOLDER = [
  { time: "—", type: "Waiting", message: "No activity yet — start the bot.", tone: "neutral" },
];

export default function LogsPanel({ logs }) {
  const entries = logs?.length ? logs : PLACEHOLDER;

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-premium">
      <p className="text-sm text-neutral-400">System</p>
      <h3 className="mt-1 text-xl font-semibold tracking-normal text-white">Activity Logs</h3>

      <div className="mt-5 max-h-72 space-y-3 overflow-y-auto pr-1">
        {entries.map((log, i) => (
          <article
            key={`${log.time}-${i}`}
            className="rounded-lg border border-neutral-800 bg-neutral-950 p-4"
          >
            <div className="flex items-start gap-3">
              <div className={`shrink-0 rounded-lg border p-2 ${logTone(log.tone)}`}>
                <LogIcon tone={log.tone} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3">
                  <p className="truncate text-sm font-medium text-white">{log.type}</p>
                  <span className="shrink-0 text-xs text-neutral-500">{log.time}</span>
                </div>
                <p className="mt-1 text-sm leading-5 text-neutral-400">{log.message}</p>
              </div>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

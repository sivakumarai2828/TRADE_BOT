function toneClass(tone) {
  switch (tone) {
    case "positive": return "text-emerald-400";
    case "negative": return "text-red-400";
    case "warning":  return "text-amber-400";
    default:         return "text-neutral-400";
  }
}

export default function DayBotLogs({ logs }) {
  const entries = logs ?? [];
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-5">
      <p className="mb-4 text-xs uppercase tracking-[0.18em] text-neutral-500">Activity Log</p>
      {entries.length === 0 ? (
        <p className="py-6 text-center text-sm text-neutral-500">No activity yet</p>
      ) : (
        <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
          {entries.map((lg, i) => (
            <div key={i} className="flex gap-3 rounded-lg bg-neutral-800/40 px-3 py-2">
              <span className="shrink-0 text-xs text-neutral-600">{lg.time}</span>
              <span className={`shrink-0 text-xs font-medium ${toneClass(lg.tone)}`}>{lg.type}</span>
              <span className="text-xs text-neutral-300 leading-relaxed">{lg.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

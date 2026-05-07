import { formatBytes } from "../lib/format";

export function ProgressBar({
  bytes,
  total,
  active = false,
  complete = false
}: {
  bytes: number | null | undefined;
  total: number | null | undefined;
  active?: boolean;
  complete?: boolean;
}) {
  const rawPct =
    total && total > 0 ? Math.min(100, Math.round(((bytes ?? 0) / total) * 100)) : 0;
  // Worker's 1-Hz progress emitter can drop the final sample, leaving a
  // genuinely-complete download stuck at 93%-ish. When the catalog says
  // status='complete', trust it over the byte count.
  const pct = complete ? 100 : rawPct;
  const hasKnownProgress = complete || (total && total > 0);
  const label = complete ? "100%" : hasKnownProgress ? `${rawPct}%` : active ? `${formatBytes(bytes)} fetched` : "—";
  return (
    <div className="w-32">
      <div className="h-2 overflow-hidden rounded bg-stone-200">
        <div
          className={`h-full bg-pine ${!hasKnownProgress && active ? "animate-pulse" : ""}`}
          style={{ width: hasKnownProgress ? `${pct}%` : active ? "100%" : "0%" }}
        />
      </div>
      <div className="mt-1 text-xs tabular-nums text-stone-600">{label}</div>
    </div>
  );
}

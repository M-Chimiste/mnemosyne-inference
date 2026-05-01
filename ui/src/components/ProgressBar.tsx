import { formatBytes } from "../lib/format";

export function ProgressBar({
  bytes,
  total,
  active = false
}: {
  bytes: number | null | undefined;
  total: number | null | undefined;
  active?: boolean;
}) {
  const pct = total && total > 0 ? Math.min(100, Math.round(((bytes ?? 0) / total) * 100)) : 0;
  const hasKnownProgress = total && total > 0;
  const label = hasKnownProgress ? `${pct}%` : active ? `${formatBytes(bytes)} fetched` : "—";
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

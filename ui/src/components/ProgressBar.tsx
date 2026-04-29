export function ProgressBar({
  bytes,
  total
}: {
  bytes: number | null | undefined;
  total: number | null | undefined;
}) {
  const pct = total && total > 0 ? Math.min(100, Math.round(((bytes ?? 0) / total) * 100)) : 0;
  return (
    <div className="w-32">
      <div className="h-2 overflow-hidden rounded bg-stone-200">
        <div className="h-full bg-pine" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-xs tabular-nums text-stone-600">{total ? `${pct}%` : "—"}</div>
    </div>
  );
}

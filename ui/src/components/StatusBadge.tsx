const palette: Record<string, string> = {
  installed: "border-pine/40 bg-pine/10 text-pine",
  downloading: "border-amber/40 bg-amber/10 text-amber",
  pending: "border-amber/40 bg-amber/10 text-amber",
  queued: "border-amber/40 bg-amber/10 text-amber",
  partial: "border-stone-400 bg-stone-100 text-stone-700",
  error: "border-brick/40 bg-brick/10 text-brick",
  cancelled: "border-stone-400 bg-stone-100 text-stone-700",
  complete: "border-pine/40 bg-pine/10 text-pine"
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const value = status ?? "unknown";
  return (
    <span className={`inline-flex rounded border px-2 py-0.5 text-xs font-medium ${palette[value] ?? "border-line bg-white text-ink"}`}>
      {value}
    </span>
  );
}

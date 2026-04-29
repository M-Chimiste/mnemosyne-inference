import type { CatalogRow } from "../api/types";
import { isCacheOnly } from "../lib/format";

export function SourceBadge({ row }: { row: Pick<CatalogRow, "alias" | "source"> }) {
  const label = isCacheOnly(row) ? "cache-only" : row.source;
  const cls = label === "config"
    ? "border-pine/40 bg-pine/10 text-pine"
    : label === "cache-only"
      ? "border-amber/40 bg-amber/10 text-amber"
      : "border-line bg-white text-ink";
  return <span className={`inline-flex rounded border px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}

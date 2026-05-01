import type { CatalogRow, GpuPlan } from "../api/types";

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(0)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
}

export function formatGb(gb: number | null | undefined): string {
  if (gb === null || gb === undefined) return "—";
  return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
}

export function formatTime(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

export function relativeFromSeconds(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function formatGpuPlan(gpus: GpuPlan | null | undefined): string {
  if (!gpus) return "—";
  if (gpus === "all") return "all visible GPUs";
  return `GPU ${gpus.join(",")}`;
}

export function isCacheOnly(row: Pick<CatalogRow, "alias">): boolean {
  return row.alias.startsWith("__cache__:") || row.alias.startsWith("__cache__/");
}

export function aliasFromModelId(modelId: string): string {
  const raw = modelId.split("/").pop() ?? modelId;
  return raw.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "model";
}

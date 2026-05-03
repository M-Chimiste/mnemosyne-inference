import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, jsonBody } from "./client";
import type { CatalogUpdateRequest, InstallRequest } from "./types";

function invalidateManager(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["status"] });
  qc.invalidateQueries({ queryKey: ["catalog"] });
  qc.invalidateQueries({ queryKey: ["downloads"] });
}

export function useLoad() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (alias: string) =>
      api("/manager/load", { method: "POST", ...jsonBody({ model: alias }) }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useUnload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api("/manager/unload", { method: "POST" }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useReload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api("/manager/reload", { method: "POST" }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useInstallStart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: InstallRequest) =>
      api("/manager/install", { method: "POST", ...jsonBody(body) }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useUpdateCatalogRow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ alias, body }: { alias: string; body: CatalogUpdateRequest }) =>
      api(`/manager/install/${encodeURIComponent(alias)}`, {
        method: "PATCH",
        ...jsonBody(body)
      }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useInstallCancel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (alias: string) =>
      api(`/manager/install/${encodeURIComponent(alias)}/cancel`, { method: "POST" }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useInstallRetry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ alias, force = false }: { alias: string; force?: boolean }) =>
      api(`/manager/install/${encodeURIComponent(alias)}/retry${force ? "?force=true" : ""}`, {
        method: "POST"
      }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useDeleteCache() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ alias, hfModelId }: { alias?: string; hfModelId?: string }) => {
      if (alias) {
        return api(`/manager/install/${encodeURIComponent(alias)}/cache`, { method: "DELETE" });
      }
      return api(`/manager/cache/${encodeURIComponent(hfModelId!)}`, { method: "DELETE" });
    },
    onSuccess: () => invalidateManager(qc)
  });
}

export function useDeleteCatalogRow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (alias: string) =>
      api(`/manager/install/${encodeURIComponent(alias)}`, { method: "DELETE" }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useLegacyDeleteDownload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (modelId: string) =>
      api(`/manager/download/${encodeURIComponent(modelId)}`, { method: "DELETE" }),
    onSuccess: () => invalidateManager(qc)
  });
}

export function useClearInstallDownload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (alias: string) =>
      api(`/manager/install/${encodeURIComponent(alias)}/download`, { method: "DELETE" }),
    onSuccess: () => invalidateManager(qc)
  });
}

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { api } from "./client";
import type {
  CatalogRow,
  DownloadEntry,
  GpuStatus,
  HfSearchEnvelope,
  InstallStatus,
  ManagerStatus,
  StorageLocation
} from "./types";

export function useStatus() {
  return useQuery({
    queryKey: ["status"],
    queryFn: () => api<ManagerStatus>("/manager/status"),
    refetchInterval: 3000
  });
}

export function useGpu() {
  return useQuery({
    queryKey: ["gpu"],
    queryFn: () => api<GpuStatus>("/manager/gpu"),
    refetchInterval: 5000
  });
}

export function useCatalog(includeCacheOnly = false, refetchInterval = 5000) {
  return useQuery({
    queryKey: ["catalog", { includeCacheOnly }],
    queryFn: () =>
      api<{ models: CatalogRow[] }>(
        `/manager/catalog?include_cache_only=${includeCacheOnly ? "true" : "false"}`
      ),
    refetchInterval
  });
}

export function useStorage() {
  return useQuery({
    queryKey: ["storage"],
    queryFn: () => api<{ locations: StorageLocation[] }>("/manager/storage")
  });
}

export function useDownloads() {
  return useQuery({
    queryKey: ["downloads"],
    queryFn: () => api<{ downloads: DownloadEntry[] }>("/manager/downloads"),
    refetchInterval: (query) => {
      const data = query.state.data as { downloads: DownloadEntry[] } | undefined;
      const active = data?.downloads.some((d) => d.status === "queued" || d.status === "pending" || d.status === "downloading");
      return active ? 2000 : 10000;
    }
  });
}

export function useInstall(alias: string | null) {
  return useQuery({
    queryKey: ["install", alias],
    queryFn: () => api<InstallStatus>(`/manager/install/${encodeURIComponent(alias!)}`),
    enabled: Boolean(alias),
    refetchInterval: 2000
  });
}

export function useHfSearch(params: {
  q: string;
  includeVision: boolean;
  filterCompat: boolean;
  pageSize: number;
  enabled: boolean;
}) {
  return useInfiniteQuery<
    HfSearchEnvelope,
    Error,
    InfiniteData<HfSearchEnvelope>,
    [string, string, boolean, boolean, number],
    number
  >({
    queryKey: ["hf-search", params.q, params.includeVision, params.filterCompat, params.pageSize],
    enabled: params.enabled,
    initialPageParam: 1,
    getNextPageParam: (lastPage) => lastPage.next_page ?? undefined,
    queryFn: ({ pageParam }) => {
      const qs = new URLSearchParams({
        q: params.q.trim(),
        page: String(pageParam),
        limit: String(params.pageSize),
        include_vision: String(params.includeVision),
        filter_compat: String(params.filterCompat)
      });
      return api<HfSearchEnvelope>(`/manager/hf/search?${qs.toString()}`);
    }
  });
}

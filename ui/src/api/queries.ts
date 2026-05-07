import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { api } from "./client";
import type {
  CatalogRow,
  DownloadEntry,
  GpuStatus,
  HfFilesResponse,
  HfPipelineTag,
  HfSearchEnvelope,
  HfSortOption,
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

export function useHfFiles(params: { modelId: string | null; revision?: string }) {
  return useQuery({
    queryKey: ["hf-files", params.modelId, params.revision ?? "main"],
    queryFn: () => {
      const qs = new URLSearchParams({ model_id: params.modelId! });
      if (params.revision) qs.set("revision", params.revision);
      return api<HfFilesResponse>(`/manager/hf/files?${qs.toString()}`);
    },
    enabled: Boolean(params.modelId),
    staleTime: 60_000
  });
}

export function useHfSearch(params: {
  q: string;
  pipelineTags: HfPipelineTag[];
  sort: HfSortOption;
  filterCompat: boolean;
  pageSize: number;
  enabled: boolean;
}) {
  const tagsKey = [...params.pipelineTags].sort().join(",");
  return useInfiniteQuery<
    HfSearchEnvelope,
    Error,
    InfiniteData<HfSearchEnvelope>,
    [string, string, string, string, boolean, number],
    number
  >({
    queryKey: ["hf-search", params.q, tagsKey, params.sort, params.filterCompat, params.pageSize],
    enabled: params.enabled,
    initialPageParam: 1,
    getNextPageParam: (lastPage) => lastPage.next_page ?? undefined,
    queryFn: ({ pageParam }) => {
      const qs = new URLSearchParams({
        q: params.q.trim(),
        page: String(pageParam),
        limit: String(params.pageSize),
        sort: params.sort,
        pipeline_tags: params.pipelineTags.join(","),
        filter_compat: String(params.filterCompat)
      });
      return api<HfSearchEnvelope>(`/manager/hf/search?${qs.toString()}`);
    }
  });
}

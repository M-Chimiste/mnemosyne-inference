export type GpuPlan = "all" | number[];

export interface ManagerStatus {
  loaded_model: string | null;
  loading: boolean;
  vllm_pid: number | null;
  loaded_at: number | null;
  loaded_at_human: string | null;
  tp_size: number | null;
  gpu_mem_util: number | null;
  inner_endpoint: string;
  alias: string | null;
  gpus: GpuPlan | null;
  quantization: string | null;
  max_model_len: number | null;
  storage_location: string | null;
  last_used_at: number | null;
  idle_seconds: number | null;
  seconds_until_eviction: number | null;
  inflight_requests: number;
  swap_target: string | null;
  vllm_arch_count: number;
  vllm_arch_source: string;
}

export interface GpuStatus {
  available: boolean;
  gpus: Array<{
    index: number;
    name: string;
    memory_used_mb: number;
    memory_total_mb: number;
    utilization_pct: number;
  }>;
}

export interface StorageLocation {
  name: string;
  path: string;
  free_bytes: number | null;
  total_bytes: number | null;
  writable: boolean;
  is_default: boolean;
}

export interface CatalogRow {
  alias: string;
  hf_model_id: string;
  source: string;
  quantization: string | null;
  gpus: GpuPlan;
  max_model_len: number | null;
  storage_location: string;
  cache_path: string | null;
  size_bytes: number | null;
  status: string;
  installed_at: number | null;
  last_used_at: number | null;
  request_count: number;
  extra_args: string[];
  revision: string;
  resolved_sha: string | null;
}

export interface HfSearchResult {
  model_id: string;
  architectures: string[];
  is_compatible: boolean;
  compat_reason: string | null;
  size_estimate_gb: number | null;
  downloads: number | null;
  likes: number | null;
  last_modified: string | null;
  tags: string[];
  pipeline_tag: string | null;
}

export interface HfSearchEnvelope {
  query: string;
  limit: number;
  page: number;
  page_size: number;
  has_next: boolean;
  next_page: number | null;
  include_vision: boolean;
  vllm_arch_source: string;
  vllm_arch_count: number;
  results: HfSearchResult[];
}

export interface DownloadInfo {
  status: string;
  started_at: number | null;
  finished_at: number | null;
  bytes_downloaded: number;
  total_bytes: number | null;
  error: string | null;
  pid: number | null;
  elapsed_seconds?: number;
}

export interface InstallStatus extends CatalogRow {
  download?: DownloadInfo;
  active: boolean;
}

export interface DownloadEntry {
  model: string;
  alias: string;
  status: string;
  started_at: number | null;
  finished_at: number | null;
  path: string | null;
  error: string | null;
  revision: string;
  bytes_downloaded: number;
  total_bytes: number | null;
  elapsed_seconds?: number;
}

export interface InstallRequest {
  alias: string;
  model: string;
  revision?: string;
  quantization?: string | null;
  gpus: GpuPlan;
  max_model_len?: number | null;
  storage?: string | null;
  extra_args: string[];
  size_estimate_gb?: number | null;
  ignore_patterns?: string[] | null;
}

export interface CatalogUpdateRequest {
  quantization?: string | null;
  gpus: GpuPlan;
  max_model_len?: number | null;
  extra_args: string[];
}

import { FormEvent, useEffect, useMemo, useState } from "react";
import { Download } from "lucide-react";
import type { Backend, HfSearchResult, InstallRequest, StorageLocation } from "../api/types";
import { useHfFiles } from "../api/queries";
import { aliasFromModelId, formatBytes, formatGb } from "../lib/format";

function parseGpuPlan(value: string): "all" | number[] {
  if (value === "all") return "all";
  return value.split(",").map((v) => Number(v.trim())).filter((v) => Number.isInteger(v) && v >= 0);
}

function defaultBackendFromResult(result: HfSearchResult): Backend {
  if (result.recommended_backend === "llama.cpp") return "llama.cpp";
  // "none" still defaults to vLLM in the form; install validation will reject.
  return "vllm";
}

export function InstallForm({
  result,
  storages,
  onSubmit,
  onCancel
}: {
  result: HfSearchResult;
  storages: StorageLocation[];
  onSubmit: (body: InstallRequest) => void;
  onCancel: () => void;
}) {
  const defaultStorage = storages.find((s) => s.is_default)?.name ?? storages[0]?.name ?? "";
  const [alias, setAlias] = useState(aliasFromModelId(result.model_id));
  const [backend, setBackend] = useState<Backend>(defaultBackendFromResult(result));
  const [quantization, setQuantization] = useState("");
  const [gpuMode, setGpuMode] = useState("all");
  const [customGpus, setCustomGpus] = useState("");
  const [storage, setStorage] = useState(defaultStorage);
  const [maxModelLen, setMaxModelLen] = useState("");
  const [extraArgs, setExtraArgs] = useState("");
  const [ggufPrimary, setGgufPrimary] = useState<string>("");
  const gpuValue = gpuMode === "custom" ? customGpus : gpuMode;
  const chosenStorage = useMemo(() => storages.find((s) => s.name === storage), [storage, storages]);

  // Probe the repo only when the user is actually looking at the GGUF path.
  // The result is cached per (modelId, revision) so flipping the toggle is
  // free after the first fetch.
  const showGguf = backend === "llama.cpp" || Boolean(result.has_gguf);
  const filesQuery = useHfFiles({ modelId: showGguf ? result.model_id : null });
  const candidates = filesQuery.data?.gguf_candidates ?? [];

  // When the backend flips to llama.cpp pre-pick the first candidate so
  // install isn't disabled until the user clicks the dropdown.
  useEffect(() => {
    if (backend !== "llama.cpp") return;
    if (!ggufPrimary && candidates.length > 0) {
      setGgufPrimary(candidates[0].primary_filename);
    }
  }, [backend, candidates, ggufPrimary]);

  const selectedCandidate = candidates.find((c) => c.primary_filename === ggufPrimary) ?? null;
  const ggufRequiredMissing = backend === "llama.cpp" && !ggufPrimary;
  const ggufSizeGb = selectedCandidate?.size_bytes ? selectedCandidate.size_bytes / 1e9 : null;
  const sizeEstimateGb = backend === "llama.cpp" ? ggufSizeGb : result.size_estimate_gb;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ggufRequiredMissing) return;
    onSubmit({
      alias,
      model: result.model_id,
      revision: "main",
      quantization: backend === "llama.cpp" ? null : quantization.trim() || null,
      gpus: parseGpuPlan(gpuValue),
      max_model_len: maxModelLen ? Number(maxModelLen) : null,
      storage: storage || null,
      extra_args: extraArgs.split("\n").map((arg) => arg.trim()).filter(Boolean),
      size_estimate_gb: sizeEstimateGb ?? undefined,
      backend,
      gguf_filename: backend === "llama.cpp" ? ggufPrimary : null
    });
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-sm font-medium">
          Alias
          <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={alias} onChange={(e) => setAlias(e.target.value)} required />
        </label>
        <label className="text-sm font-medium">
          Backend
          <select
            className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5"
            value={backend}
            onChange={(e) => setBackend(e.target.value as Backend)}
          >
            <option value="vllm">vLLM</option>
            <option value="llama.cpp">llama.cpp (GGUF)</option>
          </select>
        </label>
        {backend === "llama.cpp" && (
          <label className="text-sm font-medium md:col-span-2">
            GGUF File
            {filesQuery.isLoading && (
              <div className="mt-1 text-xs text-stone-600">Fetching repo files...</div>
            )}
            {filesQuery.error && (
              <div className="mt-1 text-xs text-brick">Failed to fetch GGUF candidates.</div>
            )}
            <select
              className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5"
              value={ggufPrimary}
              onChange={(e) => setGgufPrimary(e.target.value)}
              disabled={filesQuery.isLoading || candidates.length === 0}
              required
            >
              <option value="" disabled>
                {candidates.length === 0 ? "no GGUF candidates" : "Select a quant"}
              </option>
              {candidates.map((c) => {
                const sizeStr = c.size_bytes ? ` · ${formatGb(c.size_bytes / 1e9)}` : "";
                const shardStr = c.shard_count > 1 ? ` · ${c.shard_count} shards` : "";
                return (
                  <option key={c.primary_filename} value={c.primary_filename}>
                    {c.label}{sizeStr}{shardStr}
                  </option>
                );
              })}
            </select>
          </label>
        )}
        {backend === "vllm" && (
          <label className="text-sm font-medium">
            Quantization
            <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={quantization} onChange={(e) => setQuantization(e.target.value)} placeholder="awq" />
          </label>
        )}
        <label className="text-sm font-medium">
          GPU Plan
          <select className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={gpuMode} onChange={(e) => setGpuMode(e.target.value)}>
            <option value="all">all visible GPUs</option>
            <option value="0">GPU 0</option>
            <option value="1">GPU 1</option>
            <option value="custom">custom</option>
          </select>
        </label>
        {gpuMode === "custom" && (
          <label className="text-sm font-medium">
            Custom GPUs
            <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={customGpus} onChange={(e) => setCustomGpus(e.target.value)} placeholder="0,1" />
          </label>
        )}
        <label className="text-sm font-medium">
          Storage
          <select className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={storage} onChange={(e) => setStorage(e.target.value)}>
            {storages.map((s) => (
              <option key={s.name} value={s.name}>{s.name} · {formatBytes(s.free_bytes)} free</option>
            ))}
          </select>
        </label>
        <label className="text-sm font-medium">
          Max Context
          <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" type="number" min="1" value={maxModelLen} onChange={(e) => setMaxModelLen(e.target.value)} />
        </label>
      </div>
      <label className="block text-sm font-medium">
        Extra Args
        <textarea className="focus-ring mt-1 h-20 w-full border border-line bg-white px-2 py-1.5 font-mono text-xs" value={extraArgs} onChange={(e) => setExtraArgs(e.target.value)} placeholder={backend === "llama.cpp" ? "--ctx-size&#10;131072" : "--max-num-seqs&#10;8"} />
      </label>
      <div className="border border-line bg-white p-3 text-sm text-stone-700">
        Size estimate: <strong>{formatGb(sizeEstimateGb)}</strong>
        {sizeEstimateGb == null && <span className="ml-2 text-amber">free-space precheck will be skipped</span>}
        {chosenStorage && <span className="ml-2">Storage free: {formatBytes(chosenStorage.free_bytes)}</span>}
      </div>
      <div className="flex justify-end gap-2">
        <button type="button" className="focus-ring border border-line bg-white px-3 py-1.5 text-sm" onClick={onCancel}>Cancel</button>
        <button
          type="submit"
          className="focus-ring inline-flex items-center gap-2 border border-pine bg-pine px-3 py-1.5 text-sm text-white disabled:cursor-not-allowed disabled:border-line disabled:bg-stone-100 disabled:text-stone-500"
          disabled={ggufRequiredMissing}
          title={ggufRequiredMissing ? "Pick a GGUF file before installing" : "Install model"}
        >
          <Download className="h-4 w-4" aria-hidden /> Install
        </button>
      </div>
    </form>
  );
}

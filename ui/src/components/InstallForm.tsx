import { FormEvent, useMemo, useState } from "react";
import { Download } from "lucide-react";
import type { HfSearchResult, InstallRequest, StorageLocation } from "../api/types";
import { aliasFromModelId, formatBytes, formatGb } from "../lib/format";

function parseGpuPlan(value: string): "all" | number[] {
  if (value === "all") return "all";
  return value.split(",").map((v) => Number(v.trim())).filter((v) => Number.isInteger(v) && v >= 0);
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
  const [quantization, setQuantization] = useState("");
  const [gpuMode, setGpuMode] = useState("all");
  const [customGpus, setCustomGpus] = useState("");
  const [storage, setStorage] = useState(defaultStorage);
  const [maxModelLen, setMaxModelLen] = useState("");
  const [extraArgs, setExtraArgs] = useState("");
  const gpuValue = gpuMode === "custom" ? customGpus : gpuMode;
  const chosenStorage = useMemo(() => storages.find((s) => s.name === storage), [storage, storages]);

  function submit(e: FormEvent) {
    e.preventDefault();
    onSubmit({
      alias,
      model: result.model_id,
      revision: "main",
      quantization: quantization.trim() || null,
      gpus: parseGpuPlan(gpuValue),
      max_model_len: maxModelLen ? Number(maxModelLen) : null,
      storage: storage || null,
      extra_args: extraArgs.split("\n").map((arg) => arg.trim()).filter(Boolean),
      size_estimate_gb: result.size_estimate_gb
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
          Quantization
          <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={quantization} onChange={(e) => setQuantization(e.target.value)} placeholder="awq" />
        </label>
        <label className="text-sm font-medium">
          GPU Plan
          <select className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={gpuMode} onChange={(e) => setGpuMode(e.target.value)}>
            <option value="all">all GPUs</option>
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
        <textarea className="focus-ring mt-1 h-20 w-full border border-line bg-white px-2 py-1.5 font-mono text-xs" value={extraArgs} onChange={(e) => setExtraArgs(e.target.value)} placeholder="--max-num-seqs&#10;8" />
      </label>
      <div className="border border-line bg-white p-3 text-sm text-stone-700">
        Size estimate: <strong>{formatGb(result.size_estimate_gb)}</strong>
        {result.size_estimate_gb == null && <span className="ml-2 text-amber">free-space precheck will be skipped</span>}
        {chosenStorage && <span className="ml-2">Storage free: {formatBytes(chosenStorage.free_bytes)}</span>}
      </div>
      <div className="flex justify-end gap-2">
        <button type="button" className="focus-ring border border-line bg-white px-3 py-1.5 text-sm" onClick={onCancel}>Cancel</button>
        <button type="submit" className="focus-ring inline-flex items-center gap-2 border border-pine bg-pine px-3 py-1.5 text-sm text-white">
          <Download className="h-4 w-4" aria-hidden /> Install
        </button>
      </div>
    </form>
  );
}

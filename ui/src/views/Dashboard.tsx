import { Database, Gauge, LoaderCircle, Power, Server } from "lucide-react";
import { useCatalog, useGpu, useStatus } from "../api/queries";
import { useUnload } from "../api/mutations";
import { ErrorBox } from "../components/ErrorBox";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatGpuPlan, formatTime, relativeFromSeconds } from "../lib/format";

function pct(used: number, total: number): string {
  if (total <= 0) return "0%";
  return `${Math.round((used / total) * 100)}%`;
}

export default function Dashboard() {
  const status = useStatus();
  const gpu = useGpu();
  const catalog = useCatalog(false);
  const unload = useUnload();
  const row = catalog.data?.models.find((m) => m.alias === status.data?.alias);
  const loaded = Boolean(status.data?.alias);

  return (
    <div className="space-y-5">
      <section className="border border-line bg-white">
        <div className="flex flex-col gap-3 border-b border-line px-4 py-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-lg font-semibold">Dashboard</h1>
            <p className="text-sm text-stone-600">Resident model, swap state, GPU telemetry, and catalog counters.</p>
          </div>
          <button
            className="focus-ring inline-flex items-center gap-2 border border-brick bg-brick px-3 py-1.5 text-sm text-white disabled:cursor-not-allowed disabled:border-line disabled:bg-stone-100 disabled:text-stone-500"
            onClick={() => unload.mutate()}
            disabled={!loaded || unload.isPending}
            title="Unload resident model"
            aria-label="Unload resident model"
          >
            <Power className="h-4 w-4" aria-hidden />
            Unload
          </button>
        </div>
        {status.error && <ErrorBox error={status.error} />}
        <div className="grid gap-px bg-line md:grid-cols-4">
          <div className="bg-white p-4">
            <div className="flex items-center gap-2 text-xs uppercase text-stone-500"><Server className="h-4 w-4" /> Resident</div>
            <div className="mt-2 text-base font-semibold">{status.data?.alias ?? "none"}</div>
            <div className="mt-1 break-all text-sm text-stone-600">{status.data?.loaded_model ?? "No model loaded"}</div>
          </div>
          <div className="bg-white p-4">
            <div className="flex items-center gap-2 text-xs uppercase text-stone-500"><LoaderCircle className="h-4 w-4" /> Swap</div>
            <div className="mt-2"><StatusBadge status={status.data?.loading ? "loading" : loaded ? "installed" : "idle"} /></div>
            <div className="mt-1 text-sm text-stone-600">{status.data?.swap_target ? `target ${status.data.swap_target}` : "no queued swap"}</div>
          </div>
          <div className="bg-white p-4">
            <div className="flex items-center gap-2 text-xs uppercase text-stone-500"><Gauge className="h-4 w-4" /> GPU Plan</div>
            <div className="mt-2 text-base font-semibold">{formatGpuPlan(status.data?.gpus)}</div>
            <div className="mt-1 text-sm text-stone-600">
              cap {status.data?.gpu_mem_util != null ? `${Math.round(status.data.gpu_mem_util * 100)}%` : "—"}
            </div>
          </div>
          <div className="bg-white p-4">
            <div className="flex items-center gap-2 text-xs uppercase text-stone-500"><Database className="h-4 w-4" /> Catalog Request Count</div>
            <div className="mt-2 text-base font-semibold tabular-nums">{row?.request_count ?? 0}</div>
            <div className="mt-1 text-sm text-stone-600">persisted total for resident alias</div>
          </div>
        </div>
      </section>

      <div className="grid gap-5 lg:grid-cols-[1.3fr_1fr]">
        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Live GPU Metrics</h2>
          </div>
          {gpu.error && <ErrorBox error={gpu.error} />}
          {!gpu.data?.available ? (
            <div className="px-4 py-6 text-sm text-stone-600">No live GPU telemetry available from nvidia-smi.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="border-b border-line bg-stone-50 text-xs uppercase text-stone-500">
                  <tr>
                    <th className="px-4 py-2">GPU</th>
                    <th className="px-4 py-2">Name</th>
                    <th className="px-4 py-2">Memory</th>
                    <th className="px-4 py-2">Utilization</th>
                  </tr>
                </thead>
                <tbody>
                  {gpu.data.gpus.map((g) => (
                    <tr key={g.index} className="border-b border-line last:border-0">
                      <td className="px-4 py-2 tabular-nums">{g.index}</td>
                      <td className="px-4 py-2">{g.name}</td>
                      <td className="px-4 py-2 tabular-nums">
                        {formatBytes(g.memory_used_mb * 1_000_000)} / {formatBytes(g.memory_total_mb * 1_000_000)} ({pct(g.memory_used_mb, g.memory_total_mb)})
                      </td>
                      <td className="px-4 py-2 tabular-nums">{g.utilization_pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Runtime Detail</h2>
          </div>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 px-4 py-3 text-sm">
            <dt className="text-stone-500">Loaded at</dt>
            <dd>{formatTime(status.data?.loaded_at)}</dd>
            <dt className="text-stone-500">Last used</dt>
            <dd>{relativeFromSeconds(status.data?.last_used_at)}</dd>
            <dt className="text-stone-500">Idle eviction</dt>
            <dd>{status.data?.seconds_until_eviction != null ? `${status.data.seconds_until_eviction}s` : "—"}</dd>
            <dt className="text-stone-500">Inflight</dt>
            <dd>{status.data?.inflight_requests ?? 0}</dd>
            <dt className="text-stone-500">Tensor parallel</dt>
            <dd>{status.data?.tp_size ?? "—"}</dd>
            <dt className="text-stone-500">vLLM arch list</dt>
            <dd>{status.data ? `${status.data.vllm_arch_count} (${status.data.vllm_arch_source})` : "—"}</dd>
          </dl>
        </section>
      </div>
    </div>
  );
}

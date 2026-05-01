import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { CopyPlus, HardDrive, Pencil, Play, Save, Trash2 } from "lucide-react";
import type { CatalogRow, CatalogUpdateRequest, GpuPlan, HfSearchResult, InstallRequest } from "../api/types";
import { useCatalog, useStorage } from "../api/queries";
import { useDeleteCache, useDeleteCatalogRow, useInstallStart, useLoad, useUpdateCatalogRow } from "../api/mutations";
import { ConfirmDialog, type ConfirmDialogHandle } from "../components/ConfirmDialog";
import { ErrorBox } from "../components/ErrorBox";
import { InstallForm } from "../components/InstallForm";
import { SourceBadge } from "../components/SourceBadge";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatGpuPlan, formatTime, isCacheOnly } from "../lib/format";

type PendingAction =
  | { kind: "delete-cache"; row: CatalogRow }
  | { kind: "delete-row"; row: CatalogRow }
  | null;

function resultFromRow(row: CatalogRow): HfSearchResult {
  return {
    model_id: row.hf_model_id,
    architectures: [],
    is_compatible: true,
    compat_reason: null,
    size_estimate_gb: row.size_bytes ? row.size_bytes / 1_000_000_000 : null,
    downloads: null,
    likes: null,
    last_modified: null,
    tags: [],
    pipeline_tag: null
  };
}

function parseGpuPlan(value: string): GpuPlan {
  if (value === "all") return "all";
  return value.split(",").map((v) => Number(v.trim())).filter((v) => Number.isInteger(v) && v >= 0);
}

function gpuModeFromPlan(gpus: GpuPlan): string {
  if (gpus === "all") return "all";
  if (gpus.length === 1 && (gpus[0] === 0 || gpus[0] === 1)) return String(gpus[0]);
  return "custom";
}

function customGpuText(gpus: GpuPlan): string {
  return gpus === "all" ? "" : gpus.join(",");
}

function confirmText(action: PendingAction): { title: string; body: string; label: string } {
  if (!action) return { title: "", body: "", label: "" };
  if (action.kind === "delete-cache" && action.row.source === "config") {
    return {
      title: "Delete Cache By HF ID",
      body: `This uses ${action.row.hf_model_id}. Sibling aliases or storage locations for the same HuggingFace ID may be marked partial.`,
      label: "Delete cache"
    };
  }
  if (action.kind === "delete-cache") {
    return {
      title: "Delete Alias Cache",
      body: `This removes cached files for ${action.row.alias}. Sibling aliases for the same repository may be marked partial.`,
      label: "Delete cache"
    };
  }
  return {
    title: isCacheOnly(action.row) ? "Remove Cache Row" : "Remove Install",
    body: `This removes ${action.row.alias} and wipes the associated on-disk cache when the backend allows it.`,
    label: "Remove"
  };
}

function EditCatalogForm({
  row,
  onSubmit,
  onCancel
}: {
  row: CatalogRow;
  onSubmit: (body: CatalogUpdateRequest) => void;
  onCancel: () => void;
}) {
  const [quantization, setQuantization] = useState(row.quantization ?? "");
  const [gpuMode, setGpuMode] = useState(gpuModeFromPlan(row.gpus));
  const [customGpus, setCustomGpus] = useState(customGpuText(row.gpus));
  const [maxModelLen, setMaxModelLen] = useState(row.max_model_len == null ? "" : String(row.max_model_len));
  const [extraArgs, setExtraArgs] = useState(row.extra_args.join("\n"));
  const gpuValue = gpuMode === "custom" ? customGpus : gpuMode;

  function submit(e: FormEvent) {
    e.preventDefault();
    onSubmit({
      quantization: quantization.trim() || null,
      gpus: parseGpuPlan(gpuValue),
      max_model_len: maxModelLen ? Number(maxModelLen) : null,
      extra_args: extraArgs.split("\n").map((arg) => arg.trim()).filter(Boolean)
    });
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-sm font-medium">
          Alias
          <input className="mt-1 w-full border border-line bg-stone-50 px-2 py-1.5 text-stone-700" value={row.alias} readOnly />
        </label>
        <label className="text-sm font-medium">
          Quantization
          <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" value={quantization} onChange={(e) => setQuantization(e.target.value)} placeholder="awq" />
        </label>
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
          Max Context
          <input className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5" type="number" min="1" value={maxModelLen} onChange={(e) => setMaxModelLen(e.target.value)} />
        </label>
      </div>
      <label className="block text-sm font-medium">
        Extra Args
        <textarea className="focus-ring mt-1 h-20 w-full border border-line bg-white px-2 py-1.5 font-mono text-xs" value={extraArgs} onChange={(e) => setExtraArgs(e.target.value)} />
      </label>
      <div className="flex justify-end gap-2">
        <button type="button" className="focus-ring border border-line bg-white px-3 py-1.5 text-sm" onClick={onCancel}>Cancel</button>
        <button type="submit" className="focus-ring inline-flex items-center gap-2 border border-pine bg-pine px-3 py-1.5 text-sm text-white">
          <Save className="h-4 w-4" aria-hidden /> Save
        </button>
      </div>
    </form>
  );
}

export default function Catalog() {
  const catalog = useCatalog(true);
  const storage = useStorage();
  const load = useLoad();
  const deleteCache = useDeleteCache();
  const deleteRow = useDeleteCatalogRow();
  const install = useInstallStart();
  const updateRow = useUpdateCatalogRow();
  const dialog = useRef<ConfirmDialogHandle>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [createFrom, setCreateFrom] = useState<CatalogRow | null>(null);
  const [editRow, setEditRow] = useState<CatalogRow | null>(null);
  const confirm = confirmText(pending);

  function openConfirm(action: PendingAction) {
    setPending(action);
    requestAnimationFrame(() => dialog.current?.open());
  }

  function runConfirm() {
    if (!pending) return;
    if (pending.kind === "delete-cache") {
      if (pending.row.source === "config") {
        deleteCache.mutate({ hfModelId: pending.row.hf_model_id });
      } else {
        deleteCache.mutate({ alias: pending.row.alias });
      }
    } else {
      deleteRow.mutate(pending.row.alias);
    }
  }

  function submitInstall(body: InstallRequest) {
    install.mutate(body, { onSuccess: () => setCreateFrom(null) });
  }

  function submitUpdate(body: CatalogUpdateRequest) {
    if (!editRow) return;
    updateRow.mutate({ alias: editRow.alias, body }, { onSuccess: () => setEditRow(null) });
  }

  const rows = catalog.data?.models ?? [];

  return (
    <div className="space-y-5">
      <section className="border border-line bg-white">
        <div className="flex flex-col gap-2 border-b border-line px-4 py-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-lg font-semibold">Catalog</h1>
            <p className="text-sm text-stone-600">Configured aliases, UI installs, and discovered cache-only rows.</p>
          </div>
          <div className="text-sm text-stone-600">{rows.length} rows</div>
        </div>
        {catalog.error && <ErrorBox error={catalog.error} />}
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-line bg-stone-50 text-xs uppercase text-stone-500">
              <tr>
                <th className="px-3 py-2">Alias</th>
                <th className="px-3 py-2">Source</th>
                <th className="px-3 py-2">HF Model</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">GPU</th>
                <th className="px-3 py-2">Max Context</th>
                <th className="px-3 py-2">Size</th>
                <th className="px-3 py-2">Requests</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const cacheOnly = isCacheOnly(row);
                const removable = row.source === "ui_install";
                const editable = row.source === "ui_install" && !cacheOnly;
                const canLoad = !cacheOnly && (row.source === "config" || row.status === "installed");
                const canDeleteCache = !cacheOnly && (row.source === "ui_install" || row.source === "config");
                return (
                  <tr key={row.alias} className="border-b border-line last:border-0">
                    <td className="px-3 py-2 font-medium">{row.alias}</td>
                    <td className="px-3 py-2"><SourceBadge row={row} /></td>
                    <td className="max-w-sm break-all px-3 py-2 text-stone-700">{row.hf_model_id}</td>
                    <td className="px-3 py-2"><StatusBadge status={row.status} /></td>
                    <td className="px-3 py-2">{formatGpuPlan(row.gpus)}</td>
                    <td className="px-3 py-2 tabular-nums">{row.max_model_len ?? "derived"}</td>
                    <td className="px-3 py-2 tabular-nums">{formatBytes(row.size_bytes)}</td>
                    <td className="px-3 py-2 tabular-nums">{row.request_count}</td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-1">
                        {cacheOnly ? (
                          <button
                            className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                            onClick={() => setCreateFrom(row)}
                            title="Create alias"
                            aria-label={`Create alias for ${row.hf_model_id}`}
                          >
                            <CopyPlus className="h-4 w-4" aria-hidden /> Create alias
                          </button>
                        ) : (
                          <button
                            className="focus-ring inline-flex items-center gap-1 border border-pine bg-pine px-2 py-1 text-xs text-white disabled:cursor-not-allowed disabled:border-line disabled:bg-stone-100 disabled:text-stone-500"
                            onClick={() => load.mutate(row.alias)}
                            disabled={!canLoad || load.isPending}
                            title="Load alias"
                            aria-label={`Load ${row.alias}`}
                          >
                            <Play className="h-4 w-4" aria-hidden /> Load
                          </button>
                        )}
                        {canDeleteCache && (
                          <button
                            className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                            onClick={() => openConfirm({ kind: "delete-cache", row })}
                            title={row.source === "config" ? "Delete cache by HF ID" : "Delete alias cache"}
                            aria-label={`Delete cache for ${row.alias}`}
                          >
                            <HardDrive className="h-4 w-4" aria-hidden /> Cache
                          </button>
                        )}
                        {editable && (
                          <button
                            className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                            onClick={() => {
                              updateRow.reset();
                              setEditRow(row);
                            }}
                            title="Edit launch settings"
                            aria-label={`Edit ${row.alias}`}
                          >
                            <Pencil className="h-4 w-4" aria-hidden /> Edit
                          </button>
                        )}
                        {removable && (
                          <button
                            className="focus-ring inline-flex items-center gap-1 border border-brick bg-white px-2 py-1 text-xs text-brick hover:bg-brick/10"
                            onClick={() => openConfirm({ kind: "delete-row", row })}
                            title="Remove catalog row"
                            aria-label={`Remove ${row.alias}`}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden /> Remove
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={9}>No catalog rows.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {createFrom && (
        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Create Alias</h2>
            <p className="text-sm text-stone-600">Prefilled from {createFrom.hf_model_id}; installed at {formatTime(createFrom.installed_at)}.</p>
          </div>
          <div className="p-4">
            <InstallForm
              result={resultFromRow(createFrom)}
              storages={storage.data?.locations ?? []}
              onSubmit={submitInstall}
              onCancel={() => setCreateFrom(null)}
            />
            {install.error && <div className="mt-3"><ErrorBox error={install.error} /></div>}
          </div>
        </section>
      )}

      {editRow && (
        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Edit Install</h2>
            <p className="text-sm text-stone-600">{editRow.hf_model_id}</p>
          </div>
          <div className="p-4">
            <EditCatalogForm
              key={editRow.alias}
              row={editRow}
              onSubmit={submitUpdate}
              onCancel={() => setEditRow(null)}
            />
            {updateRow.error && <div className="mt-3"><ErrorBox error={updateRow.error} /></div>}
          </div>
        </section>
      )}

      <ConfirmDialog
        ref={dialog}
        title={confirm.title}
        body={confirm.body}
        confirmLabel={confirm.label}
        onConfirm={runConfirm}
      />
    </div>
  );
}

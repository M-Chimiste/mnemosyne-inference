import { useRef, useState } from "react";
import { RefreshCw, RotateCcw, Square, Trash2 } from "lucide-react";
import { useDownloads, useInstall } from "../api/queries";
import { useClearInstallDownload, useInstallCancel, useInstallRetry } from "../api/mutations";
import { ConfirmDialog, type ConfirmDialogHandle } from "../components/ConfirmDialog";
import { ErrorBox } from "../components/ErrorBox";
import { ProgressBar } from "../components/ProgressBar";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatTime } from "../lib/format";

function canCancel(status: string) {
  return status === "queued" || status === "pending" || status === "downloading";
}

// Errored / cancelled / partial: re-spawn the worker; HF will resume from
// the cache and the missing files come down. Single-click, no confirm.
function canRetry(status: string) {
  return status === "error" || status === "cancelled" || status === "partial";
}

// Completed installs can also be re-fetched (cache verification, suspected
// corruption, refreshed tag). Routed through the same retry endpoint but
// gated behind a confirm dialog because it's a no-op cost on a healthy row.
function canRestart(status: string) {
  return status === "complete";
}

function progressTotal(status: string | undefined, bytes: number | null | undefined, total: number | null | undefined) {
  if (total != null) return total;
  if (status === "complete" && bytes != null && bytes > 0) return bytes;
  return total;
}

function isActiveDownload(status: string | undefined) {
  return status === "queued" || status === "pending" || status === "downloading";
}

export default function Downloads() {
  const downloads = useDownloads();
  const cancel = useInstallCancel();
  const retry = useInstallRetry();
  const clear = useClearInstallDownload();
  const [selectedAlias, setSelectedAlias] = useState<string | null>(null);
  const [restartTarget, setRestartTarget] = useState<{ alias: string; model: string } | null>(null);
  const restartDialog = useRef<ConfirmDialogHandle>(null);
  const detail = useInstall(selectedAlias);
  const rows = downloads.data?.downloads ?? [];

  function openRestart(alias: string, model: string) {
    setRestartTarget({ alias, model });
    requestAnimationFrame(() => restartDialog.current?.open());
  }

  function runRestart() {
    if (!restartTarget) return;
    retry.mutate({ alias: restartTarget.alias });
    setRestartTarget(null);
  }

  return (
    <div className="space-y-5">
      <section className="border border-line bg-white">
        <div className="flex flex-col gap-2 border-b border-line px-4 py-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-lg font-semibold">Downloads</h1>
            <p className="text-sm text-stone-600">Queued and completed install workers with live detail polling.</p>
          </div>
          <div className="text-sm text-stone-600">{rows.length} records</div>
        </div>
        {downloads.error && <ErrorBox error={downloads.error} />}
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-line bg-stone-50 text-xs uppercase text-stone-500">
              <tr>
                <th className="px-3 py-2">Alias</th>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Progress</th>
                <th className="px-3 py-2">Started</th>
                <th className="px-3 py-2">Path</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.alias}
                  className={`border-b border-line last:border-0 ${selectedAlias === row.alias ? "bg-pine/5" : ""}`}
                >
                  <td className="px-3 py-2 font-medium">
                    <button className="focus-ring text-left underline decoration-stone-300 underline-offset-2" onClick={() => setSelectedAlias(row.alias)}>
                      {row.alias}
                    </button>
                  </td>
                  <td className="max-w-sm break-all px-3 py-2 text-stone-700">{row.model}</td>
                  <td className="px-3 py-2"><StatusBadge status={row.status} /></td>
                  <td className="px-3 py-2">
                    <ProgressBar
                      bytes={row.bytes_downloaded}
                      total={progressTotal(row.status, row.bytes_downloaded, row.total_bytes)}
                      active={isActiveDownload(row.status)}
                      complete={row.status === "complete"}
                    />
                  </td>
                  <td className="px-3 py-2">{formatTime(row.started_at)}</td>
                  <td className="max-w-xs truncate px-3 py-2 text-xs text-stone-600" title={row.path ?? ""}>{row.path ?? "—"}</td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1">
                      {canCancel(row.status) && (
                        <button
                          className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                          onClick={() => cancel.mutate(row.alias)}
                          title="Cancel download"
                          aria-label={`Cancel ${row.alias}`}
                        >
                          <Square className="h-4 w-4" aria-hidden /> Cancel
                        </button>
                      )}
                      {canRetry(row.status) && (
                        <button
                          className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                          onClick={() => retry.mutate({ alias: row.alias })}
                          title="Retry download"
                          aria-label={`Retry ${row.alias}`}
                        >
                          <RotateCcw className="h-4 w-4" aria-hidden /> Retry
                        </button>
                      )}
                      {canRestart(row.status) && (
                        <button
                          className="focus-ring inline-flex items-center gap-1 border border-line bg-white px-2 py-1 text-xs hover:bg-stone-100"
                          onClick={() => openRestart(row.alias, row.model)}
                          title="Re-download model (verifies / refreshes cache)"
                          aria-label={`Restart download for ${row.alias}`}
                        >
                          <RefreshCw className="h-4 w-4" aria-hidden /> Restart
                        </button>
                      )}
                      <button
                        className="focus-ring inline-flex items-center gap-1 border border-brick bg-white px-2 py-1 text-xs text-brick hover:bg-brick/10"
                        onClick={() => clear.mutate(row.alias)}
                        disabled={canCancel(row.status)}
                        title="Clear download record"
                        aria-label={`Clear record for ${row.model}`}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden /> Clear
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={7}>No download records.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedAlias && (
        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Install Detail</h2>
          </div>
          {detail.error && <ErrorBox error={detail.error} />}
          <div className="grid gap-4 px-4 py-3 md:grid-cols-[1fr_max-content]">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className="text-stone-500">Alias</dt>
              <dd>{detail.data?.alias ?? selectedAlias}</dd>
              <dt className="text-stone-500">Model</dt>
              <dd className="break-all">{detail.data?.hf_model_id ?? "—"}</dd>
              <dt className="text-stone-500">Status</dt>
              <dd><StatusBadge status={detail.data?.download?.status ?? detail.data?.status} /></dd>
              <dt className="text-stone-500">Downloaded</dt>
              <dd>{formatBytes(detail.data?.download?.bytes_downloaded)} / {formatBytes(detail.data?.download?.total_bytes)}</dd>
              <dt className="text-stone-500">Elapsed</dt>
              <dd>{detail.data?.download?.elapsed_seconds != null ? `${detail.data.download.elapsed_seconds}s` : "—"}</dd>
              <dt className="text-stone-500">Error</dt>
              <dd>{detail.data?.download?.error ?? "—"}</dd>
            </dl>
            <ProgressBar
              bytes={detail.data?.download?.bytes_downloaded}
              total={progressTotal(
                detail.data?.download?.status,
                detail.data?.download?.bytes_downloaded,
                detail.data?.download?.total_bytes
              )}
              active={isActiveDownload(detail.data?.download?.status)}
              complete={detail.data?.download?.status === "complete"}
            />
          </div>
        </section>
      )}

      <ConfirmDialog
        ref={restartDialog}
        title="Restart download"
        body={
          restartTarget
            ? `Re-download ${restartTarget.model}? HuggingFace will skip files already on disk and refetch any that are missing or have changed; expect this to be quick on a healthy install.`
            : ""
        }
        confirmLabel="Restart"
        onConfirm={runRestart}
      />
    </div>
  );
}

import { FormEvent, useState } from "react";
import { Download, Search as SearchIcon, ShieldAlert } from "lucide-react";
import type { HfSearchResult, InstallRequest } from "../api/types";
import { useHfSearch, useStorage } from "../api/queries";
import { useInstallStart } from "../api/mutations";
import { ErrorBox } from "../components/ErrorBox";
import { InstallForm } from "../components/InstallForm";
import { formatGb } from "../lib/format";

export default function Search() {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [includeVision, setIncludeVision] = useState(false);
  const [filterCompat, setFilterCompat] = useState(false);
  const [target, setTarget] = useState<HfSearchResult | null>(null);
  const search = useHfSearch({ q: query, includeVision, filterCompat, enabled: Boolean(query) });
  const storage = useStorage();
  const install = useInstallStart();

  function submit(e: FormEvent) {
    e.preventDefault();
    setQuery(text.trim());
  }

  function submitInstall(body: InstallRequest) {
    install.mutate(body, { onSuccess: () => setTarget(null) });
  }

  const results = search.data?.results ?? [];

  return (
    <div className="space-y-5">
      <section className="border border-line bg-white">
        <div className="border-b border-line px-4 py-3">
          <h1 className="text-lg font-semibold">HuggingFace Search</h1>
          <p className="text-sm text-stone-600">vLLM compatibility is checked from the pinned architecture snapshot.</p>
        </div>
        <form className="flex flex-col gap-3 p-4 md:flex-row md:items-end" onSubmit={submit}>
          <label className="flex-1 text-sm font-medium">
            Query
            <input
              className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="qwen instruct awq"
            />
          </label>
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" checked={includeVision} onChange={(e) => setIncludeVision(e.target.checked)} />
            Vision models
          </label>
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" checked={filterCompat} onChange={(e) => setFilterCompat(e.target.checked)} />
            Compatible only
          </label>
          <button className="focus-ring inline-flex items-center gap-2 border border-pine bg-pine px-3 py-1.5 text-sm text-white" type="submit">
            <SearchIcon className="h-4 w-4" aria-hidden /> Search
          </button>
        </form>
        {search.error && <ErrorBox error={search.error} />}
        {search.data && (
          <div className="border-t border-line px-4 py-2 text-xs text-stone-600">
            {search.data.results.length} results · {search.data.vllm_arch_count} architectures · {search.data.vllm_arch_source}
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="border-y border-line bg-stone-50 text-xs uppercase text-stone-500">
              <tr>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Compatibility</th>
                <th className="px-3 py-2">Architectures</th>
                <th className="px-3 py-2">Size</th>
                <th className="px-3 py-2">Signals</th>
                <th className="px-3 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <tr key={row.model_id} className="border-b border-line last:border-0">
                  <td className="max-w-sm break-all px-3 py-2 font-medium">{row.model_id}</td>
                  <td className="px-3 py-2">
                    {row.is_compatible ? (
                      <span className="inline-flex rounded border border-pine/40 bg-pine/10 px-2 py-0.5 text-xs font-medium text-pine">compatible</span>
                    ) : (
                      <div className="max-w-xs text-brick">
                        <span className="inline-flex items-center gap-1 rounded border border-brick/40 bg-brick/10 px-2 py-0.5 text-xs font-medium">
                          <ShieldAlert className="h-3.5 w-3.5" aria-hidden /> incompatible
                        </span>
                        <div className="mt-1 text-xs">{row.compat_reason}</div>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-stone-700">{row.architectures.join(", ") || "—"}</td>
                  <td className="px-3 py-2 tabular-nums">{formatGb(row.size_estimate_gb)}</td>
                  <td className="px-3 py-2 text-xs text-stone-700">
                    downloads {row.downloads ?? "—"} · likes {row.likes ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      className="focus-ring inline-flex items-center gap-1 border border-pine bg-pine px-2 py-1 text-xs text-white disabled:cursor-not-allowed disabled:border-line disabled:bg-stone-100 disabled:text-stone-500"
                      onClick={() => setTarget(row)}
                      disabled={!row.is_compatible}
                      title={row.is_compatible ? "Install model" : "Incompatible models cannot be installed from search"}
                      aria-label={`Install ${row.model_id}`}
                    >
                      <Download className="h-4 w-4" aria-hidden /> Install
                    </button>
                  </td>
                </tr>
              ))}
              {query && !search.isFetching && results.length === 0 && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={6}>No search results.</td></tr>
              )}
              {!query && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={6}>Enter a query to search HuggingFace.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {target && (
        <section className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">Install {target.model_id}</h2>
          </div>
          <div className="p-4">
            <InstallForm
              result={target}
              storages={storage.data?.locations ?? []}
              onSubmit={submitInstall}
              onCancel={() => setTarget(null)}
            />
            {install.error && <div className="mt-3"><ErrorBox error={install.error} /></div>}
          </div>
        </section>
      )}
    </div>
  );
}

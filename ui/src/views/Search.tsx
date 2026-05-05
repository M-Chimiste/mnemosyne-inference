import { FormEvent, UIEvent, useEffect, useRef, useState } from "react";
import { ChevronDown, Download, Search as SearchIcon, ShieldAlert } from "lucide-react";
import type { HfPipelineTag, HfSearchResult, HfSortOption, InstallRequest } from "../api/types";
import { useHfSearch, useStorage } from "../api/queries";
import { useInstallStart } from "../api/mutations";
import { ErrorBox } from "../components/ErrorBox";
import { InstallForm } from "../components/InstallForm";
import { formatGb } from "../lib/format";

const MODALITY_OPTIONS: { tag: HfPipelineTag; label: string }[] = [
  { tag: "text-generation", label: "Text" },
  { tag: "image-text-to-text", label: "Vision" },
  { tag: "audio-text-to-text", label: "Audio" },
  { tag: "any-to-any", label: "Omni" }
];

const SORT_OPTIONS: { value: HfSortOption; label: string }[] = [
  { value: "trending", label: "Trending" },
  { value: "downloads", label: "Downloads" },
  { value: "likes", label: "Likes" },
  { value: "recent", label: "Recently updated" }
];

const DEFAULT_TAGS: HfPipelineTag[] = MODALITY_OPTIONS.map((o) => o.tag);

export default function Search() {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [pipelineTags, setPipelineTags] = useState<HfPipelineTag[]>(DEFAULT_TAGS);
  const [sort, setSort] = useState<HfSortOption>("trending");
  const [filterCompat, setFilterCompat] = useState(false);
  const [pageSize, setPageSize] = useState(20);
  const [target, setTarget] = useState<HfSearchResult | null>(null);
  const installPanelRef = useRef<HTMLElement | null>(null);
  const installHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const search = useHfSearch({ q: query, pipelineTags, sort, filterCompat, pageSize, enabled: true });

  function toggleTag(tag: HfPipelineTag) {
    setPipelineTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
    );
  }
  const storage = useStorage();
  const install = useInstallStart();

  useEffect(() => {
    if (!target) return;
    installPanelRef.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
    installHeadingRef.current?.focus();
  }, [target]);

  function submit(e: FormEvent) {
    e.preventDefault();
    setQuery(text.trim());
  }

  function openInstall(row: HfSearchResult) {
    install.reset();
    setTarget(row);
  }

  function submitInstall(body: InstallRequest) {
    install.mutate(body, { onSuccess: () => setTarget(null) });
  }

  function maybeLoadMore(e: UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
    if (nearBottom && search.hasNextPage && !search.isFetchingNextPage) {
      search.fetchNextPage();
    }
  }

  const pages = search.data?.pages ?? [];
  const results = pages.flatMap((page) => page.results);
  const firstPage = pages[0];
  const lastPage = pages[pages.length - 1];
  const browseLabel = sort === "trending" ? "Trending HuggingFace models" : "Top HuggingFace models";
  const headerLabel = query.length === 0 ? browseLabel : `Search: ${query}`;
  const noTagsSelected = pipelineTags.length === 0;

  return (
    <div className="space-y-5">
      <section className="border border-line bg-white">
        <div className="border-b border-line px-4 py-3">
          <h1 className="text-lg font-semibold">HuggingFace Search</h1>
          <p className="text-sm text-stone-600">vLLM compatibility is checked from the pinned architecture snapshot.</p>
        </div>
        <form className="flex flex-col gap-3 p-4" onSubmit={submit}>
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
            <label className="flex-1 text-sm font-medium">
              Query
              <input
                className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5"
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="qwen instruct awq"
              />
            </label>
            <label className="text-sm font-medium">
              Sort by
              <select
                className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5 lg:w-44"
                value={sort}
                onChange={(e) => setSort(e.target.value as HfSortOption)}
              >
                {SORT_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>
            <label className="text-sm font-medium">
              Page size
              <select
                className="focus-ring mt-1 w-full border border-line bg-white px-2 py-1.5 lg:w-24"
                value={pageSize}
                onChange={(e) => setPageSize(Number(e.target.value))}
              >
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
            </label>
            <button className="focus-ring inline-flex items-center gap-2 border border-pine bg-pine px-3 py-1.5 text-sm text-white" type="submit">
              <SearchIcon className="h-4 w-4" aria-hidden /> Search
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
            <span className="text-xs uppercase tracking-wide text-stone-500">Modalities</span>
            {MODALITY_OPTIONS.map((opt) => (
              <label key={opt.tag} className="inline-flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={pipelineTags.includes(opt.tag)}
                  onChange={() => toggleTag(opt.tag)}
                />
                {opt.label}
              </label>
            ))}
            <span className="ml-auto inline-flex items-center gap-2">
              <label className="inline-flex items-center gap-2">
                <input type="checkbox" checked={filterCompat} onChange={(e) => setFilterCompat(e.target.checked)} />
                Compatible only
              </label>
            </span>
          </div>
          {noTagsSelected && (
            <p className="text-xs text-brick">Select at least one modality — the server falls back to all four otherwise.</p>
          )}
        </form>
        {search.error && <ErrorBox error={search.error} />}
        {firstPage && (
          <div className="flex flex-col gap-1 border-t border-line px-4 py-2 text-xs text-stone-600 md:flex-row md:items-center md:justify-between">
            <span>
              {headerLabel} · Showing {results.length} results · page {lastPage?.page ?? 1} · {firstPage.vllm_arch_count} architectures · {firstPage.vllm_arch_source}
            </span>
            {search.isFetching && !search.isFetchingNextPage && <span>Refreshing...</span>}
          </div>
        )}
        <div className="max-h-[65vh] overflow-auto border-t border-line" onScroll={maybeLoadMore}>
          <table className="min-w-full text-left text-sm">
            <thead className="sticky top-0 z-10 border-y border-line bg-stone-50 text-xs uppercase text-stone-500">
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
              {search.isFetching && results.length === 0 && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={6}>Loading models...</td></tr>
              )}
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
                      onClick={() => openInstall(row)}
                      disabled={!row.is_compatible}
                      title={row.is_compatible ? "Install model" : "Incompatible models cannot be installed from search"}
                      aria-label={`Install ${row.model_id}`}
                    >
                      <Download className="h-4 w-4" aria-hidden /> Install
                    </button>
                  </td>
                </tr>
              ))}
              {!search.isFetching && results.length === 0 && (
                <tr><td className="px-3 py-6 text-center text-stone-600" colSpan={6}>No search results.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {results.length > 0 && (
          <div className="flex items-center justify-center border-t border-line p-3">
            {search.hasNextPage ? (
              <button
                className="focus-ring inline-flex items-center gap-2 border border-line bg-white px-3 py-1.5 text-sm hover:bg-stone-100 disabled:cursor-wait disabled:opacity-70"
                onClick={() => search.fetchNextPage()}
                disabled={search.isFetchingNextPage}
              >
                <ChevronDown className="h-4 w-4" aria-hidden />
                {search.isFetchingNextPage ? "Loading..." : "Load more"}
              </button>
            ) : (
              <span className="text-xs text-stone-600">End of results</span>
            )}
          </div>
        )}
      </section>

      {target && (
        <section ref={installPanelRef} className="border border-line bg-white">
          <div className="border-b border-line px-4 py-3">
            <h2 ref={installHeadingRef} tabIndex={-1} className="text-base font-semibold outline-none">Install {target.model_id}</h2>
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

"""HuggingFace search + vLLM compatibility filter (Phase 5, PRD §5.9).

Supports `GET /manager/hf/search` on the admin plane. Two-stage pipeline:

1. Pre-filter via `HfApi.list_models(search=q, filter="transformers",
   pipeline_tag=...)` — server-side, no per-model fetch.
2. For each candidate, download `config.json` and cross-reference
   `architectures` against the architecture set vLLM supports.

The architecture set is sourced *primarily* by introspecting vLLM's model
registry at startup; if that import path breaks on a vLLM bump, we fall back
to a JSON snapshot bundled in the image. `scripts/refresh_arch_list.py`
regenerates that snapshot from a live vLLM install during the upgrade
workflow.

`huggingface_hub` is synchronous and `HfApi.list_models` does not expose a
per-call timeout. Two layers protect the admin plane:

  - Bounded daemon worker pool (`_search_pool`, max_workers=2) caps stuck
    thread buildup and cannot block process exit if an HF call is wedged.
  - Outer `asyncio.wait_for(timeout=30)` raises 504 on the response side.
    The worker thread keeps running — `huggingface_hub` does not honor
    `Future.cancel()`. The bounded pool and daemon threads prevent pile-up
    and clean-shutdown hangs.

Env-var hints set by the Dockerfile (`HF_HUB_ETAG_TIMEOUT`,
`HF_HUB_DOWNLOAD_TIMEOUT`) cover the `hf_hub_download` HTTP path. They do
not cover `list_models`; `model_info` gets an explicit per-call timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import (
    EntryNotFoundError,
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)

logger = logging.getLogger("hf-search")

# ── Module state ─────────────────────────────────────────────────────
_supported_archs: frozenset[str] = frozenset()
_arch_source: str = "empty"  # one of: vllm-registry | bundled-json | empty

_api = HfApi()

class _DaemonSearchPool:
    """Tiny bounded future pool backed by daemon threads.

    `ThreadPoolExecutor` uses non-daemon threads that can keep the process
    alive if a blocking HF request gets wedged. For search we prefer bounded
    concurrency plus abandonment on process exit; queued jobs are cancelable
    during lifespan shutdown.
    """

    _STOP = object()

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads: list[threading.Thread] = []
        for i in range(max_workers):
            t = threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}_{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        with self._lock:
            if self._shutdown:
                fut.cancel()
                return fut
            self._queue.put((fut, fn, args))
        return fut

    def shutdown(self, *, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is self._STOP:
                        continue
                    fut = item[0]  # type: ignore[index]
                    fut.cancel()
            for _ in self._threads:
                self._queue.put(self._STOP)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            fut, fn, args = item  # type: ignore[misc]
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)


_search_pool: Optional[_DaemonSearchPool] = None
_search_pool_lock = threading.Lock()


def _get_search_pool() -> _DaemonSearchPool:
    global _search_pool
    with _search_pool_lock:
        if _search_pool is None or _search_pool.is_shutdown:
            _search_pool = _DaemonSearchPool(
                max_workers=2,
                thread_name_prefix="hf-search",
            )
        return _search_pool


def shutdown_search_pool() -> None:
    """Cancel queued HF search jobs and stop accepting work.

    Running jobs may continue on daemon threads; they cannot block process
    exit. A later search lazily creates a fresh pool, which keeps tests and
    explicit in-process lifespan restarts usable.
    """
    global _search_pool
    with _search_pool_lock:
        if _search_pool is not None:
            _search_pool.shutdown(cancel_futures=True)
            _search_pool = None


# Per-process config.json cache: keyed on (repo_id, version). Simple FIFO
# eviction; the huggingface_hub library has its own ETag cache on disk.
# Entries without version metadata get a TTL so long-lived managers refresh.
_CONFIG_CACHE_CAP = 256
_CONFIG_CACHE_TTL_SECONDS = 600
_config_cache: "OrderedDict[tuple[str, Optional[str]], tuple[float, dict]]" = OrderedDict()
_config_cache_lock = threading.Lock()

_SEARCH_TIMEOUT_SECONDS = 30
_MODEL_INFO_TIMEOUT_SECONDS = 15
_WEIGHT_EXTENSIONS = (".safetensors", ".bin", ".gguf", ".pt")

# Modalities the search exposes. The Hub uses pipeline_tag values to bucket
# repos; these four cover the LLM-shaped surface vLLM can serve.
_DEFAULT_PIPELINE_TAGS: tuple[str, ...] = (
    "text-generation",
    "image-text-to-text",
    "audio-text-to-text",
    "any-to-any",
)
_VALID_PIPELINE_TAGS: frozenset[str] = frozenset(_DEFAULT_PIPELINE_TAGS)

# Maps the API-facing sort name to the `huggingface_hub` 1.x sort literal.
# The library converts these snake_case names to the Hub API's camelCase
# internally, and sorts descending by default — there's no public
# `direction` kwarg in 1.x.
_SORT_FIELDS: dict[str, str] = {
    "trending":  "trending_score",
    "downloads": "downloads",
    "likes":     "likes",
    "recent":    "last_modified",
}
_DEFAULT_SORT = "trending"


# ── Architecture set loading ─────────────────────────────────────────


def load_supported_architectures(
    json_fallback_path: Path,
) -> tuple[frozenset[str], str]:
    """Load the set of architectures vLLM supports.

    Returns (archs, source) where source ∈ {"vllm-registry", "bundled-json",
    "empty"}. Tried in order:

    1. Runtime introspection of vLLM's ModelRegistry. Survives the documented
       module path moving to the package re-export.
    2. Bundled JSON fallback (`vllm_supported_architectures.json`).
    3. Empty set — search still works, every result flagged as incompatible.
    """
    try:
        try:
            from vllm.model_executor.models.registry import ModelRegistry  # type: ignore
        except ImportError:
            from vllm.model_executor.models import ModelRegistry  # type: ignore
        archs = frozenset(ModelRegistry.get_supported_archs())
        if archs:
            logger.info(
                "Loaded %d vLLM architectures from runtime registry.",
                len(archs),
            )
            return archs, "vllm-registry"
        logger.warning(
            "vLLM ModelRegistry returned no architectures; falling back to "
            "bundled JSON.",
        )
    except (ImportError, AttributeError, Exception) as e:  # noqa: BLE001
        logger.warning(
            "vLLM runtime registry introspection failed (%s: %s); falling "
            "back to bundled JSON.",
            type(e).__name__, e,
        )

    try:
        if not json_fallback_path.exists():
            logger.error(
                "vllm_supported_architectures.json not found at %s; search "
                "will flag every result as incompatible.",
                json_fallback_path,
            )
            return frozenset(), "empty"
        with json_fallback_path.open("r") as f:
            data = json.load(f)
        archs = frozenset(data.get("architectures") or [])
        if not archs:
            logger.error(
                "vllm_supported_architectures.json has empty 'architectures' "
                "(vllm_version=%s). Run scripts/refresh_arch_list.py inside "
                "the container to regenerate.",
                data.get("vllm_version"),
            )
            return frozenset(), "empty"
        logger.warning(
            "Using bundled vllm_supported_architectures.json (vllm_version=%s, "
            "%d architectures). Run scripts/refresh_arch_list.py after a "
            "vLLM bump to keep this current.",
            data.get("vllm_version"), len(archs),
        )
        return archs, "bundled-json"
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
        logger.error(
            "Failed to read bundled architecture JSON at %s: %s: %s",
            json_fallback_path, type(e).__name__, e,
        )
        return frozenset(), "empty"


def set_supported_architectures(archs: frozenset[str], source: str) -> None:
    """Stash the loaded architecture set on module globals. Called once
    during manager_lifespan startup."""
    global _supported_archs, _arch_source
    _supported_archs = archs
    _arch_source = source


def get_arch_count() -> int:
    return len(_supported_archs)


def get_arch_source() -> str:
    return _arch_source


# ── Config.json fetch + parse ────────────────────────────────────────


def _cache_get(cache_key: tuple[str, Optional[str]]) -> Optional[dict]:
    with _config_cache_lock:
        entry = _config_cache.get(cache_key)
        if entry is None:
            return None
        stored_at, cfg = entry
        repo_id, version = cache_key
        if version is None and time.monotonic() - stored_at > _CONFIG_CACHE_TTL_SECONDS:
            _config_cache.pop(cache_key, None)
            logger.debug("Expired unversioned config cache entry for %s", repo_id)
            return None
        _config_cache.move_to_end(cache_key)
        return cfg


def _cache_put(cache_key: tuple[str, Optional[str]], cfg: dict) -> None:
    with _config_cache_lock:
        _config_cache[cache_key] = (time.monotonic(), cfg)
        _config_cache.move_to_end(cache_key)
        while len(_config_cache) > _CONFIG_CACHE_CAP:
            _config_cache.popitem(last=False)


def _clear_config_cache() -> None:
    """Test hook."""
    with _config_cache_lock:
        _config_cache.clear()


@dataclass
class _ConfigResult:
    architectures: list[str]
    fetch_status: str  # "ok" | "missing" | "gated" | "error"
    error_type: Optional[str] = None


def _fetch_config(
    repo_id: str,
    token: Optional[str],
    cache_dir: Optional[str],
    version: Optional[str],
    revision: Optional[str],
) -> _ConfigResult:
    """Per-row config fetch. Returns architectures + a fetch_status the
    caller maps to compat_reason. Failures here are row-level (200 + flagged),
    not endpoint-level."""
    cache_key = (repo_id, version)
    cached = _cache_get(cache_key)
    if cached is not None:
        archs = _normalize_architectures(cached.get("architectures"))
        return _ConfigResult(architectures=archs, fetch_status="ok")
    try:
        path = hf_hub_download(
            repo_id=repo_id,
            filename="config.json",
            revision=revision,
            cache_dir=cache_dir,
            token=token,
        )
    except GatedRepoError as e:
        # GatedRepoError subclasses HfHubHTTPError — keep this branch first.
        return _ConfigResult(
            architectures=[], fetch_status="gated",
            error_type=type(e).__name__,
        )
    except (EntryNotFoundError, RepositoryNotFoundError) as e:
        # EntryNotFoundError subclasses HfHubHTTPError too. Order matters.
        return _ConfigResult(
            architectures=[], fetch_status="missing",
            error_type=type(e).__name__,
        )
    except HfHubHTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return _ConfigResult(
                architectures=[], fetch_status="gated",
                error_type=type(e).__name__,
            )
        if status == 404:
            return _ConfigResult(
                architectures=[], fetch_status="missing",
                error_type=type(e).__name__,
            )
        return _ConfigResult(
            architectures=[], fetch_status="error",
            error_type=type(e).__name__,
        )
    except Exception as e:  # noqa: BLE001
        return _ConfigResult(
            architectures=[], fetch_status="error",
            error_type=type(e).__name__,
        )
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return _ConfigResult(
            architectures=[], fetch_status="error",
            error_type=type(e).__name__,
        )
    _cache_put(cache_key, cfg)
    archs = _normalize_architectures(cfg.get("architectures"))
    return _ConfigResult(architectures=archs, fetch_status="ok")


def _normalize_architectures(raw: Any) -> list[str]:
    """`config.json#architectures` is usually `list[str]` but a handful of
    repos ship it as a bare string. Treat None/empty list/None-elements as
    'missing'."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [a for a in raw if isinstance(a, str)]
    return []


# ── Compatibility decision ───────────────────────────────────────────


def _decide_compat(
    archs: list[str],
    fetch_status: str,
) -> tuple[bool, Optional[str]]:
    """Returns (is_compatible, compat_reason). Compatible iff fetch_status is
    'ok', architectures non-empty, every entry in `_supported_archs`, and
    the supported set is non-empty.
    """
    if fetch_status == "gated":
        return False, "gated or unauthorized"
    if fetch_status == "missing":
        return False, "missing config.architectures"
    if fetch_status == "error":
        return False, "config fetch failed"
    if _arch_source == "empty":
        return False, "vllm registry unavailable"
    if not archs:
        return False, "missing config.architectures"
    for a in archs:
        if a not in _supported_archs:
            return False, f"unsupported architecture: {a}"
    return True, None


# ── Size estimation ──────────────────────────────────────────────────


def _safetensor_total(repo_id: str, token: Optional[str]) -> Optional[int]:
    """Best-effort sum of weight-file sizes from `model_info(files_metadata=True)`.
    Mirrors download_worker._safetensor_total to avoid import coupling. Returns
    None on any failure — size-estimate failures must NOT change is_compatible
    or compat_reason."""
    try:
        info = _api.model_info(
            repo_id,
            files_metadata=True,
            timeout=_MODEL_INFO_TIMEOUT_SECONDS,
            token=token,
        )
    except Exception:  # noqa: BLE001
        return None
    siblings = getattr(info, "siblings", None) or []
    total = 0
    found = False
    for s in siblings:
        name = getattr(s, "rfilename", "") or ""
        size = getattr(s, "size", None)
        if size is None:
            continue
        if name.endswith(_WEIGHT_EXTENSIONS):
            total += int(size)
            found = True
    return total if found else None


def _bytes_to_gb(n: Optional[int]) -> Optional[float]:
    if n is None:
        return None
    return round(n / 1e9, 2)


# ── Search pipeline ──────────────────────────────────────────────────


class HFSearchError(Exception):
    """Endpoint-level error raised by run_search. Carries an HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _list_one(
    api: HfApi,
    q: str,
    pipeline_tag: str,
    limit: int,
    token: Optional[str],
    sort_field: str = "downloads",
) -> list:
    """Single `list_models` call for one pipeline_tag.

    We deliberately do NOT pass `filter="transformers"`: many newer/research
    releases (e.g. poolside/Laguna-XS.2, nvidia Nemotron-3-Omni) skip the
    `library:transformers` tag, and the Hub drops them before we get to
    do our own architecture check.

    `sort_field` uses the snake_case literals the library accepts in 1.x
    (`trending_score`, `downloads`, `likes`, `last_modified`); the
    library descends by default for these fields — `direction` is not a
    public kwarg anymore.
    """
    kwargs: dict[str, Any] = {
        "pipeline_tag": pipeline_tag,
        "limit": limit,
        "sort": sort_field,
        "token": token,
    }
    if q:
        kwargs["search"] = q
    return list(api.list_models(**kwargs))


def _lookup_exact_repo(
    api: HfApi,
    q: str,
    token: Optional[str],
) -> Optional[Any]:
    """Direct `model_info` for queries that look like a complete `org/repo`.

    Bypasses pipeline_tag pre-filters so a user who types the full ID
    always finds their model — even if the repo lacks the `pipeline_tag`
    or library tags that the broader `list_models` pre-filter requires.
    Returns None on any failure (404, gated, network) so the caller can
    fall back to the broader list_models result without surfacing errors
    when q is just normal search text that happens to contain a slash.
    """
    if "/" not in q:
        return None
    repo_id = q.strip().strip("/")
    parts = repo_id.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    try:
        return api.model_info(
            repo_id,
            timeout=_MODEL_INFO_TIMEOUT_SECONDS,
            token=token,
        )
    except Exception:  # noqa: BLE001
        return None


def _merge_by_id(*lists: list, sort_field: str = "downloads") -> list:
    """Merge any number of ModelInfo lists by `.id`, preserving the
    higher-ranked (earlier) appearance. Final order is by `sort_field`
    (downloads/likes/trendingScore/lastModified) desc, with stable
    fallbacks on missing values."""
    seen: dict[str, Any] = {}
    for src in lists:
        for m in src:
            mid = getattr(m, "id", None)
            if not mid or mid in seen:
                continue
            seen[mid] = m
    rows = list(seen.values())

    if sort_field == "last_modified":
        def key(m: Any) -> str:
            v = getattr(m, "last_modified", None)
            if v is None:
                return ""
            return v if isinstance(v, str) else str(v)
        rows.sort(key=key, reverse=True)
    elif sort_field in ("downloads", "likes"):
        # ModelInfo exposes downloads / likes directly. trending_score is
        # not surfaced as an attribute, so for trending we fall back to
        # insertion order — list_models already returns them in Hub order.
        rows.sort(
            key=lambda m: getattr(m, sort_field, 0) or 0,
            reverse=True,
        )
    return rows


def _model_version(m: Any) -> Optional[str]:
    """Return stable metadata for config-cache invalidation.

    Prefer Hub commit sha. If unavailable, last_modified still lets us avoid
    stale cache entries when the list API exposes update timestamps. Repos
    without either fall back to a TTL-governed unversioned cache key.
    """
    sha = getattr(m, "sha", None)
    if isinstance(sha, str) and sha:
        return sha
    last_modified = getattr(m, "last_modified", None)
    if last_modified is None:
        return None
    if isinstance(last_modified, str):
        return last_modified or None
    try:
        return last_modified.isoformat()
    except Exception:  # noqa: BLE001
        return str(last_modified)


def _model_revision(m: Any) -> Optional[str]:
    """Revision to pass to hf_hub_download. Only commit sha is a valid value
    here; last_modified is cache metadata, not a Hub revision."""
    sha = getattr(m, "sha", None)
    return sha if isinstance(sha, str) and sha else None


def _row_for_model(
    m: Any,
    token: Optional[str],
    cache_dir: Optional[str],
) -> dict:
    """Build the per-row dict. Per-row fetch errors stay row-level."""
    repo_id = m.id
    cfg_result = _fetch_config(
        repo_id,
        token=token,
        cache_dir=cache_dir,
        version=_model_version(m),
        revision=_model_revision(m),
    )
    is_compatible, compat_reason = _decide_compat(
        cfg_result.architectures, cfg_result.fetch_status,
    )
    size_bytes = _safetensor_total(repo_id, token=token)
    last_modified = getattr(m, "last_modified", None)
    if last_modified is not None and not isinstance(last_modified, str):
        # ModelInfo emits datetime objects; serialize for JSON.
        try:
            last_modified = last_modified.isoformat()
        except Exception:  # noqa: BLE001
            last_modified = str(last_modified)
    return {
        "model_id": repo_id,
        "architectures": cfg_result.architectures,
        "is_compatible": is_compatible,
        "compat_reason": compat_reason,
        "size_estimate_gb": _bytes_to_gb(size_bytes),
        "downloads": getattr(m, "downloads", None),
        "likes": getattr(m, "likes", None),
        "last_modified": last_modified,
        "tags": list(getattr(m, "tags", []) or []),
        "pipeline_tag": getattr(m, "pipeline_tag", None),
    }


def _resolve_pipeline_tags(
    pipeline_tags: Optional[Iterable[str]],
    include_vision: Optional[bool],
) -> tuple[str, ...]:
    """Resolve modality selection.

    Precedence:
      1. Explicit `pipeline_tags` (CSV from the route, list internally).
      2. Legacy `include_vision`: True → text + vision; False → text only.
      3. Neither: full default (text + vision + audio + omni).
    """
    if pipeline_tags is not None:
        seen: list[str] = []
        for t in pipeline_tags:
            t = (t or "").strip()
            if t and t in _VALID_PIPELINE_TAGS and t not in seen:
                seen.append(t)
        return tuple(seen) if seen else _DEFAULT_PIPELINE_TAGS
    if include_vision is True:
        return ("text-generation", "image-text-to-text")
    if include_vision is False:
        return ("text-generation",)
    return _DEFAULT_PIPELINE_TAGS


def _do_search_sync(
    q: str,
    limit: int,
    page: int,
    pipeline_tags: tuple[str, ...],
    sort: str,
    filter_compat: bool,
) -> dict:
    """Sync search body. Runs on the bounded executor."""
    token = os.environ.get("HUGGING_FACE_HUB_TOKEN") or None
    cache_dir = os.environ.get("HF_HOME") or None
    start = (page - 1) * limit
    end = start + limit
    fetch_limit = end + 1

    sort_field = _SORT_FIELDS.get(sort, _SORT_FIELDS[_DEFAULT_SORT])

    try:
        per_tag: list[list] = []
        for tag in pipeline_tags:
            per_tag.append(_list_one(
                _api, q, tag, fetch_limit, token,
                sort_field=sort_field,
            ))
        rows = _merge_by_id(*per_tag, sort_field=sort_field)

        # Pin an exact-ID match (org/repo) to the head — bypasses pre-filter
        # quirks for newer/research repos with missing pipeline_tag tags.
        exact = _lookup_exact_repo(_api, q, token) if q else None
        if exact is not None and getattr(exact, "id", None):
            rows = [exact] + [m for m in rows if getattr(m, "id", None) != exact.id]
    except HfHubHTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            raise HFSearchError(
                502,
                "hub unauthorized — set HUGGING_FACE_HUB_TOKEN",
            )
        raise HFSearchError(502, f"hub error: {type(e).__name__}: {e}")
    except (GatedRepoError, RepositoryNotFoundError) as e:
        raise HFSearchError(502, f"hub error: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        raise HFSearchError(502, f"hub list_models failed: {type(e).__name__}: {e}")

    out_rows: list[dict] = []
    dropped_incompat = 0

    if filter_compat:
        for m in rows:
            row = _row_for_model(m, token=token, cache_dir=cache_dir)
            if not row["is_compatible"]:
                dropped_incompat += 1
                continue
            out_rows.append(row)
            if len(out_rows) > end:
                break
        page_rows = out_rows[start:end]
        has_next = len(out_rows) > end
    else:
        page_models = rows[start:fetch_limit]
        for m in page_models:
            out_rows.append(_row_for_model(m, token=token, cache_dir=cache_dir))
        page_rows = out_rows[:limit]
        has_next = len(out_rows) > limit

    if filter_compat and dropped_incompat:
        logger.info(
            "filter_compat=true dropped %d incompatible row(s) for q=%r",
            dropped_incompat, q,
        )

    return {
        "query": q,
        "limit": limit,
        "page": page,
        "page_size": limit,
        "has_next": has_next,
        "next_page": page + 1 if has_next else None,
        "include_vision": "image-text-to-text" in pipeline_tags,
        "pipeline_tags": list(pipeline_tags),
        "sort": sort,
        "vllm_arch_source": _arch_source,
        "vllm_arch_count": len(_supported_archs),
        "results": page_rows,
    }


def _legacy_do_search_sync(
    q: str,
    limit: int,
    include_vision: bool,
    filter_compat: bool,
) -> dict:
    """Compatibility wrapper for older direct tests/imports."""
    tags = _resolve_pipeline_tags(None, include_vision)
    return _do_search_sync(q, limit, 1, tags, _DEFAULT_SORT, filter_compat)


async def run_search(
    q: str,
    limit: int,
    include_vision: Optional[bool] = None,
    filter_compat: bool = False,
    page: int = 1,
    pipeline_tags: Optional[Iterable[str]] = None,
    sort: str = _DEFAULT_SORT,
) -> dict:
    """Async wrapper around _do_search_sync. Caps end-to-end at 30s; on
    timeout, raises HFSearchError(504). The worker thread keeps running —
    the bounded pool prevents pile-up.

    `include_vision` is the legacy bool toggle; `pipeline_tags` (when
    provided) takes precedence. `sort` ∈ {trending, downloads, likes,
    recent}; unknown values fall back to trending.
    """
    q = (q or "").strip()
    limit = max(1, min(int(limit), 50))
    page = max(1, min(int(page), 20))
    sort = sort if sort in _SORT_FIELDS else _DEFAULT_SORT
    tags = _resolve_pipeline_tags(pipeline_tags, include_vision)
    pool = _get_search_pool()
    future = pool.submit(
        _do_search_sync,
        q, limit, page, tags, sort, bool(filter_compat),
    )
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(future),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HFSearchError(
            504,
            f"hub search timed out after {_SEARCH_TIMEOUT_SECONDS}s",
        )

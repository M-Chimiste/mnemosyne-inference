"""Mnemosyne Inference — manager-side download orchestration.

Owns the dict of live download subprocesses keyed by alias; spawns,
cancels, reaps. Writes catalog state through `catalog.mark_*` methods so
catalog state stays single-writer.

See project_docs/plans/phase_4.md §2.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from catalog import Catalog

logger = logging.getLogger("vllm-manager.downloader")


class ConflictError(Exception):
    """Raised when a concurrent install would collide with an active one.
    Carries the conflicting alias so the route handler can include it in
    the 409 body."""

    def __init__(self, conflict_alias: str):
        super().__init__(f"active install in progress for alias '{conflict_alias}'")
        self.conflict_alias = conflict_alias


class CacheWipeError(Exception):
    """Raised when a cache delete cannot be safely completed."""


@dataclass
class DownloadHandle:
    alias: str
    proc: subprocess.Popen
    started_at: float
    reader_thread: threading.Thread


# Module-level state. Replaces the v0 vllm_manager._downloads dict.
_active: dict[str, DownloadHandle] = {}
_active_lock = threading.Lock()


def repo_cache_dir(storage_path: str, hf_model_id: str) -> str:
    """Path to the HF repo cache dir under a storage location.

    Mirrors HF's convention (`<HF_HOME>/hub/models--<org>--<repo>`). Used
    by cache deletes — wiping just the snapshot path leaves refs/blobs/
    behind, so deletes target the entire repo dir.
    """
    safe = "models--" + hf_model_id.replace("/", "--")
    return os.path.join(storage_path, "hub", safe)


def force_wipe_cache(cache_path: str, *, allowed_roots: list[str]) -> bool:
    """Recursively remove `cache_path`. Refuses paths that don't sit under
    any of `allowed_roots` (the configured `storage.locations[].path`).
    Defensive against catalog corruption pointing at /etc.

    Returns True when the path was removed, False when it was already absent.
    Raises CacheWipeError when the wipe is refused or fails.
    """
    if not cache_path:
        raise CacheWipeError("cache path is empty")
    abs_path = os.path.realpath(cache_path)
    allowed = False
    for root in allowed_roots:
        root_abs = os.path.realpath(root)
        if abs_path == root_abs:
            # Refuse to wipe the storage root itself.
            raise CacheWipeError(f"refusing to wipe storage root '{abs_path}'")
        if abs_path.startswith(root_abs + os.sep):
            allowed = True
            break
    if not allowed:
        raise CacheWipeError(
            f"refusing to wipe '{abs_path}' outside configured storage roots"
        )
    if not os.path.exists(abs_path):
        return False
    try:
        if os.path.isdir(abs_path) and not os.path.islink(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
    except OSError as e:
        raise CacheWipeError(f"failed to wipe '{abs_path}': {e}") from e
    return True


def _build_worker_env(base_env: dict[str, str], hf_token: Optional[str]) -> dict[str, str]:
    """Return a copy of base_env with HF token set for the subprocess.
    The main process os.environ is never mutated."""
    env = dict(base_env)
    if hf_token:
        env["HUGGING_FACE_HUB_TOKEN"] = hf_token
    return env


def _spawn_worker(
    *,
    alias: str,
    model_id: str,
    revision: str,
    cache_dir: str,
    ignore_patterns: Optional[list[str]],
    env: dict[str, str],
) -> subprocess.Popen:
    """Run `python -m download_worker <args>`. Stdout is line-delimited
    JSON; stderr is inherited so worker tracebacks land in manager logs."""
    args = {
        "alias": alias,
        "model_id": model_id,
        "revision": revision,
        "cache_dir": cache_dir,
        "ignore_patterns": ignore_patterns,
    }
    encoded = base64.b64encode(json.dumps(args).encode("utf-8")).decode("ascii")
    cmd = [sys.executable, "-m", "download_worker", encoded]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=None,
        env=env,
        bufsize=1,
        text=True,
    )


def _reader_loop(handle: DownloadHandle, catalog: Catalog) -> None:
    """Read line-delimited JSON from worker stdout; classify each event;
    write catalog updates. On EOF, classify exit code → mark_*."""
    alias = handle.alias
    proc = handle.proc
    saw_complete = False
    worker_error: Optional[str] = None
    last_progress_write = 0.0
    try:
        if proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                logger.warning("download[%s]: garbled line %r", alias, line[:200])
                continue
            kind = event.get("event")
            try:
                if kind == "start":
                    catalog.mark_downloading(
                        alias,
                        pid=proc.pid,
                        total_bytes=event.get("total_bytes"),
                    )
                elif kind == "progress":
                    now = time.monotonic()
                    if now - last_progress_write >= 1.0:
                        last_progress_write = now
                        catalog.mark_progress(
                            alias, event.get("bytes_downloaded", 0)
                        )
                elif kind == "complete":
                    saw_complete = True
                    catalog.mark_complete(
                        alias,
                        cache_path=event.get("cache_path", ""),
                        size_bytes=event.get("size_bytes"),
                        resolved_sha=event.get("resolved_sha"),
                    )
                elif kind == "error":
                    # Don't write here — let the exit-code classifier below
                    # decide between cancelled (130) vs error (other), but
                    # preserve the worker's useful diagnostic.
                    message = event.get("message")
                    if message:
                        worker_error = str(message)
            except Exception as e:
                logger.warning("download[%s]: catalog write failed: %s", alias, e)
    except Exception as e:
        logger.warning("download[%s]: reader thread errored: %s", alias, e)
    finally:
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -1

        try:
            if saw_complete and rc == 0:
                # mark_complete already written.
                pass
            elif rc == 130:
                catalog.mark_cancelled(alias)
            elif saw_complete:
                # Worker emitted complete but exited non-zero — trust the event.
                logger.warning(
                    "download[%s]: complete event seen but exit=%d", alias, rc
                )
            else:
                catalog.mark_error(
                    alias,
                    worker_error or f"worker exited with code {rc}",
                )
        except Exception as e:
            logger.warning("download[%s]: terminal mark failed: %s", alias, e)

        with _active_lock:
            existing = _active.get(alias)
            if existing is handle:
                _active.pop(alias, None)


def start_install(
    *,
    alias: str,
    model_id: str,
    revision: str = "main",
    cache_dir: str,
    ignore_patterns: Optional[list[str]] = None,
    hf_token: Optional[str] = None,
    catalog: Catalog,
    storage_location: str,
) -> DownloadHandle:
    """Spawn the worker subprocess for a queued download.

    Builds a subprocess env from os.environ (copy) + optional HF token —
    main-process os.environ is never mutated. Caller has already written
    the queued models/downloads rows via catalog.start_install_tx; this
    function only handles the subprocess + reader thread.

    Raises ConflictError if there is already an active install for this
    alias OR for the same (storage_location, model_id) repo cache dir.
    """
    with _active_lock:
        if alias in _active:
            raise ConflictError(alias)
        # Repo-wide dedup at the manager level too — defensive against
        # callers that bypass the catalog check.
        active_other = catalog.find_active_for(storage_location, model_id)
        if active_other and active_other != alias:
            raise ConflictError(active_other)

        env = _build_worker_env(os.environ, hf_token)
        os.makedirs(cache_dir, exist_ok=True)
        proc = _spawn_worker(
            alias=alias,
            model_id=model_id,
            revision=revision,
            cache_dir=cache_dir,
            ignore_patterns=ignore_patterns,
            env=env,
        )
        handle = DownloadHandle(
            alias=alias,
            proc=proc,
            started_at=time.time(),
            reader_thread=None,  # set immediately below
        )
        t = threading.Thread(
            target=_reader_loop,
            args=(handle, catalog),
            name=f"download-reader-{alias}",
            daemon=True,
        )
        handle.reader_thread = t
        _active[alias] = handle
        t.start()
    return handle


def cancel_install(alias: str) -> bool:
    """SIGTERM the worker. Idempotent — returns False if no active worker.
    Reader thread will reap and call catalog.mark_cancelled when the
    process exits."""
    with _active_lock:
        handle = _active.get(alias)
    if handle is None:
        return False
    try:
        handle.proc.terminate()
    except ProcessLookupError:
        return False
    # Escalate to SIGKILL if the worker doesn't honor SIGTERM in 10s.
    def _escalate() -> None:
        try:
            handle.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                handle.proc.kill()
            except Exception:
                pass
    threading.Thread(target=_escalate, daemon=True).start()
    return True


def is_active(alias: str) -> bool:
    with _active_lock:
        return alias in _active


def reap_orphans_on_startup(catalog: Catalog) -> int:
    """Mark any downloads rows in queued/downloading state as interrupted.
    Called from lifespan startup BEFORE apply_config so reconcile may
    promote any whose snapshot is actually complete on disk."""
    return catalog.recover_orphan_downloads()

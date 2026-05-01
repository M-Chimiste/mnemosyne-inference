"""Mnemosyne Inference — download worker subprocess.

Standalone module run as `python -m download_worker <args-json-base64>`.
Imports only `huggingface_hub`, `tqdm`, and stdlib — does NOT import
`vllm`, `torch`, FastAPI, or any manager module so cold-start stays fast.

Args (base64-encoded JSON on argv[1]):
  {
    "alias": "...",
    "model_id": "org/repo",
    "revision": "main",
    "cache_dir": "/storage/.../hub",
    "ignore_patterns": ["*.pt", ...] | null
  }

HF token is read from HUGGING_FACE_HUB_TOKEN env on the worker side
(parent puts it there for that one subprocess only).

Stdout events (one JSON object per line):
  {"event":"start","total_bytes": N | null}
  {"event":"progress","bytes_downloaded": N, "total_bytes": M}
  {"event":"complete","cache_path":"...","size_bytes": N, "resolved_sha":"..."}
  {"event":"error","message":"..."}

Exit codes:
  0   — complete
  1   — hard error
  130 — SIGTERM (cancel)
"""
from __future__ import annotations

import base64
import json
import os
import signal
import sys
import threading
import time
from typing import Optional


def _emit(event: dict) -> None:
    """Emit one JSON line on stdout. Best-effort flush."""
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # Parent's reader pipe is gone; nothing useful to do but exit.
        os._exit(1)


def _install_parent_death_signal() -> None:
    """Linux: prctl(PR_SET_PDEATHSIG, SIGTERM) so the worker dies when the
    manager exits. Non-Linux: setsid + a daemon thread polling getppid() == 1.
    """
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
            return
        except Exception:
            pass
    # Non-Linux fallback (dev only). Detach into our own session so
    # SIGINT to the parent's process group doesn't take us with it.
    try:
        os.setsid()
    except OSError:
        pass

    def _poll_parent() -> None:
        while True:
            time.sleep(2)
            if os.getppid() == 1:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    t = threading.Thread(target=_poll_parent, daemon=True)
    t.start()


_progress_lock = threading.Lock()
_last_emit = 0.0
_last_bytes = 0


def _maybe_emit_progress(bytes_downloaded: int, total_bytes: Optional[int]) -> None:
    """Throttle progress emission to ≤1 / sec wall clock."""
    global _last_emit, _last_bytes
    with _progress_lock:
        now = time.monotonic()
        if now - _last_emit < 1.0 and bytes_downloaded < (_last_bytes + (1 << 24)):
            return
        _last_emit = now
        _last_bytes = bytes_downloaded
        _emit({
            "event": "progress",
            "bytes_downloaded": bytes_downloaded,
            "total_bytes": total_bytes,
        })


class _ProgressTqdm:
    """tqdm-like class accepted by huggingface_hub. We don't need bars or
    rate displays — just an aggregate byte counter that emits JSON.

    HF passes one instance per file. We aggregate via a module-level
    counter under a lock."""
    _total_lock = threading.Lock()
    _lock = _total_lock
    _total_downloaded = 0
    _total_size: Optional[int] = None

    def __init__(self, *args, **kwargs):
        # tqdm-like signature; ignore most kwargs.
        self.total = kwargs.get("total")
        self.n = 0
        self.disable = False
        self.unit = kwargs.get("unit", "it")
        if self.total is not None:
            with _ProgressTqdm._total_lock:
                if _ProgressTqdm._total_size is None:
                    _ProgressTqdm._total_size = 0
                _ProgressTqdm._total_size += int(self.total)

    def update(self, n: int = 1) -> None:
        self.n += n
        with _ProgressTqdm._total_lock:
            _ProgressTqdm._total_downloaded += n
            cur = _ProgressTqdm._total_downloaded
            tot = _ProgressTqdm._total_size
        _maybe_emit_progress(cur, tot)

    def set_description(self, *_a, **_k) -> None:
        pass

    def set_postfix(self, *_a, **_k) -> None:
        pass

    def close(self) -> None:
        pass

    def reset(self, total: Optional[int] = None) -> None:
        self.n = 0
        if total is not None:
            self.total = total

    def refresh(self) -> None:
        pass

    @classmethod
    def get_lock(cls):
        return cls._lock

    @classmethod
    def set_lock(cls, lock) -> None:
        cls._lock = lock
        cls._total_lock = lock

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        self.close()

    def __iter__(self):
        return iter([])

    @classmethod
    def reset_state(cls) -> None:
        with cls._total_lock:
            cls._total_downloaded = 0
            cls._total_size = None


def _safetensor_total(model_id: str, revision: str, token: Optional[str]) -> Optional[int]:
    """Best-effort total-bytes estimate from HF API. Sums sizes of common
    weight files. Returns None on any API failure."""
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        info = api.model_info(model_id, revision=revision, token=token)
        siblings = getattr(info, "siblings", []) or []
        total = 0
        for s in siblings:
            name = getattr(s, "rfilename", "") or ""
            size = getattr(s, "size", None)
            if size is None:
                continue
            if name.endswith((".safetensors", ".bin", ".gguf", ".pt")):
                total += int(size)
        return total or None
    except Exception:
        return None


def _classify_error(exc: BaseException) -> Optional[str]:
    """Tag the error category so the parent can rewrite the message.

    Returns "auth" for gated/private repos that need HUGGING_FACE_HUB_TOKEN,
    "not_found" for missing repos, or None if we can't classify.
    """
    type_name = type(exc).__name__
    if type_name in ("GatedRepoError",):
        return "auth"
    status = getattr(exc, "response", None)
    code = getattr(status, "status_code", None) if status is not None else None
    if code in (401, 403):
        return "auth"
    if type_name == "RepositoryNotFoundError" or code == 404:
        return "not_found"
    return None


def _resolved_sha_from_path(cache_path: str) -> Optional[str]:
    """The HF cache layout is .../snapshots/<sha>/. Pull the SHA from the
    final path component."""
    base = os.path.basename(os.path.normpath(cache_path))
    if len(base) == 40 and all(c in "0123456789abcdef" for c in base):
        return base
    return None


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _run(args: dict) -> int:
    alias = args["alias"]
    model_id = args["model_id"]
    revision = args.get("revision") or "main"
    cache_dir = args["cache_dir"]
    ignore_patterns = args.get("ignore_patterns")
    token = os.environ.get("HUGGING_FACE_HUB_TOKEN") or None

    _install_parent_death_signal()

    # SIGTERM → graceful exit 130 (so parent can distinguish cancel from error).
    def _on_sigterm(_signum, _frame):
        _emit({"event": "error", "message": "cancelled"})
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    signal.signal(signal.SIGTERM, _on_sigterm)

    total_estimate = _safetensor_total(model_id, revision, token)
    _emit({"event": "start", "total_bytes": total_estimate})

    _ProgressTqdm.reset_state()
    if total_estimate is not None:
        with _ProgressTqdm._total_lock:
            _ProgressTqdm._total_size = total_estimate

    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(
            repo_id=model_id,
            revision=revision,
            cache_dir=cache_dir,
            ignore_patterns=ignore_patterns,
            token=token,
            local_files_only=False,
            tqdm_class=_ProgressTqdm,
        )
    except Exception as e:
        _emit({
            "event": "error",
            "message": f"{type(e).__name__}: {e}",
            "category": _classify_error(e),
        })
        return 1

    size = _dir_size(path)
    sha = _resolved_sha_from_path(path)
    _emit({
        "event": "complete",
        "cache_path": path,
        "size_bytes": size,
        "resolved_sha": sha,
        "alias": alias,
    })
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _emit({"event": "error", "message": "missing args"})
        return 1
    try:
        decoded = base64.b64decode(argv[1].encode("utf-8")).decode("utf-8")
        args = json.loads(decoded)
    except Exception as e:
        _emit({"event": "error", "message": f"bad args: {e}"})
        return 1
    return _run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

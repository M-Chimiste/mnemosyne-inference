"""CLI for the private MFLUX worker process."""

from __future__ import annotations

import argparse
import os
import threading
import time

import uvicorn


def _watch_parent(parent_pid: int) -> None:
    while True:
        time.sleep(1)
        if os.getppid() != parent_pid:
            os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17324)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    uvicorn.run(
        "mnemosyne_mflux_worker.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


__all__ = ["main"]

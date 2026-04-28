"""Stub download worker for offline tests.

Reads a JSON 'script' file, then emits the scripted events to stdout in
order. Each event is a dict with an optional 'sleep' key that delays
emission by that many seconds.

Invocation:
    python fake_download_worker.py <script-path> [--exit-code N]
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _emit({"event": "error", "message": "no script"})
        return 1

    script_path = argv[1]
    exit_code = 0
    if "--exit-code" in argv:
        idx = argv.index("--exit-code")
        exit_code = int(argv[idx + 1])

    cancelled = {"flag": False}

    def _on_term(*_a):
        cancelled["flag"] = True
        _emit({"event": "error", "message": "cancelled"})
        sys.stdout.flush()
        os._exit(130)
    signal.signal(signal.SIGTERM, _on_term)

    with open(script_path) as f:
        events = json.load(f)
    for ev in events:
        sleep = ev.pop("sleep", 0)
        if sleep:
            time.sleep(sleep)
        _emit(ev)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

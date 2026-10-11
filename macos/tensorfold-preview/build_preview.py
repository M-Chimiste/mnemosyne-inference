#!/usr/bin/env python3
"""Build an isolated, pinned TensorFold research candidate, never activate it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
UPSTREAM = "https://github.com/ashhart/TensorFold.git"
REVISION = "cb2ebf0540f42604e2759b2ddef497861e928248"


def load_pin(directory: Path = HERE) -> dict:
    pin = json.loads((directory / "pin.json").read_text(encoding="utf-8"))
    if pin["schema_version"] != 1 or pin["upstream"] != UPSTREAM or pin["revision"] != REVISION:
        raise ValueError("unexpected TensorFold Preview source pin")
    for patch in pin["patches"]:
        relative = Path(patch["path"])
        if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", patch["sha256"]):
            raise ValueError("unsafe Preview patch descriptor")
        path = directory / relative
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("unsafe Preview patch path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != patch["sha256"]:
            raise ValueError("TensorFold Preview patch integrity mismatch")
    if not pin["fixture_tests"] or any(
        not re.fullmatch(r"tests/test_[a-z0-9_]+\.py", test) for test in pin["fixture_tests"]
    ):
        raise ValueError("unexpected Preview test selection")
    return pin


def run(argv: list[str], *, cwd: Path, timeout: int = 900) -> str:
    result = subprocess.run(argv, cwd=cwd, check=True, timeout=timeout, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    return result.stdout


def build(output: Path, *, python: str, local_source: Path | None = None) -> Path:
    pin = load_pin()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("this candidate requires Apple Silicon macOS and Metal")
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory; existing runtimes are never replaced")
    output.mkdir(mode=0o700, parents=True)
    source, environment = output / "source", output / "venv"
    origin = str(local_source.resolve(strict=True)) if local_source else UPSTREAM
    run(["git", "clone", "--no-hardlinks", "--no-checkout", origin, str(source)], cwd=output)
    run(["git", "checkout", "--detach", pin["revision"]], cwd=source)
    revision = run(["git", "rev-parse", "HEAD"], cwd=source).strip()
    if revision != pin["revision"]:
        raise ValueError("TensorFold source revision does not match the pin")
    for patch in pin["patches"]:
        path = str(HERE / patch["path"])
        run(["git", "apply", "--check", path], cwd=source)
        run(["git", "apply", path], cwd=source)
    run([python, "-m", "venv", str(environment)], cwd=output)
    executable = str(environment / "bin" / "python")
    # Dependencies come from the exact upstream pyproject, not the coordinator or a second manifest.
    run([executable, "-m", "pip", "install", f"{source}[vision,test]"], cwd=output)
    run([executable, "-m", "pytest", *pin["fixture_tests"], "-q"], cwd=source)
    versions = run([executable, "-c",
        "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()},sort_keys=True))"],
        cwd=output)
    packages = json.loads(versions)
    if packages.get("tensorfold") != pin["version"]:
        raise ValueError("installed TensorFold version does not match the pin")
    receipt = {
        "schema_version": 1,
        "channel": pin["channel"],
        "source_revision": revision,
        "patches": pin["patches"],
        "packages": packages,
        "fixture_tests": "passed",
        "created_at_unix": int(time.time()),
        "qualification": {
            "glm_full_model_vision": "not_run",
            "qwen_full_model_vision": "not_run",
            "m3_ultra_thunderbolt_cluster": "not_run",
        },
        "release_ready": False,
    }
    receipt_path = output / "preview-build.json"
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.chmod(receipt_path, 0o600)
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--local-source", type=Path, help="reuse local Git objects; uncommitted edits are not copied")
    args = parser.parse_args()
    try:
        receipt = build(args.output.expanduser().absolute(), python=args.python, local_source=args.local_source)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Preview build failed: {error}", file=sys.stderr)
        return 1
    print(f"Candidate built: {receipt}. Hardware qualification is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regenerate vllm_supported_architectures.json from a live vLLM install.

Run inside the running container after a vLLM bump:

    docker exec vllm-manager python scripts/refresh_arch_list.py

Default output path matches the path the manager loads from at startup
(repo root, next to vllm_manager.py). Pass an alternate path as argv[1].

Exits non-zero with a clear error if vLLM cannot be imported or its
ModelRegistry no longer exposes get_supported_archs() — that is a real
signal the API has shifted and the bundled fallback now diverges from
runtime introspection.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path


DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "vllm_supported_architectures.json"


def _import_registry():
    try:
        from vllm.model_executor.models.registry import ModelRegistry  # type: ignore
        return ModelRegistry
    except ImportError:
        from vllm.model_executor.models import ModelRegistry  # type: ignore
        return ModelRegistry


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate vllm_supported_architectures.json.",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=str(DEFAULT_OUTPUT),
        help=f"Output JSON path (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args(argv[1:])
    out_path = Path(args.output)

    try:
        import vllm  # type: ignore
    except ImportError as e:
        print(f"error: cannot import vllm: {e}", file=sys.stderr)
        return 1

    try:
        registry = _import_registry()
    except (ImportError, AttributeError) as e:
        print(
            f"error: cannot import ModelRegistry from vllm "
            f"(API may have shifted): {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    try:
        archs = sorted(set(registry.get_supported_archs()))
    except (AttributeError, Exception) as e:  # noqa: BLE001
        print(
            f"error: ModelRegistry.get_supported_archs() failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 3

    if not archs:
        print(
            "error: ModelRegistry.get_supported_archs() returned an empty set",
            file=sys.stderr,
        )
        return 4

    payload = {
        "vllm_version": getattr(vllm, "__version__", None),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "architectures": archs,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")

    print(
        f"wrote {len(archs)} architectures to {out_path} "
        f"(vllm_version={payload['vllm_version']})",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Killable filesystem operations for protected and removable model folders."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .local_models import scan_local_models
from .storage import inspect_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--name")
    inspect.add_argument("--expected-volume-uuid")

    scan = commands.add_parser("scan")
    scan.add_argument("--path", required=True)
    scan.add_argument("--max-files", type=int, default=100_000)
    scan.add_argument("--max-models", type=int, default=2_000)

    validate = commands.add_parser("validate-llama")
    validate.add_argument("--root", required=True)
    validate.add_argument("--model", required=True)
    validate.add_argument("--projector")
    validate.add_argument("--expected-volume-uuid")

    size = commands.add_parser("directory-size")
    size.add_argument("--root", required=True)
    size.add_argument("--path", required=True)

    ensure = commands.add_parser("ensure-directory")
    ensure.add_argument("--root", required=True)
    ensure.add_argument("--path", required=True)
    ensure.add_argument("--expected-volume-uuid")

    delete = commands.add_parser("delete-directory")
    delete.add_argument("--root", required=True)
    delete.add_argument("--path", required=True)
    delete.add_argument("--expected-volume-uuid")
    return parser


def _contained(root: Path, value: str, *, must_exist: bool) -> Path:
    candidate = Path(value).expanduser().resolve(strict=must_exist)
    if candidate == root or candidate.is_relative_to(root):
        return candidate
    raise ValueError(f"path escapes selected model storage: {candidate}")


def _gguf(root: Path, value: str, *, label: str) -> Path:
    path = _contained(root, value, must_exist=True)
    if not path.is_file():
        raise ValueError(f"{label} GGUF file is unavailable: {path}")
    if path.suffix.casefold() != ".gguf":
        raise ValueError(f"{label} must select a .gguf file: {path}")
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError(f"{label} does not have a GGUF header: {path}")
    return path


def _validated_root(path: str, expected_volume_uuid: str | None) -> tuple[Path, dict]:
    status = inspect_path(path, expected_volume_uuid=expected_volume_uuid)
    if not status.exists or not status.is_directory or not status.volume_matches:
        raise ValueError(status.diagnostic or "selected model storage is unavailable")
    # StorageStatus intentionally preserves the user's lexical path, including
    # a symlink. Resolve only inside this bounded helper for containment.
    return Path(status.path).resolve(strict=True), status.to_dict()


def _directory_size(root: Path, path: str) -> int:
    selected = _contained(root, path, must_exist=False)
    if not selected.exists():
        return 0
    total = 0
    for current, _directories, files in os.walk(selected, followlinks=False):
        current_path = Path(current)
        for filename in files:
            candidate = _contained(root, str(current_path / filename), must_exist=True)
            if candidate.is_file():
                total += candidate.stat().st_size
    return total


def _delete_directory(root: Path, root_value: str, path: str) -> bool:
    lexical_root = Path(root_value).expanduser().absolute()
    lexical_target = Path(path).expanduser().absolute()
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(
            f"path escapes selected model storage: {lexical_target}"
        ) from exc
    if not relative.parts:
        raise ValueError("refusing to delete the selected model storage root")

    current = lexical_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(
                f"refusing to delete a model path containing a symlink: {current}"
            )

    selected = _contained(root, str(lexical_target), must_exist=False)
    if selected == root:
        raise ValueError("refusing to delete the selected model storage root")
    if not selected.exists():
        return False
    if not selected.is_dir():
        raise ValueError("managed model deletion requires a directory")
    shutil.rmtree(lexical_target)
    return True


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "inspect":
        return {
            "status": inspect_path(
                args.path,
                name=args.name,
                expected_volume_uuid=args.expected_volume_uuid,
            ).to_dict()
        }
    if args.command == "scan":
        root, status = _validated_root(args.path, None)
        models = scan_local_models(
            root,
            max_files=args.max_files,
            max_models=args.max_models,
        )
        return {"status": status, "models": [model.to_dict() for model in models]}
    if args.command == "validate-llama":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        model = _gguf(root, args.model, label="model")
        projector = (
            _gguf(root, args.projector, label="multimodal projector")
            if args.projector
            else None
        )
        if projector == model:
            raise ValueError("the model and multimodal projector must be different GGUF files")
        return {
            "status": status,
            "model": str(model),
            "projector": str(projector) if projector is not None else None,
        }
    if args.command == "directory-size":
        root, _status = _validated_root(args.root, None)
        return {"bytes": _directory_size(root, args.path)}
    if args.command == "ensure-directory":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(status["diagnostic"] or "selected model storage is not writable")
        destination = _contained(root, args.path, must_exist=False)
        destination.mkdir(parents=True, exist_ok=True)
        destination = _contained(root, str(destination), must_exist=True)
        if not destination.is_dir():
            raise ValueError("model destination is not a directory")
        return {"status": status, "path": str(destination)}
    if args.command == "delete-directory":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(
                status["diagnostic"] or "selected model storage is not writable"
            )
        return {
            "status": status,
            "deleted": _delete_directory(root, args.root, args.path),
        }
    raise ValueError("unsupported filesystem operation")


def main() -> None:
    try:
        payload = {"ok": True, **_run(_parser().parse_args())}
        code = 0
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
        code = 2
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()

"""Process-isolated Hugging Face downloader for native model installs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--filename")
    parser.add_argument("--include-file", action="append", default=[])
    return parser


def _emit(**payload: object) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def main() -> None:
    args = _parser().parse_args()
    destination = Path(args.destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    token = (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        or None
    )
    if args.include_file:
        for filename in args.include_file:
            hf_hub_download(
                repo_id=args.repo_id,
                filename=filename,
                revision=args.revision,
                local_dir=destination,
                token=token,
            )
    elif args.filename:
        hf_hub_download(
            repo_id=args.repo_id,
            filename=args.filename,
            revision=args.revision,
            local_dir=destination,
            token=token,
        )
    else:
        snapshot_download(
            repo_id=args.repo_id,
            revision=args.revision,
            local_dir=destination,
            token=token,
        )
    _emit(status="complete")


if __name__ == "__main__":
    main()

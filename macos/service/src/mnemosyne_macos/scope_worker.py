"""Killable CoreFoundation bookmark receive and activation worker."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .security_scopes import SecurityScopeRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register")
    register.add_argument("--path", required=True)

    activate = commands.add_parser("activate")
    activate.add_argument("--scope-id", required=True)
    activate.add_argument("--path", required=True)
    return parser


def _stdin_object() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise ValueError("selected-folder permission payload must be an object")
    return value


def _run(args: argparse.Namespace) -> dict[str, Any]:
    registry = SecurityScopeRegistry(args.scope_root)
    try:
        if args.command == "register":
            payload = _stdin_object()
            bookmark_data = payload.get("bookmark_data")
            if not isinstance(bookmark_data, str):
                raise ValueError("selected-folder bookmark is missing")
            registered = registry.register(args.path, bookmark_data)
        else:
            registered = registry.activate(args.scope_id, args.path)
        return {"scope": {"id": registered.id, "path": registered.path}}
    finally:
        registry.close()


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

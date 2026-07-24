"""Consume a persisted folder grant and exec a manager-owned child process."""

from __future__ import annotations

import argparse
import os
import sys

from .security_scopes import SecurityScopeError, SecurityScopeRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scope-root", required=True)
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--scope-path", required=True)
    parser.add_argument("--remove-pythonpath", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = _parser().parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("scope_exec requires a command after --")

    registry = SecurityScopeRegistry(args.scope_root)
    try:
        registry.activate(args.scope_id, args.scope_path)
        # Do not close the scope before exec. macOS associates the consumed
        # extension with this process; exec preserves the process identity and
        # termination releases the extension.
        environment = os.environ.copy()
        if args.remove_pythonpath:
            removed = {
                os.path.normcase(os.path.normpath(os.path.abspath(value)))
                for value in args.remove_pythonpath
            }
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in environment.get("PYTHONPATH", "").split(os.pathsep)
                if value
                and os.path.normcase(os.path.normpath(os.path.abspath(value)))
                not in removed
            )
            if not environment["PYTHONPATH"]:
                environment.pop("PYTHONPATH", None)
        os.execvpe(command[0], command, environment)
    except (OSError, SecurityScopeError) as exc:
        registry.close()
        print(f"selected-folder grant could not start child: {exc}", file=sys.stderr)
        raise SystemExit(78) from exc


if __name__ == "__main__":
    main()

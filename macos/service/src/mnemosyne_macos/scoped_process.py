"""Build child commands that retain a selected-folder grant across ``exec``."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence


def wrap_scoped_argv(
    argv: Sequence[str],
    *,
    scope_root: str | Path | None,
    scope_id: str | None,
    scope_path: str | None,
    python_executable: str | None = None,
    remove_pythonpath: Sequence[str] = (),
) -> list[str]:
    """Run ``argv`` in a process that first consumes its own bookmark grant.

    Security-scoped access belongs to a process, not its parent.  The wrapper
    resolves and starts the receiver-owned bookmark, then replaces itself with
    the upstream executable.  The PID and process group therefore remain the
    manager-owned identity that the adapters validate.
    """

    command = [str(value) for value in argv]
    if not command:
        raise ValueError("scoped process command must not be empty")
    if scope_id is None:
        return command
    if scope_root is None or not scope_path:
        raise ValueError("scoped process requires its bookmark root and exact path")
    wrapper = [
        python_executable or sys.executable,
        "-m",
        "mnemosyne_macos.scope_exec",
        "--scope-root",
        str(Path(scope_root).expanduser()),
        "--scope-id",
        scope_id,
        "--scope-path",
        scope_path,
    ]
    for value in remove_pythonpath:
        if not value:
            raise ValueError("scoped process PYTHONPATH removal must not be empty")
        wrapper.extend(["--remove-pythonpath", value])
    return [*wrapper, "--", *command]


__all__ = ["wrap_scoped_argv"]

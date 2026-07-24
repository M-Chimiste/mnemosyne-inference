from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys

import pytest

import mnemosyne_macos.scope_process as scope_process_module
from mnemosyne_macos.scope_process import SecurityScopeProcess
from mnemosyne_macos.security_scopes import SecurityScopeError


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _blocking_helper(path: Path, pids_path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, os, subprocess, sys, time",
                "from pathlib import Path",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "Path(sys.argv[1]).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid}), encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )


async def _wait_for_pids(path: Path) -> dict[str, int]:
    deadline = asyncio.get_running_loop().time() + 3
    while not path.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert path.exists()
    return json.loads(path.read_text(encoding="utf-8"))


async def _wait_for_exit(pids: dict[str, int]) -> None:
    deadline = asyncio.get_running_loop().time() + 3
    while (
        any(_process_is_running(pid) for pid in pids.values())
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_scope_process_timeout_terminates_complete_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "blocking_scope_helper.py"
    pids_path = tmp_path / "scope-pids.json"
    _blocking_helper(helper, pids_path)
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def launch_blocking_helper(
        *_arguments: str,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        return await real_create_subprocess_exec(
            sys.executable,
            str(helper),
            str(pids_path),
            **kwargs,
        )

    monkeypatch.setattr(
        scope_process_module.asyncio,
        "create_subprocess_exec",
        launch_blocking_helper,
    )
    client = SecurityScopeProcess(
        tmp_path / "scopes",
        timeout_seconds=0.35,
    )
    pids: dict[str, int] = {}
    try:
        with pytest.raises(SecurityScopeError, match="operation timed out"):
            await client.activate("a" * 64, str(tmp_path / "Models"))
        pids = await _wait_for_pids(pids_path)
        await _wait_for_exit(pids)
        assert {
            label: pid
            for label, pid in pids.items()
            if _process_is_running(pid)
        } == {}
    finally:
        for pid in pids.values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_scope_process_cancellation_terminates_complete_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "cancelled_scope_helper.py"
    pids_path = tmp_path / "cancelled-scope-pids.json"
    _blocking_helper(helper, pids_path)
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def launch_blocking_helper(
        *_arguments: str,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        return await real_create_subprocess_exec(
            sys.executable,
            str(helper),
            str(pids_path),
            **kwargs,
        )

    monkeypatch.setattr(
        scope_process_module.asyncio,
        "create_subprocess_exec",
        launch_blocking_helper,
    )
    client = SecurityScopeProcess(
        tmp_path / "scopes",
        timeout_seconds=30,
    )
    task = asyncio.create_task(
        client.register(
            str(tmp_path / "Models"),
            "ZmluZGVyLWJvb2ttYXJr",
        )
    )
    pids = await _wait_for_pids(pids_path)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_exit(pids)
    try:
        assert {
            label: pid
            for label, pid in pids.items()
            if _process_is_running(pid)
        } == {}
    finally:
        for pid in pids.values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

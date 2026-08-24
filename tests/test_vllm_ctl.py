"""Host-side contract tests for the Docker lifecycle CLI."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parent.parent
CTL = REPO_ROOT / "vllm-ctl"


def _fake_docker(tmp_path: Path, *, fail_build: bool = False) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    failure = "exit 9" if fail_build else ":"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$*" == *" cp "* && -n "${DOCKER_CP_JSON:-}" ]]; then\n'
        '  printf "%s\\n" "$DOCKER_CP_JSON" > "${@: -1}"\n'
        "fi\n"
        'if [[ "$*" == *" build "* ]]; then\n'
        f"  {failure}\n"
        "fi\n"
    )
    script.chmod(0o755)
    return bin_dir, log


def _run_update(
    tmp_path: Path,
    *,
    fail_build: bool = False,
    skip_architecture_refresh: bool = True,
    ctl: Path = CTL,
    copied_architecture_json: str | None = None,
):
    bin_dir, log = _fake_docker(tmp_path, fail_build=fail_build)
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "DOCKER_LOG": str(log),
        "VLLM_COMPOSE_DIR": str(compose_dir),
        "VLLM_UPDATE_WAIT_TIMEOUT": "42",
    })
    if copied_architecture_json is not None:
        env["DOCKER_CP_JSON"] = copied_architecture_json
    argv = [str(ctl), "update"]
    if skip_architecture_refresh:
        argv.append("--skip-architecture-refresh")
    result = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    calls = log.read_text().splitlines()
    return result, calls, compose_dir


def test_update_builds_recreates_waits_and_verifies(tmp_path):
    result, calls, compose_dir = _run_update(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    prefix = f"compose -f {compose_dir}/docker-compose.yml"
    assert calls[0] == f"{prefix} build --pull vllm-manager"
    assert calls[1] == (
        f"{prefix} up -d --force-recreate --wait --wait-timeout 42 vllm-manager"
    )
    assert calls[2].startswith(f"{prefix} exec -T vllm-manager python -c ")
    assert calls[3] == (
        f"{prefix} exec -T vllm-manager /usr/local/bin/llama-server --version"
    )
    assert "Skipped architecture snapshot refresh" in result.stdout
    assert "Inference engines updated" in result.stdout


def test_update_does_not_replace_container_when_build_fails(tmp_path):
    result, calls, compose_dir = _run_update(tmp_path, fail_build=True)

    assert result.returncode != 0
    assert calls == [
        f"compose -f {compose_dir}/docker-compose.yml build --pull vllm-manager"
    ]
    assert "existing container was not replaced" in result.stdout


def test_update_rebuilds_app_layer_when_architectures_change(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ctl = checkout / "vllm-ctl"
    shutil.copy2(CTL, ctl)
    (checkout / "vllm_supported_architectures.json").write_text(
        '{"vllm_version":"old","architectures":["OldModel"]}\n'
    )
    new_snapshot = (
        '{"vllm_version":"new","generated_at":"now",'
        '"architectures":["NewModel"]}'
    )

    result, calls, compose_dir = _run_update(
        tmp_path,
        skip_architecture_refresh=False,
        ctl=ctl,
        copied_architecture_json=new_snapshot,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    prefix = f"compose -f {compose_dir}/docker-compose.yml"
    assert calls[6] == f"{prefix} build vllm-manager"
    assert calls[7] == (
        f"{prefix} up -d --force-recreate --wait --wait-timeout 42 vllm-manager"
    )
    assert "NewModel" in (
        checkout / "vllm_supported_architectures.json"
    ).read_text()
    assert "Inference engines and architecture metadata are updated" in result.stdout


def test_update_rejects_unknown_option():
    result = subprocess.run(
        [str(CTL), "update", "--surprise"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Unknown update option: --surprise" in result.stdout

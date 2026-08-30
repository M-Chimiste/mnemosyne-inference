#!/usr/bin/env python3
"""Collect secret-safe, machine-readable evidence for a native Mac candidate."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib import error, parse, request


MACOS_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = MACOS_ROOT / "VERSION"
DEFAULT_APP = Path("/Applications/Unified Inference.app")
AGENT_LABEL = "com.mnemosyne.inference.agent"
CONTROL_URL = "http://127.0.0.1:17321"
PUBLIC_URL = "http://127.0.0.1:1240"
PREFERENCES_DOMAIN = "com.mnemosyne.inference.menu"
ENGINE_DEFAULTS = {
    "llama_cpp": True,
    "omlx": False,
    "ds4": False,
    "mflux": False,
}
SECRET_KEYS = {
    "authorization",
    "bookmark_data",
    "cookie",
    "credential",
    "dsn",
    "hf_token",
    "password",
    "postgres_dsn",
    "private_key",
    "secret",
    "token",
}
SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}
URL_PATTERN = re.compile(r"\b(?:https?|postgres(?:ql)?)://[^\s\"'<>]+")


def _display_path(path: Path) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home or value.startswith(f"{home}/"):
        return f"~{value[len(home):]}"
    return value


def _redact_url(value: str) -> str:
    try:
        parsed = parse.urlsplit(value)
    except ValueError:
        return "[redacted URL]"
    if parsed.scheme not in {"http", "https", "postgres", "postgresql"}:
        return value

    try:
        hostname = parsed.hostname or ""
        parsed_port = parsed.port
    except ValueError:
        return f"{parsed.scheme}://[redacted]"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed_port}" if parsed_port is not None else ""
    if parsed.username is not None or parsed.password is not None:
        netloc = f"[redacted]@{hostname}{port}"
    else:
        netloc = f"{hostname}{port}"
    query = parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [
        (key, "[redacted]" if key.lower() in SECRET_QUERY_KEYS else item)
        for key, item in query
    ]
    return parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parse.urlencode(safe_query), "")
    )


def _redact_text(value: str) -> str:
    redacted = URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), value)
    redacted = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:basic|bearer)\s+\S+",
        "Authorization: [redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+",
        "[redacted authorization]",
        redacted,
    )
    redacted = re.sub(
        (
            r"(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|DSN|API_KEY|HF_TOKEN|"
            r"ACCESS_TOKEN))\s*=\s*[^\s,;]+"
        ),
        r"\1=[redacted]",
        redacted,
    )
    home = str(Path.home())
    return redacted.replace(home, "~")


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove credential material without hiding token metrics."""

    normalized = (key or "").lower()
    if normalized in SECRET_KEYS or normalized.endswith(("_password", "_secret", "_dsn")):
        return "[redacted]" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(item): redact(child, key=str(item)) for item, child in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _run(
    arguments: list[str],
    *,
    timeout: float = 20,
    output_limit: int = 4000,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "returncode": None,
            "diagnostic": _redact_text(str(exc)),
        }
    output = "\n".join(
        item.strip() for item in (completed.stdout, completed.stderr) if item.strip()
    )
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "diagnostic": _redact_text(output[:output_limit]) if output else None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _signing_details(app: Path) -> dict[str, Any]:
    result = _run(["codesign", "-d", "--verbose=4", str(app)])
    diagnostic = str(result.get("diagnostic") or "")

    def match(pattern: str) -> str | None:
        found = re.search(pattern, diagnostic, re.MULTILINE)
        return found.group(1).strip() if found else None

    authorities = re.findall(r"^Authority=(.+)$", diagnostic, re.MULTILINE)
    return {
        "inspection_ok": result["ok"],
        "signature": match(r"^Signature=(.+)$"),
        "team_identifier": match(r"^TeamIdentifier=(.+)$"),
        "authorities": authorities,
        "hardened_runtime": bool(
            re.search(
                r"^CodeDirectory .*\bruntime\b",
                diagnostic,
                re.MULTILINE,
            )
        ),
        "timestamped": bool(re.search(r"^Timestamp=", diagnostic, re.MULTILINE)),
    }


def _helper_identifier(helper: Path) -> str | None:
    result = _run(["codesign", "-d", "--verbose=4", str(helper)])
    diagnostic = str(result.get("diagnostic") or "")
    found = re.search(r"^Identifier=(.+)$", diagnostic, re.MULTILINE)
    return found.group(1).strip() if found else None


def _app_runtime_links(app: Path) -> dict[str, Any]:
    executable = app / "Contents" / "MacOS" / "UnifiedInference"
    sparkle = (
        app
        / "Contents"
        / "Frameworks"
        / "Sparkle.framework"
        / "Versions"
        / "B"
        / "Sparkle"
    )
    dependencies = _run(
        ["/usr/bin/otool", "-L", str(executable)],
        output_limit=256 * 1024,
    )
    load_commands = _run(
        ["/usr/bin/otool", "-l", str(executable)],
        output_limit=256 * 1024,
    )
    checks = {
        "sparkle_binary_present": sparkle.is_file(),
        "sparkle_dependency_present": bool(
            dependencies.get("ok")
            and "@rpath/Sparkle.framework/Versions/B/Sparkle"
            in str(dependencies.get("diagnostic") or "")
        ),
        "framework_rpath_present": bool(
            load_commands.get("ok")
            and "path @executable_path/../Frameworks "
            in str(load_commands.get("diagnostic") or "")
        ),
    }
    return {
        "checks": checks,
        "accepted": all(checks.values()),
    }


def _packaged_engine_defaults(path: Path) -> dict[str, bool | None]:
    if not path.is_file():
        return {engine: None for engine in ENGINE_DEFAULTS}
    found: dict[str, bool | None] = {engine: None for engine in ENGINE_DEFAULTS}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        engine = re.fullmatch(r"  ([a-z0-9_]+):", line)
        if engine:
            current = engine.group(1) if engine.group(1) in found else None
            continue
        enabled = re.fullmatch(r"    enabled: (true|false)", line)
        if current and enabled:
            found[current] = enabled.group(1) == "true"
            current = None
    return found


def collect_app(
    app: Path,
    expected_version: str,
    *,
    distribution: bool,
    allow_bare: bool,
) -> dict[str, Any]:
    info_path = app / "Contents" / "Info.plist"
    executable = app / "Contents" / "MacOS" / "UnifiedInference"
    helper = app / "Contents" / "MacOS" / "mnemosyne-service-bootstrap"
    result: dict[str, Any] = {
        "path": _display_path(app),
        "exists": app.is_dir(),
        "expected_version": expected_version,
    }
    if not app.is_dir() or not info_path.is_file():
        result["accepted"] = False
        result["diagnostic"] = "application bundle or Info.plist is missing"
        return result

    with info_path.open("rb") as stream:
        info = plistlib.load(stream)
    signature = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    architecture = _run(["lipo", "-archs", str(executable)])
    signing = _signing_details(app)
    bytecode = [
        _display_path(path.relative_to(app))
        for path in app.rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    ]
    defaults = _packaged_engine_defaults(
        app / "Contents" / "Resources" / "config.yaml.example"
    )
    runtime_embedded = (
        (app / "Contents" / "Resources" / "Python").is_dir()
        and (
            app
            / "Contents"
            / "Resources"
            / "Service"
            / "mnemosyne_macos"
        ).is_dir()
    )
    feed = info.get("SUFeedURL")
    sparkle_key_present = bool(info.get("SUPublicEDKey"))
    helper_identifier = _helper_identifier(helper)
    runtime_links = _app_runtime_links(app)
    developer_id = any(
        authority.startswith("Developer ID Application:")
        for authority in signing["authorities"]
    )
    distribution_checks: dict[str, Any] | None = None
    if distribution:
        staple = _run(["xcrun", "stapler", "validate", str(app)], timeout=60)
        gatekeeper = _run(
            ["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)],
            timeout=60,
        )
        distribution_checks = {
            "developer_id": developer_id,
            "hardened_runtime": signing["hardened_runtime"],
            "timestamped": signing["timestamped"],
            "sparkle_public_key_present": sparkle_key_present,
            "https_feed": isinstance(feed, str) and feed.startswith("https://"),
            "stapled": staple["ok"],
            "gatekeeper_accepted": gatekeeper["ok"],
            "accepted": all(
                (
                    developer_id,
                    signing["hardened_runtime"],
                    signing["timestamped"],
                    sparkle_key_present,
                    isinstance(feed, str) and feed.startswith("https://"),
                    staple["ok"],
                    gatekeeper["ok"],
                )
            ),
        }

    version = str(info.get("CFBundleShortVersionString", ""))
    result.update(
        {
            "version": version,
            "build": str(info.get("CFBundleVersion", "")),
            "architecture": architecture.get("diagnostic"),
            "arm64": architecture["ok"] and "arm64" in str(architecture.get("diagnostic")),
            "signature_valid": signature["ok"],
            "signing": signing,
            "helper_identifier": helper_identifier,
            "runtime_embedded": runtime_embedded,
            "runtime_links": runtime_links,
            "bare_bundle_allowed": allow_bare,
            "bytecode_entries": bytecode[:20],
            "bytecode_entry_count": len(bytecode),
            "sparkle": {
                "feed_url": redact(feed),
                "public_key_present": sparkle_key_present,
                "updates_enabled": sparkle_key_present,
            },
            "engine_defaults": defaults,
            "engine_defaults_match_v1": defaults == ENGINE_DEFAULTS,
            "distribution": distribution_checks,
        }
    )
    result["accepted"] = all(
        (
            version == expected_version,
            result["arm64"],
            signature["ok"],
            helper_identifier == "com.mnemosyne.inference.service",
            runtime_embedded or allow_bare,
            runtime_links["accepted"],
            not bytecode,
            defaults == ENGINE_DEFAULTS,
            distribution_checks is None or distribution_checks["accepted"],
        )
    )
    return result


def collect_dmg(dmg: Path, expected_version: str, *, distribution: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": _display_path(dmg),
        "exists": dmg.is_file(),
        "expected_filename": f"Unified-Inference-{expected_version}-macos-arm64.dmg",
    }
    if not dmg.is_file():
        result["accepted"] = False
        result["diagnostic"] = "disk image is missing"
        return result
    verify = _run(["hdiutil", "verify", "-quiet", str(dmg)], timeout=180)
    signature = _run(["codesign", "--verify", "--verbose=2", str(dmg)])
    distribution_checks: dict[str, Any] | None = None
    if distribution:
        staple = _run(["xcrun", "stapler", "validate", str(dmg)], timeout=60)
        gatekeeper = _run(
            [
                "spctl",
                "--assess",
                "--type",
                "open",
                "--context",
                "context:primary-signature",
                "--verbose=2",
                str(dmg),
            ],
            timeout=60,
        )
        distribution_checks = {
            "signature_valid": signature["ok"],
            "stapled": staple["ok"],
            "gatekeeper_accepted": gatekeeper["ok"],
            "accepted": signature["ok"] and staple["ok"] and gatekeeper["ok"],
        }
    result.update(
        {
            "filename": dmg.name,
            "bytes": dmg.stat().st_size,
            "sha256": _sha256(dmg),
            "image_valid": verify["ok"],
            "signature_valid": signature["ok"],
            "distribution": distribution_checks,
            "accepted": (
                dmg.name == result["expected_filename"]
                and verify["ok"]
                and (distribution_checks is None or distribution_checks["accepted"])
            ),
        }
    )
    return result


def _json_request(
    url: str,
    *,
    admin_password: str | None = None,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if admin_password:
        encoded = base64.b64encode(f"admin:{admin_password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    http_request = request.Request(
        url,
        data=data,
        headers=headers,
        method=method or ("POST" if payload is not None else "GET"),
    )
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(http_request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024)
            parsed = json.loads(body)
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "payload": redact(parsed),
            }
    except error.HTTPError as exc:
        body = exc.read(4096).decode(errors="replace")
        try:
            detail: Any = json.loads(body)
        except ValueError:
            detail = body
        return {
            "ok": False,
            "status": exc.code,
            "diagnostic": redact(detail),
        }
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "status": None,
            "diagnostic": _redact_text(str(exc)),
        }


def _exercise_fleet_participation(
    *,
    control_url: str,
    admin_password: str | None,
) -> dict[str, Any]:
    """Exercise the local pool toggle and restore its exact prior preference.

    The exercise refuses to begin while Fleet work is active.  Configuration
    is sampled before and after because participation is deliberately stored
    outside model/runtime/storage configuration.  The report retains only
    fixed state and equality checks, never the configuration or local paths.
    """

    endpoint = f"{control_url.rstrip('/')}/manager/fleet/participation"
    config_endpoint = f"{control_url.rstrip('/')}/manager/config"
    baseline = _json_request(endpoint, admin_password=admin_password)
    baseline_config = _json_request(
        config_endpoint,
        admin_password=admin_password,
    )

    def valid_status(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        enabled = value.get("enabled")
        state = value.get("state")
        active_requests = value.get("active_requests")
        updated_at = value.get("updated_at")
        if (
            type(enabled) is not bool
            or state not in {"joined", "draining", "paused"}
            or type(active_requests) is not int
            or active_requests < 0
            or type(updated_at) not in {int, float}
            or not math.isfinite(updated_at)
            or updated_at < 0
        ):
            return False
        expected_state = (
            "joined"
            if enabled
            else ("draining" if active_requests > 0 else "paused")
        )
        return state == expected_state

    def valid_idle_status(value: Any, *, enabled: bool) -> bool:
        return bool(
            valid_status(value)
            and value.get("enabled") is enabled
            and value.get("active_requests") == 0
        )

    baseline_payload = baseline.get("payload")
    baseline_valid = bool(
        baseline.get("ok") is True and valid_status(baseline_payload)
    )
    baseline_config_valid = bool(
        baseline_config.get("ok") is True
        and isinstance(baseline_config.get("payload"), dict)
    )
    baseline_idle = bool(
        baseline_valid and baseline_payload.get("active_requests") == 0
    )
    initial_enabled = (
        bool(baseline_payload["enabled"])
        if baseline_valid and isinstance(baseline_payload, dict)
        else None
    )

    paused: dict[str, Any] | None = None
    joined: dict[str, Any] | None = None
    restored: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    final_config: dict[str, Any] | None = None
    if baseline_idle and initial_enabled is not None and baseline_config_valid:
        try:
            paused = _json_request(
                endpoint,
                admin_password=admin_password,
                payload={"enabled": False},
                method="PUT",
            )
            paused_payload = paused.get("payload")
            if paused.get("ok") is True and valid_idle_status(
                paused_payload,
                enabled=False,
            ):
                joined = _json_request(
                    endpoint,
                    admin_password=admin_password,
                    payload={"enabled": True},
                    method="PUT",
                )
        finally:
            restored = _json_request(
                endpoint,
                admin_password=admin_password,
                payload={"enabled": initial_enabled},
                method="PUT",
            )
            final = _json_request(endpoint, admin_password=admin_password)
            final_config = _json_request(
                config_endpoint,
                admin_password=admin_password,
            )

    paused_payload = paused.get("payload") if isinstance(paused, dict) else None
    joined_payload = joined.get("payload") if isinstance(joined, dict) else None
    restored_payload = (
        restored.get("payload") if isinstance(restored, dict) else None
    )
    final_payload = final.get("payload") if isinstance(final, dict) else None
    expected_state = "joined" if initial_enabled else "paused"
    checks = {
        "baseline_reachable_and_valid": baseline_valid,
        "baseline_configuration_snapshot_valid": baseline_config_valid,
        "baseline_has_no_active_fleet_requests": baseline_idle,
        "pause_reached_closed_idle_state": bool(
            isinstance(paused, dict)
            and paused.get("ok") is True
            and valid_idle_status(paused_payload, enabled=False)
        ),
        "rejoin_reached_joined_state": bool(
            isinstance(joined, dict)
            and joined.get("ok") is True
            and valid_idle_status(joined_payload, enabled=True)
        ),
        "baseline_preference_restored": bool(
            isinstance(restored, dict)
            and restored.get("ok") is True
            and initial_enabled is not None
            and valid_idle_status(restored_payload, enabled=initial_enabled)
            and restored_payload.get("state") == expected_state
            and isinstance(final, dict)
            and final.get("ok") is True
            and valid_idle_status(final_payload, enabled=initial_enabled)
            and final_payload.get("state") == expected_state
        ),
        "model_runtime_storage_configuration_unchanged": bool(
            baseline_config_valid
            and isinstance(final_config, dict)
            and final_config.get("ok") is True
            and isinstance(final_config.get("payload"), dict)
            and baseline_config.get("payload") == final_config.get("payload")
        ),
    }
    return {
        "initial_state": (
            baseline_payload.get("state")
            if baseline_valid and isinstance(baseline_payload, dict)
            else None
        ),
        "restored_state": (
            final_payload.get("state")
            if valid_status(final_payload)
            else None
        ),
        "checks": checks,
        "accepted": all(checks.values()),
    }


def _launch_agent() -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{AGENT_LABEL}"
    result = _run(["launchctl", "print", target])
    diagnostic = str(result.get("diagnostic") or "")

    def integer(field: str) -> int | None:
        found = re.search(
            rf"^\s*{re.escape(field)} = (-?\d+)$",
            diagnostic,
            re.MULTILINE,
        )
        return int(found.group(1)) if found else None

    state = re.search(r"^\s*state = (.+)$", diagnostic, re.MULTILINE)
    return {
        "registered": result["ok"],
        "state": state.group(1).strip() if state else None,
        "asid": integer("asid"),
        "pid": integer("pid"),
        "runs": integer("runs"),
        "last_exit_code": integer("last exit code"),
    }


def _exercise_launch_agent(
    mode: str,
    *,
    control_url: str,
    public_url: str,
    admin_password: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Restart the exact registered job and prove both planes return."""

    before = _launch_agent()
    target = f"gui/{os.getuid()}/{AGENT_LABEL}"
    if mode == "restart":
        arguments = ["launchctl", "kickstart", "-k", target]
    elif mode == "keepalive":
        arguments = ["launchctl", "kill", "SIGTERM", target]
    else:
        raise ValueError(f"unknown LaunchAgent exercise '{mode}'")
    started = time.monotonic()
    command = _run(arguments, timeout=min(20, timeout))
    after = _launch_agent()
    health: dict[str, Any] = {"ok": False, "status": None}
    readiness: dict[str, Any] = {"ok": False, "status": None}
    if command["ok"]:
        deadline = time.monotonic() + timeout
        while True:
            after = _launch_agent()
            health = _json_request(
                f"{public_url.rstrip('/')}/health",
                timeout=min(2, max(0.1, deadline - time.monotonic())),
            )
            readiness = _json_request(
                f"{control_url.rstrip('/')}/manager/readiness",
                admin_password=admin_password,
                timeout=min(2, max(0.1, deadline - time.monotonic())),
            )
            if (
                after.get("state") == "running"
                and isinstance(after.get("pid"), int)
                and after.get("pid") != before.get("pid")
                and health.get("ok")
                and readiness.get("ok")
            ):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    accepted = bool(
        command["ok"]
        and before.get("registered")
        and before.get("state") == "running"
        and isinstance(before.get("pid"), int)
        and after.get("state") == "running"
        and isinstance(after.get("pid"), int)
        and after.get("pid") != before.get("pid")
        and health.get("ok")
        and readiness.get("ok")
    )
    return redact(
        {
            "mode": mode,
            "before": before,
            "command": command,
            "after": after,
            "health": {
                "ok": health.get("ok"),
                "status": health.get("status"),
                "diagnostic": health.get("diagnostic"),
            },
            "readiness": {
                "ok": readiness.get("ok"),
                "status": readiness.get("status"),
                "diagnostic": readiness.get("diagnostic"),
            },
            "elapsed_seconds": time.monotonic() - started,
            "accepted": accepted,
        }
    )


def _configuration_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        return None
    config = payload["config"]
    engines = config.get("engines")
    storage = config.get("storage")
    models = config.get("models")
    migration = config.get("migration")
    return {
        "schema_version": config.get("schema_version"),
        "revision": payload.get("revision"),
        "applied_revision_matches": (
            payload.get("revision") == payload.get("applied_revision")
        ),
        "restart_required": payload.get("restart_required"),
        "engines": {
            name: {"enabled": value.get("enabled")}
            for name, value in engines.items()
            if isinstance(name, str) and isinstance(value, dict)
        }
        if isinstance(engines, dict)
        else None,
        "storage": [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "volume_uuid_configured": bool(item.get("volume_uuid")),
                "scope_configured": bool(item.get("scope_id")),
            }
            for item in storage.get("locations", [])
            if isinstance(item, dict)
        ]
        if isinstance(storage, dict)
        else None,
        "models": [
            {
                "alias": item.get("alias"),
                "engine": item.get("engine"),
                "storage": item.get("storage"),
                "kind": item.get("kind"),
                "capabilities": item.get("capabilities"),
                "projector_configured": bool(
                    isinstance(item.get("load"), dict)
                    and item["load"].get("projector_path")
                ),
            }
            for item in models
            if isinstance(item, dict)
        ]
        if isinstance(models, list)
        else None,
        "legacy_lmstudio_aliases": [
            item.get("alias")
            for item in migration.get("legacy_lmstudio_profiles", [])
            if isinstance(item, dict)
        ]
        if isinstance(migration, dict)
        else None,
    }


def _install_evidence_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("installs"), list):
        return None
    installs: list[dict[str, Any]] = []
    for item in payload["installs"][:100]:
        if not isinstance(item, dict):
            continue
        events = item.get("events")
        installs.append(
            {
                "id": item.get("id"),
                "repo_id": item.get("repo_id"),
                "engine": item.get("engine"),
                "alias": item.get("alias"),
                "status": item.get("status"),
                "revision_pinned": bool(item.get("revision")),
                "filename": item.get("filename"),
                "projector_filename": item.get("projector_filename"),
                "download_file_count": (
                    len(item.get("download_files", []))
                    if isinstance(item.get("download_files"), list)
                    else None
                ),
                "bytes_downloaded": item.get("bytes_downloaded"),
                "total_bytes": item.get("total_bytes"),
                "dismissed": item.get("dismissed"),
                "events": [
                    {
                        "sequence": event.get("sequence"),
                        "event": event.get("event"),
                        "status": event.get("status"),
                        "bytes_downloaded": event.get("bytes_downloaded"),
                        "created_at": event.get("created_at"),
                    }
                    for event in events
                    if isinstance(event, dict)
                ]
                if isinstance(events, list)
                else [],
            }
        )
    return {"install_count": len(installs), "installs": installs}


def _download_lifecycle_summary(payload: Any) -> dict[str, Any]:
    summary = _install_evidence_summary(payload)
    installs = summary["installs"] if summary is not None else []
    sequences = [
        [
            (event.get("event"), event.get("status"))
            for event in item.get("events", [])
            if isinstance(event, dict)
        ]
        for item in installs
    ]

    def ordered(sequence: list[tuple[Any, Any]], statuses: tuple[str, ...]) -> bool:
        index = 0
        for event, status in sequence:
            if event == "status" and status == statuses[index]:
                index += 1
                if index == len(statuses):
                    return True
        return False

    checks = {
        "cancelled": any(
            any(event == "status" and status == "cancelled" for event, status in sequence)
            for sequence in sequences
        ),
        "cancel_retry_completed": any(
            ordered(sequence, ("cancelled", "queued", "installed"))
            for sequence in sequences
        ),
        "registration_retry_completed": any(
            ordered(sequence, ("downloaded", "registering", "installed"))
            for sequence in sequences
        ),
        "history_dismissed": any(
            any(event == "history_dismissed" for event, _status in sequence)
            for sequence in sequences
        ),
        "managed_files_deleted": any(
            any(event == "status" and status == "deleted" for event, status in sequence)
            for sequence in sequences
        ),
        "exact_revision_pinned": any(
            item.get("revision_pinned")
            and item.get("status") in {"installed", "deleted"}
            for item in installs
        ),
    }
    return {
        "accepted": all(checks.values()),
        "checks": checks,
        "evidence": summary,
    }


def _pilot_install_storage_summary(
    config_payload: Any,
    storage_payload: Any,
    install_payload: Any,
    *,
    alias: str | None,
    storage_name: str,
) -> dict[str, Any]:
    """Prove one non-destructive pilot download, registration, and binding.

    The check deliberately follows lexical strings and never resolves a path.
    This makes a Finder-selected nested or symlink-spelled root part of the
    evidence rather than silently replacing it with its physical target.
    """

    config = (
        config_payload.get("config")
        if isinstance(config_payload, dict)
        and isinstance(config_payload.get("config"), dict)
        else {}
    )
    locations = (
        config.get("storage", {}).get("locations", [])
        if isinstance(config.get("storage"), dict)
        else []
    )
    profiles = config.get("models") if isinstance(config.get("models"), list) else []
    status_locations = (
        storage_payload.get("locations", [])
        if isinstance(storage_payload, dict)
        and isinstance(storage_payload.get("locations"), list)
        else []
    )
    installs = (
        install_payload.get("installs", [])
        if isinstance(install_payload, dict)
        and isinstance(install_payload.get("installs"), list)
        else []
    )
    profile = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and item.get("alias") == alias
        ),
        None,
    )
    location = next(
        (
            item
            for item in locations
            if isinstance(item, dict) and item.get("name") == storage_name
        ),
        None,
    )
    storage_status = next(
        (
            item
            for item in status_locations
            if isinstance(item, dict) and item.get("name") == storage_name
        ),
        None,
    )
    # Evidence is newest-first. Selecting the newest row for the alias ensures
    # an older successful download cannot hide a current failed/rebound one.
    install = next(
        (
            item
            for item in installs
            if isinstance(item, dict) and item.get("alias") == alias
        ),
        None,
    )
    events = (
        install.get("events", [])
        if isinstance(install, dict) and isinstance(install.get("events"), list)
        else []
    )
    statuses = [
        event.get("status")
        for event in events
        if isinstance(event, dict) and event.get("event") == "status"
    ]
    registration_transition = any(
        status == "registering" and "installed" in statuses[index + 1 :]
        for index, status in enumerate(statuses)
    )
    lexical_root = location.get("path") if isinstance(location, dict) else None
    destination = install.get("destination") if isinstance(install, dict) else None
    profile_model = profile.get("model") if isinstance(profile, dict) else None
    engine = install.get("engine") if isinstance(install, dict) else None
    profile_registration_matches = bool(
        isinstance(profile, dict)
        and isinstance(install, dict)
        and profile.get("engine") == engine
        and (
            (
                engine == "omlx"
                and profile_model == install.get("repo_id")
            )
            or (
                engine != "omlx"
                and _path_is_within(profile_model, destination)
            )
        )
    )
    bytes_downloaded = (
        install.get("bytes_downloaded") if isinstance(install, dict) else None
    )
    total_bytes = install.get("total_bytes") if isinstance(install, dict) else None
    checks = {
        "profile_found": profile is not None,
        "selected_storage_found": location is not None,
        "selected_storage_healthy": bool(
            storage_status is not None
            and storage_status.get("exists")
            and storage_status.get("is_directory")
            and storage_status.get("writable")
            and storage_status.get("volume_matches")
        ),
        "latest_alias_install_found": install is not None,
        "latest_alias_install_completed": bool(
            isinstance(install, dict) and install.get("status") == "installed"
        ),
        "install_storage_matches_selection": bool(
            isinstance(install, dict) and install.get("storage") == storage_name
        ),
        "install_destination_within_lexical_root": bool(
            _path_is_within(destination, lexical_root)
        ),
        "registered_profile_matches_install": profile_registration_matches,
        "exact_revision_pinned": bool(
            isinstance(install, dict) and install.get("revision")
        ),
        "download_bytes_complete": bool(
            isinstance(bytes_downloaded, int)
            and not isinstance(bytes_downloaded, bool)
            and isinstance(total_bytes, int)
            and not isinstance(total_bytes, bool)
            and total_bytes > 0
            and bytes_downloaded >= total_bytes
        ),
        "registration_transition_recorded": registration_transition,
    }
    return {
        "alias": alias,
        "storage": (
            {
                "name": location.get("name"),
                "path": lexical_root,
                "volume_uuid_configured": bool(location.get("volume_uuid")),
                "scope_configured": bool(location.get("scope_id")),
            }
            if isinstance(location, dict)
            else None
        ),
        "install": (
            {
                "id": install.get("id"),
                "repo_id": install.get("repo_id"),
                "engine": engine,
                "status": install.get("status"),
                "revision_pinned": bool(install.get("revision")),
                "bytes_downloaded": bytes_downloaded,
                "total_bytes": total_bytes,
            }
            if isinstance(install, dict)
            else None
        ),
        "checks": checks,
        "accepted": all(checks.values()),
    }


def _protected_model_summary(
    config_payload: Any,
    storage_payload: Any,
    *,
    alias: str | None,
) -> dict[str, Any]:
    config = (
        config_payload.get("config")
        if isinstance(config_payload, dict)
        and isinstance(config_payload.get("config"), dict)
        else {}
    )
    storage_config = config.get("storage")
    locations = (
        storage_config.get("locations", [])
        if isinstance(storage_config, dict)
        else []
    )
    models = config.get("models") if isinstance(config.get("models"), list) else []
    status_locations = (
        storage_payload.get("locations", [])
        if isinstance(storage_payload, dict)
        and isinstance(storage_payload.get("locations"), list)
        else []
    )
    profile = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("alias") == alias
        ),
        None,
    )
    location = next(
        (
            item
            for item in locations
            if isinstance(item, dict)
            and profile is not None
            and item.get("name") == profile.get("storage")
        ),
        None,
    )
    status = next(
        (
            item
            for item in status_locations
            if isinstance(item, dict)
            and location is not None
            and item.get("name") == location.get("name")
        ),
        None,
    )
    checks = {
        "profile_found": profile is not None,
        "manager_owned_llamacpp": bool(
            profile is not None and profile.get("engine") == "llama.cpp"
        ),
        "storage_found": location is not None,
        "receiver_scope_configured": bool(
            location is not None
            and isinstance(location.get("scope_id"), str)
            and re.fullmatch(r"[0-9a-f]{64}", location["scope_id"])
        ),
        "volume_identity_configured": bool(
            location is not None and location.get("volume_uuid")
        ),
        "storage_available": bool(
            status is not None
            and status.get("exists")
            and status.get("is_directory")
            and status.get("writable")
            and status.get("volume_matches")
        ),
    }
    return {
        "alias": alias,
        "storage": (
            {
                "name": location.get("name"),
                "path": location.get("path"),
                "scope_configured": bool(location.get("scope_id")),
                "volume_uuid_configured": bool(location.get("volume_uuid")),
            }
            if location is not None
            else None
        ),
        "checks": checks,
        "accepted": all(checks.values()),
    }


def _path_is_within(path: Any, root: Any) -> bool:
    if not isinstance(path, str) or not isinstance(root, str):
        return False
    try:
        normalized_path = os.path.normcase(os.path.normpath(path))
        normalized_root = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except ValueError:
        return False


def _lmstudio_adoption_summary(
    config_payload: Any,
    sources_payload: Any,
    *,
    alias: str | None,
    lmstudio_offline: bool,
) -> dict[str, Any]:
    config = (
        config_payload.get("config")
        if isinstance(config_payload, dict)
        and isinstance(config_payload.get("config"), dict)
        else {}
    )
    engines = config.get("engines") if isinstance(config.get("engines"), dict) else {}
    models = config.get("models") if isinstance(config.get("models"), list) else []
    migration = (
        config.get("migration")
        if isinstance(config.get("migration"), dict)
        else {}
    )
    legacy = (
        migration.get("legacy_lmstudio_profiles", [])
        if isinstance(migration.get("legacy_lmstudio_profiles"), list)
        else []
    )
    storage = config.get("storage") if isinstance(config.get("storage"), dict) else {}
    locations = (
        storage.get("locations", [])
        if isinstance(storage.get("locations"), list)
        else []
    )
    sources = (
        sources_payload.get("sources", [])
        if isinstance(sources_payload, dict)
        and isinstance(sources_payload.get("sources"), list)
        else []
    )
    lmstudio_sources = [
        item
        for item in sources
        if isinstance(item, dict)
        and str(item.get("source", "")).startswith("lmstudio-")
    ]
    profile = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("alias") == alias
        ),
        None,
    )
    location = next(
        (
            item
            for item in locations
            if isinstance(item, dict)
            and profile is not None
            and item.get("name") == profile.get("storage")
        ),
        None,
    )
    adopted_from_hint = bool(
        profile is not None
        and any(
            _path_is_within(profile.get("model"), source.get("path"))
            or (
                location is not None
                and _path_is_within(location.get("path"), source.get("path"))
            )
            for source in lmstudio_sources
        )
    )
    checks = {
        "lmstudio_engine_absent": "lmstudio" not in engines,
        "native_profile_found": bool(
            profile is not None
            and profile.get("engine") in {"llama.cpp", "omlx"}
        ),
        "legacy_alias_consumed": not any(
            isinstance(item, dict) and item.get("alias") == alias
            for item in legacy
        ),
        "lmstudio_source_hint_present": bool(lmstudio_sources),
        "profile_uses_hinted_library": adopted_from_hint,
        "lmstudio_listener_offline": lmstudio_offline,
    }
    return {
        "alias": alias,
        "engine": profile.get("engine") if profile is not None else None,
        "source_hints": [
            {
                "id": item.get("id"),
                "path": item.get("path"),
                "source": item.get("source"),
            }
            for item in lmstudio_sources
        ],
        "checks": checks,
        "accepted": all(checks.values()),
    }


def _loopback_port_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        return True


def _usage_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    sidecar = payload.get("token_sidecar")
    rows = payload.get("rows")
    summary_rows: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            summary_rows.append(
                {
                    "event_id": redact(row.get("event_id"), key="event_id"),
                    "timestamp": row.get("timestamp", row.get("ts")),
                    "model": redact(
                        row.get("model")
                        or row.get("alias")
                        or row.get("requested_model")
                    ),
                    "engine": row.get("engine", row.get("backend")),
                    "endpoint": row.get("endpoint"),
                    "prompt_tokens": row.get("prompt_tokens"),
                    "completion_tokens": row.get("completion_tokens"),
                    "total_tokens": row.get("total_tokens"),
                }
            )
    return {
        "row_count_returned": len(rows) if isinstance(rows, list) else None,
        "recent_rows": summary_rows,
        "token_sidecar": redact(sidecar),
    }


def _postgres_drained(payload: Any, *, since: float) -> bool:
    if not isinstance(payload, dict):
        return False
    sidecar = payload.get("token_sidecar")
    if not isinstance(sidecar, dict):
        return False
    last_flush = sidecar.get("last_flush_at")
    return bool(
        sidecar.get("enabled")
        and sidecar.get("writer_ready")
        and sidecar.get("outbox_depth") == 0
        and sidecar.get("outbox_pending") == 0
        and sidecar.get("last_error") in (None, "")
        and isinstance(last_flush, (int, float))
        and float(last_flush) >= since
    )


def _runtime_lifecycle_summary(
    payload: Any,
    *,
    engine: str,
) -> dict[str, Any]:
    """Require an update/restart/rollback/restart/rejection proof chain."""

    journal = payload.get("journal") if isinstance(payload, dict) else None
    installed = payload.get("installed") if isinstance(payload, dict) else None
    events = (
        journal.get("events")
        if isinstance(journal, dict) and isinstance(journal.get("events"), list)
        else []
    )
    last_reset = max(
        (
            index
            for index, event in enumerate(events)
            if isinstance(event, dict) and event.get("action") == "journal_reset"
        ),
        default=-1,
    )
    engine_events = [
        event
        for event in events[last_reset + 1 :]
        if isinstance(event, dict) and event.get("engine") == engine
    ]
    installed_row = (
        installed.get(engine)
        if isinstance(installed, dict) and isinstance(installed.get(engine), dict)
        else {}
    )
    current_version = installed_row.get("version")

    def later(
        start: int,
        *,
        action: str,
        outcome: str,
        before: object = ...,
        after: object = ...,
        failure_codes: set[str] | None = None,
        different_instance_from: str | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        for index in range(start + 1, len(engine_events)):
            event = engine_events[index]
            if event.get("action") != action or event.get("outcome") != outcome:
                continue
            if before is not ... and event.get("active_version_before") != before:
                continue
            if after is not ... and event.get("active_version_after") != after:
                continue
            if (
                failure_codes is not None
                and event.get("failure_code") not in failure_codes
            ):
                continue
            if (
                different_instance_from is not None
                and event.get("service_instance_id") == different_instance_from
            ):
                continue
            return index, event
        return None

    matched: dict[str, dict[str, Any]] | None = None
    for activation_index in range(len(engine_events) - 1, -1, -1):
        activation = engine_events[activation_index]
        baseline = activation.get("active_version_before")
        updated = activation.get("active_version_after")
        activation_instance = activation.get("service_instance_id")
        if (
            activation.get("action") != "activated"
            or activation.get("outcome") != "succeeded"
            or not isinstance(baseline, str)
            or not isinstance(updated, str)
            or baseline == updated
            or not isinstance(activation_instance, str)
        ):
            continue
        updated_validation = later(
            activation_index,
            action="inference_validated",
            outcome="succeeded",
            before=updated,
            after=updated,
            different_instance_from=activation_instance,
        )
        if updated_validation is None:
            continue
        rollback = later(
            updated_validation[0],
            action="rolled_back",
            outcome="succeeded",
            before=updated,
            after=baseline,
        )
        if rollback is None:
            continue
        rollback_instance = rollback[1].get("service_instance_id")
        if not isinstance(rollback_instance, str):
            continue
        baseline_validation = later(
            rollback[0],
            action="inference_validated",
            outcome="succeeded",
            before=baseline,
            after=baseline,
            different_instance_from=rollback_instance,
        )
        if baseline_validation is None:
            continue
        rejected = later(
            baseline_validation[0],
            action="install_rejected",
            outcome="failed",
            before=baseline,
            after=baseline,
            failure_codes={"integrity", "unsafe_archive"},
        )
        if rejected is None:
            continue
        matched = {
            "activated": activation,
            "updated_inference_after_restart": updated_validation[1],
            "rolled_back": rollback[1],
            "baseline_inference_after_restart": baseline_validation[1],
            "corrupt_update_rejected": rejected[1],
        }
        break

    def bounded_event(event: dict[str, Any]) -> dict[str, Any]:
        return {
            key: event.get(key)
            for key in (
                "sequence",
                "event_id",
                "service_instance_id",
                "action",
                "outcome",
                "requested_version",
                "prepared_version",
                "active_version_before",
                "active_version_after",
                "source_revision",
                "failure_code",
                "created_at",
            )
        }

    checks = {
        "journal_valid": bool(
            isinstance(journal, dict) and journal.get("valid") is True
        ),
        "complete_transition_chain": matched is not None,
        "baseline_restored": bool(
            matched is not None
            and current_version
            == matched["activated"].get("active_version_before")
        ),
    }
    return {
        "engine": engine,
        "current_version": current_version,
        "journal_event_count": len(events),
        "dropped_events": (
            journal.get("dropped_events") if isinstance(journal, dict) else None
        ),
        "last_journal_reset_sequence": (
            events[last_reset].get("sequence")
            if last_reset >= 0 and isinstance(events[last_reset], dict)
            else None
        ),
        "checks": checks,
        "matched_events": (
            {name: bounded_event(event) for name, event in matched.items()}
            if matched is not None
            else None
        ),
        "recent_events": [
            bounded_event(event) for event in engine_events[-25:]
        ],
        "accepted": all(checks.values()),
    }


def _guided_setup_preferences() -> dict[str, str | None]:
    keys = (
        "didCompleteNativeSetupV1",
        "nativeSetupFirstPresentedVersionV1",
        "nativeSetupFirstPresentedBuildV1",
        "nativeSetupFirstPresentedAtV1",
        "nativeSetupCompletedVersionV1",
        "nativeSetupCompletedBuildV1",
        "nativeSetupCompletedAtV1",
    )
    values: dict[str, str | None] = {}
    for key in keys:
        result = _run(
            ["/usr/bin/defaults", "read", PREFERENCES_DOMAIN, key],
            timeout=5,
        )
        value = result.get("diagnostic")
        values[key] = (
            str(value).strip() if result.get("ok") and value is not None else None
        )
    return values


def _guided_setup_summary(
    preferences: Any,
    *,
    expected_version: str,
    expected_build: str | None,
) -> dict[str, Any]:
    values = preferences if isinstance(preferences, dict) else {}

    def timestamp(key: str) -> float | None:
        try:
            return float(values.get(key))
        except (TypeError, ValueError):
            return None

    first_at = timestamp("nativeSetupFirstPresentedAtV1")
    completed_at = timestamp("nativeSetupCompletedAtV1")
    first_build = values.get("nativeSetupFirstPresentedBuildV1")
    completed_build = values.get("nativeSetupCompletedBuildV1")
    checks = {
        "first_run_setup_presented_by_candidate": (
            values.get("nativeSetupFirstPresentedVersionV1")
            == expected_version
            and first_build == expected_build
        ),
        "setup_completed_by_candidate": (
            str(values.get("didCompleteNativeSetupV1", "")).casefold()
            in {"1", "true", "yes"}
            and values.get("nativeSetupCompletedVersionV1") == expected_version
            and completed_build == expected_build
        ),
        "presentation_precedes_completion": bool(
            first_at is not None
            and completed_at is not None
            and first_at > 0
            and completed_at >= first_at
        ),
    }
    return {
        "preferences_domain": PREFERENCES_DOMAIN,
        "expected_version": expected_version,
        "expected_build": expected_build,
        "first_presented_version": values.get(
            "nativeSetupFirstPresentedVersionV1"
        ),
        "first_presented_build": first_build,
        "first_presented_at": first_at,
        "completed_version": values.get("nativeSetupCompletedVersionV1"),
        "completed_build": completed_build,
        "completed_at": completed_at,
        "checks": checks,
        "accepted": bool(expected_build) and all(checks.values()),
    }


def _login_cycle_summary(
    baseline_path: Path,
    *,
    current_agent: Any,
    expected_version: str,
    expected_build: str | None,
    current_host: str,
) -> dict[str, Any]:
    diagnostic: str | None = None
    payload: Any = None
    try:
        if baseline_path.is_symlink() or not baseline_path.is_file():
            raise ValueError("baseline is not a regular report file")
        baseline_stat = baseline_path.stat()
        if baseline_stat.st_size <= 0 or baseline_stat.st_size > 10 * 1024 * 1024:
            raise ValueError("baseline report size is invalid")
        if baseline_stat.st_mode & 0o077:
            raise ValueError("baseline report is not private")
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        diagnostic = "login-cycle baseline report is unavailable or invalid"

    artifact = (
        payload.get("artifact")
        if isinstance(payload, dict) and isinstance(payload.get("artifact"), dict)
        else {}
    )
    baseline_app = (
        artifact.get("app")
        if isinstance(artifact.get("app"), dict)
        else {}
    )
    baseline_live = (
        payload.get("live")
        if isinstance(payload, dict) and isinstance(payload.get("live"), dict)
        else {}
    )
    baseline_agent = (
        baseline_live.get("launch_agent")
        if isinstance(baseline_live.get("launch_agent"), dict)
        else {}
    )
    baseline_asid = baseline_agent.get("asid")
    current_asid = (
        current_agent.get("asid") if isinstance(current_agent, dict) else None
    )
    baseline_generated_at = (
        payload.get("generated_at") if isinstance(payload, dict) else None
    )
    try:
        baseline_timestamp = (
            datetime.fromisoformat(baseline_generated_at).timestamp()
            if isinstance(baseline_generated_at, str)
            else None
        )
    except ValueError:
        baseline_timestamp = None
    checks = {
        "baseline_report_private_and_accepted": bool(
            diagnostic is None
            and isinstance(payload, dict)
            and payload.get("accepted") is True
            and baseline_live.get("accepted") is True
        ),
        "same_candidate_build": bool(
            expected_build
            and baseline_app.get("version") == expected_version
            and baseline_app.get("build") == expected_build
        ),
        "same_target_host": bool(
            isinstance(payload, dict) and payload.get("host") == current_host
        ),
        "baseline_predates_current_collection": bool(
            baseline_timestamp is not None and baseline_timestamp <= time.time()
        ),
        "baseline_launch_agent_running": bool(
            baseline_agent.get("registered")
            and baseline_agent.get("state") == "running"
            and isinstance(baseline_agent.get("pid"), int)
            and isinstance(baseline_asid, int)
        ),
        "new_gui_audit_session": bool(
            isinstance(baseline_asid, int)
            and isinstance(current_asid, int)
            and current_asid != baseline_asid
        ),
        "new_launch_agent_process": bool(
            isinstance(baseline_agent.get("pid"), int)
            and isinstance(current_agent, dict)
            and current_agent.get("registered") is True
            and isinstance(current_agent.get("pid"), int)
            and current_agent.get("pid") != baseline_agent.get("pid")
            and current_agent.get("state") == "running"
        ),
    }
    return {
        "baseline_path": _display_path(baseline_path),
        "baseline_generated_at": baseline_generated_at,
        "baseline_asid": baseline_asid,
        "current_asid": current_asid,
        "diagnostic": diagnostic,
        "checks": checks,
        "accepted": all(checks.values()),
    }


def collect_live(
    *,
    expected_version: str,
    expected_build: str | None,
    control_url: str,
    public_url: str,
    admin_password: str | None,
    self_test_model: str | None,
    include_vision: bool,
    expected_engine: str | None,
    require_vision: bool,
    require_postgres_drain: bool,
    postgres_timeout: float,
    launch_agent_exercise: str | None = None,
    service_restart_timeout: float = 60,
    exercise_reconcile: bool = False,
    require_protected_model: bool = False,
    require_download_lifecycle: bool = False,
    require_pilot_install_storage: str | None = None,
    require_cold_jit: bool = False,
    require_lmstudio_adoption: str | None = None,
    require_omlx_recovery: bool = False,
    require_runtime_lifecycle: str | None = None,
    require_guided_setup: bool = False,
    exercise_fleet_participation: bool = False,
    login_cycle_baseline: Path | None = None,
) -> dict[str, Any]:
    control = control_url.rstrip("/")
    public = public_url.rstrip("/")
    agent_exercise = (
        _exercise_launch_agent(
            launch_agent_exercise,
            control_url=control,
            public_url=public,
            admin_password=admin_password,
            timeout=service_restart_timeout,
        )
        if launch_agent_exercise is not None
        else None
    )
    health = _json_request(f"{public}/health")
    readiness = _json_request(
        f"{control}/manager/readiness",
        admin_password=admin_password,
    )
    status = _json_request(
        f"{control}/manager/status",
        admin_password=admin_password,
    )
    models = _json_request(
        f"{control}/manager/models",
        admin_password=admin_password,
    )
    usage = _json_request(
        f"{control}/manager/usage?limit=5",
        admin_password=admin_password,
    )
    storage = _json_request(
        f"{control}/manager/storage",
        admin_password=admin_password,
    )
    configuration = _json_request(
        f"{control}/manager/config",
        admin_password=admin_password,
    )
    install_evidence = _json_request(
        f"{control}/manager/model-library/install-evidence?limit=100",
        admin_password=admin_password,
    )
    local_sources = _json_request(
        f"{control}/manager/model-library/local-sources",
        admin_password=admin_password,
    )
    fleet_participation = (
        _exercise_fleet_participation(
            control_url=control,
            admin_password=admin_password,
        )
        if exercise_fleet_participation
        else None
    )
    reconcile = (
        _json_request(
            f"{control}/manager/reconcile",
            admin_password=admin_password,
            payload={},
            timeout=60,
        )
        if exercise_reconcile
        else None
    )
    if reconcile is not None and reconcile.get("ok"):
        # Capture final authoritative state after the mutation rather than the
        # pre-reconcile diagnostic that prompted the exercise.
        readiness = _json_request(
            f"{control}/manager/readiness",
            admin_password=admin_password,
        )
        status = _json_request(
            f"{control}/manager/status",
            admin_password=admin_password,
        )
    self_test = None
    self_test_started: float | None = None
    pre_self_test_status = status
    post_self_test_status: dict[str, Any] | None = None
    if self_test_model:
        self_test_started = time.time()
        self_test = _json_request(
            f"{control}/manager/self-test",
            admin_password=admin_password,
            payload={
                "model": self_test_model,
                "include_vision": include_vision,
                "unload_after": True,
            },
            timeout=180,
        )
        if require_cold_jit:
            post_self_test_status = _json_request(
                f"{control}/manager/status",
                admin_password=admin_password,
            )
    runtime_evidence = _json_request(
        f"{control}/manager/runtime-updates/evidence",
        admin_password=admin_password,
    )
    postgres_drain: dict[str, Any] | None = None
    if require_postgres_drain and self_test_started is not None:
        deadline = time.monotonic() + postgres_timeout
        latest_usage = usage
        while True:
            latest_usage = _json_request(
                f"{control}/manager/usage?limit=5",
                admin_password=admin_password,
            )
            if latest_usage.get("ok") and _postgres_drained(
                latest_usage.get("payload"),
                since=self_test_started,
            ):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        usage = latest_usage
        postgres_drain = {
            "required": True,
            "timeout_seconds": postgres_timeout,
            "accepted": bool(
                usage.get("ok")
                and _postgres_drained(
                    usage.get("payload"),
                    since=self_test_started,
                )
            ),
            "status": _usage_summary(usage.get("payload")),
            "diagnostic": usage.get("diagnostic"),
        }

    readiness_payload = readiness.get("payload")
    health_payload = health.get("payload")
    product_version = (
        readiness_payload.get("product_version")
        if isinstance(readiness_payload, dict)
        else None
    )
    if product_version is None and isinstance(health_payload, dict):
        product_version = health_payload.get("version")
    core_ready = bool(
        isinstance(readiness_payload, dict)
        and isinstance(readiness_payload.get("core"), dict)
        and readiness_payload["core"].get("ready")
    )
    ready_for_inference = bool(
        isinstance(readiness_payload, dict)
        and readiness_payload.get("ready_for_inference")
    )
    self_test_payload = self_test.get("payload") if isinstance(self_test, dict) else None
    self_test_accepted = (
        bool(
            self_test.get("ok")
            and isinstance(self_test_payload, dict)
            and self_test_payload.get("success")
            and self_test_payload.get("usage")
            and self_test_payload.get("usage_recorded") is True
        )
        if self_test is not None
        else None
    )
    config_payload = configuration.get("payload")
    storage_payload = storage.get("payload")
    install_payload = install_evidence.get("payload")
    sources_payload = local_sources.get("payload")
    protected_model = (
        _protected_model_summary(
            config_payload,
            storage_payload,
            alias=self_test_model,
        )
        if require_protected_model
        else None
    )
    download_lifecycle = (
        _download_lifecycle_summary(install_payload)
        if require_download_lifecycle
        else None
    )
    pilot_install_storage = (
        _pilot_install_storage_summary(
            config_payload,
            storage_payload,
            install_payload,
            alias=self_test_model,
            storage_name=require_pilot_install_storage,
        )
        if require_pilot_install_storage is not None
        else None
    )
    lmstudio_adoption = (
        _lmstudio_adoption_summary(
            config_payload,
            sources_payload,
            alias=require_lmstudio_adoption,
            lmstudio_offline=_loopback_port_closed(1234),
        )
        if require_lmstudio_adoption is not None
        else None
    )
    runtime_lifecycle = (
        _runtime_lifecycle_summary(
            runtime_evidence.get("payload"),
            engine=require_runtime_lifecycle,
        )
        if require_runtime_lifecycle is not None
        else None
    )
    guided_setup = (
        _guided_setup_summary(
            _guided_setup_preferences(),
            expected_version=expected_version,
            expected_build=expected_build,
        )
        if require_guided_setup
        else None
    )
    engine_rows = (
        readiness_payload.get("engines")
        if isinstance(readiness_payload, dict)
        and isinstance(readiness_payload.get("engines"), list)
        else []
    )
    omlx_row = next(
        (
            item
            for item in engine_rows
            if isinstance(item, dict) and item.get("engine") == "omlx"
        ),
        None,
    )
    reconcile_payload = (
        reconcile.get("payload") if isinstance(reconcile, dict) else None
    )
    reconcile_accepted = bool(
        isinstance(reconcile, dict)
        and reconcile.get("ok")
        and isinstance(reconcile_payload, dict)
        and reconcile_payload.get("diagnostic") in (None, "")
        and reconcile_payload.get("startup_error") in (None, "")
        and reconcile_payload.get("state") in {"idle", "ready"}
    )
    agent = _launch_agent()
    login_cycle = (
        _login_cycle_summary(
            login_cycle_baseline,
            current_agent=agent,
            expected_version=expected_version,
            expected_build=expected_build,
            current_host=socket.gethostname().split(".", 1)[0],
        )
        if login_cycle_baseline is not None
        else None
    )
    checks = {
        "launch_agent_running": agent["registered"] and agent["state"] == "running",
        "public_health_reachable": health["ok"],
        "control_reachable": readiness["ok"] and status["ok"],
        "product_version_matches": product_version == expected_version,
        "core_ready": core_ready,
        "ready_for_inference": ready_for_inference,
        "model_catalog_reachable": models["ok"],
        "usage_reachable": usage["ok"],
        "storage_diagnostics_reachable": storage["ok"],
        "configuration_snapshot_reachable": configuration["ok"],
        "download_evidence_reachable": install_evidence["ok"],
        "local_migration_hints_reachable": local_sources["ok"],
        "runtime_lifecycle_evidence_reachable": runtime_evidence["ok"],
    }
    if agent_exercise is not None:
        checks[f"launch_agent_{launch_agent_exercise}"] = bool(
            agent_exercise.get("accepted")
        )
    if reconcile is not None:
        checks["authoritative_reconcile"] = reconcile_accepted
    if self_test is not None:
        checks["self_test_usage_durable"] = self_test_accepted
        checks["self_test_stable_engine"] = bool(
            isinstance(self_test_payload, dict)
            and self_test_payload.get("release_tier") == "stable"
        )
    if expected_engine is not None:
        checks["self_test_engine_matches"] = bool(
            isinstance(self_test_payload, dict)
            and self_test_payload.get("engine") == expected_engine
        )
    if require_vision:
        checks["self_test_used_vision"] = bool(
            isinstance(self_test_payload, dict)
            and self_test_payload.get("vision") is True
        )
    if postgres_drain is not None:
        checks["postgres_outbox_drained_after_self_test"] = postgres_drain["accepted"]
    if protected_model is not None:
        checks["protected_model_reactivated_after_restart"] = bool(
            protected_model["accepted"] and self_test_accepted
        )
    if download_lifecycle is not None:
        checks["durable_download_lifecycle"] = bool(
            download_lifecycle["accepted"]
        )
    if pilot_install_storage is not None:
        checks["pilot_download_registered_at_selected_storage"] = bool(
            pilot_install_storage["accepted"] and self_test_accepted
        )
    if require_cold_jit:
        pre_payload = pre_self_test_status.get("payload")
        post_payload = (
            post_self_test_status.get("payload")
            if isinstance(post_self_test_status, dict)
            else None
        )
        checks["cold_jit_from_empty_residency"] = bool(
            isinstance(pre_payload, dict)
            and pre_payload.get("resident_alias") is None
            and pre_payload.get("resident_engine") is None
            and pre_payload.get("in_flight_requests") == 0
            and pre_payload.get("queued") == 0
            and isinstance(self_test_payload, dict)
            and self_test_payload.get("cold_start") is True
            and self_test_payload.get("unloaded_after") is True
            and isinstance(post_self_test_status, dict)
            and post_self_test_status.get("ok")
            and isinstance(post_payload, dict)
            and post_payload.get("resident_alias") is None
            and post_payload.get("resident_engine") is None
            and post_payload.get("in_flight_requests") == 0
            and post_payload.get("queued") == 0
        )
    if lmstudio_adoption is not None:
        checks["lmstudio_directory_adopted_without_engine"] = bool(
            lmstudio_adoption["accepted"]
            and self_test_model == require_lmstudio_adoption
            and self_test_accepted
        )
    if require_omlx_recovery:
        checks["omlx_core_restart_auth_recovery"] = bool(
            agent_exercise is not None
            and agent_exercise.get("accepted")
            and reconcile_accepted
            and isinstance(omlx_row, dict)
            and omlx_row.get("enabled")
            and omlx_row.get("authoritative")
            and omlx_row.get("ready")
            and isinstance(self_test_payload, dict)
            and self_test_payload.get("engine") == "omlx"
            and self_test_accepted
        )
    if runtime_lifecycle is not None:
        checks["managed_runtime_update_rollback_recovery"] = bool(
            runtime_lifecycle["accepted"]
            and agent_exercise is not None
            and agent_exercise.get("accepted")
            and self_test_accepted
            and isinstance(self_test_payload, dict)
            and self_test_payload.get("engine") == require_runtime_lifecycle
            and self_test_payload.get("runtime_validation_recorded") is True
        )
    if guided_setup is not None:
        checks["guided_clean_install_completed"] = bool(
            guided_setup["accepted"] and self_test_accepted
        )
    if fleet_participation is not None:
        checks["fleet_participation_pause_rejoin_restored"] = bool(
            fleet_participation["accepted"]
        )
    if login_cycle is not None:
        checks["login_cycle_launchagent_recovery"] = bool(
            login_cycle["accepted"] and self_test_accepted
        )
    result = {
        "control_url": _redact_url(control),
        "public_url": _redact_url(public),
        "launch_agent": agent,
        "launch_agent_exercise": agent_exercise,
        "health": health,
        "readiness": readiness,
        "status": status,
        "models": {
            "ok": models["ok"],
            "status": models.get("status"),
            "model_count": (
                len(models["payload"].get("models", []))
                if models.get("ok") and isinstance(models.get("payload"), dict)
                else None
            ),
            "diagnostic": models.get("diagnostic"),
        },
        "usage": {
            "ok": usage["ok"],
            "status": usage.get("status"),
            "summary": _usage_summary(usage.get("payload")),
            "diagnostic": usage.get("diagnostic"),
        },
        "storage": {
            "ok": storage["ok"],
            "status": storage.get("status"),
            "payload": storage.get("payload"),
            "diagnostic": storage.get("diagnostic"),
        },
        "configuration": {
            "ok": configuration["ok"],
            "status": configuration.get("status"),
            "summary": _configuration_summary(config_payload),
            "diagnostic": configuration.get("diagnostic"),
        },
        "install_evidence": {
            "ok": install_evidence["ok"],
            "status": install_evidence.get("status"),
            "summary": _install_evidence_summary(install_payload),
            "diagnostic": install_evidence.get("diagnostic"),
        },
        "local_sources": {
            "ok": local_sources["ok"],
            "status": local_sources.get("status"),
            "payload": local_sources.get("payload"),
            "diagnostic": local_sources.get("diagnostic"),
        },
        "runtime_evidence": {
            "ok": runtime_evidence["ok"],
            "status": runtime_evidence.get("status"),
            "summary": runtime_lifecycle,
            "diagnostic": runtime_evidence.get("diagnostic"),
        },
        "reconcile": reconcile,
        "protected_model": protected_model,
        "download_lifecycle": download_lifecycle,
        "pilot_install_storage": pilot_install_storage,
        "lmstudio_adoption": lmstudio_adoption,
        "runtime_lifecycle": runtime_lifecycle,
        "guided_setup": guided_setup,
        "fleet_participation": fleet_participation,
        "login_cycle": login_cycle,
        "postgres_drain": postgres_drain,
        "self_test": self_test,
        "pre_self_test_status": (
            pre_self_test_status if require_cold_jit else None
        ),
        "post_self_test_status": post_self_test_status,
        "checks": checks,
    }
    result["accepted"] = all(result["checks"].values())
    return redact(result)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect secret-safe Unified Inference acceptance evidence."
    )
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--dmg", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--require-distribution", action="store_true")
    parser.add_argument(
        "--allow-bare",
        action="store_true",
        help="allow an intentionally runtime-free CI bundle",
    )
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--public-url", default=PUBLIC_URL)
    parser.add_argument(
        "--admin-password-env",
        default="MNEMOSYNE_ADMIN_PASSWORD",
        help="environment variable holding control-plane Basic auth password",
    )
    parser.add_argument("--self-test", metavar="ALIAS")
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="opt out of automatic vision input for the optional self-test",
    )
    parser.add_argument(
        "--expected-engine",
        choices=("llama.cpp", "omlx", "ds4", "mflux"),
        help="require the self-test to resolve through this engine",
    )
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help="require the self-test to select a configured vision projector",
    )
    parser.add_argument(
        "--require-postgres-drain",
        action="store_true",
        help="wait for the self-test usage event to leave the Postgres outbox",
    )
    parser.add_argument(
        "--postgres-timeout",
        type=float,
        default=60,
        help="seconds to wait for a required Postgres outbox drain",
    )
    parser.add_argument(
        "--exercise-service-restart",
        action="store_true",
        help=(
            "restart the exact registered LaunchAgent with launchctl and wait "
            "for both HTTP planes to return"
        ),
    )
    parser.add_argument(
        "--exercise-keepalive",
        action="store_true",
        help=(
            "send SIGTERM through launchctl and require KeepAlive to return "
            "the exact registered LaunchAgent"
        ),
    )
    parser.add_argument(
        "--service-restart-timeout",
        type=float,
        default=60,
        help="seconds to wait for a LaunchAgent restart or KeepAlive exercise",
    )
    parser.add_argument(
        "--exercise-reconcile",
        action="store_true",
        help="run authoritative engine reconciliation before the self-test",
    )
    parser.add_argument(
        "--require-protected-model",
        action="store_true",
        help=(
            "require the self-tested llama.cpp alias to use a healthy "
            "Finder-authorized storage scope after a service restart"
        ),
    )
    parser.add_argument(
        "--require-download-lifecycle",
        action="store_true",
        help=(
            "require durable target-Mac evidence for cancellation, retry, "
            "registration retry, dismissal, and managed deletion"
        ),
    )
    parser.add_argument(
        "--require-pilot-install-storage",
        metavar="STORAGE",
        help=(
            "require the self-tested alias to have a completed, revision-pinned "
            "download and registration at this exact configured storage name"
        ),
    )
    parser.add_argument(
        "--require-cold-jit",
        action="store_true",
        help=(
            "require empty residency before the self-test, an authoritative "
            "cold admission, and empty residency after its requested unload"
        ),
    )
    parser.add_argument(
        "--require-lmstudio-adoption",
        metavar="ALIAS",
        help=(
            "require a native self-tested alias adopted from an LM Studio "
            "directory hint while the LM Studio listener is offline"
        ),
    )
    parser.add_argument(
        "--require-omlx-recovery",
        action="store_true",
        help=(
            "require oMLX readiness, authoritative reconciliation, and a "
            "successful oMLX self-test after core service restart"
        ),
    )
    parser.add_argument(
        "--require-runtime-lifecycle",
        choices=("llama.cpp", "mflux", "ds4"),
        metavar="ENGINE",
        help=(
            "require a durable managed-runtime update, restart validation, "
            "rollback, second restart validation, and corrupt-update rejection"
        ),
    )
    parser.add_argument(
        "--require-guided-setup",
        action="store_true",
        help=(
            "require this exact app version/build to have presented first-run "
            "Setup & Health and completed it through the durable-usage self-test"
        ),
    )
    parser.add_argument(
        "--exercise-fleet-participation",
        action="store_true",
        help=(
            "require an idle local pause/rejoin cycle, restore the exact prior "
            "preference, and prove model/runtime/storage configuration unchanged"
        ),
    )
    parser.add_argument(
        "--require-login-cycle-baseline",
        type=Path,
        metavar="REPORT",
        help=(
            "require a private accepted pre-logout report from this host/build "
            "and a new GUI audit session before the current durable self-test"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.exercise_service_restart and args.exercise_keepalive:
        parser.error(
            "--exercise-service-restart and --exercise-keepalive are mutually exclusive"
        )
    launch_agent_exercise = (
        "restart"
        if args.exercise_service_restart
        else ("keepalive" if args.exercise_keepalive else None)
    )
    if (
        args.require_postgres_drain
        or args.require_vision
        or args.require_protected_model
        or args.require_download_lifecycle
        or args.require_pilot_install_storage
        or args.require_cold_jit
        or args.require_lmstudio_adoption
        or args.require_omlx_recovery
        or args.require_runtime_lifecycle
        or args.require_guided_setup
        or args.exercise_fleet_participation
        or args.require_login_cycle_baseline
    ):
        args.require_live = True
    if args.require_live:
        args.live = True
        if not args.self_test:
            parser.error("--require-live requires --self-test")
    if args.require_postgres_drain:
        if not args.self_test:
            parser.error("--require-postgres-drain requires --self-test")
    if args.require_pilot_install_storage is not None and not args.self_test:
        parser.error("--require-pilot-install-storage requires --self-test")
    if args.require_cold_jit and not args.self_test:
        parser.error("--require-cold-jit requires --self-test")
    if (args.expected_engine or args.require_vision) and not args.self_test:
        parser.error("--expected-engine and --require-vision require --self-test")
    if args.text_only and args.require_vision:
        parser.error("--text-only and --require-vision conflict")
    if args.postgres_timeout <= 0:
        parser.error("--postgres-timeout must be greater than zero")
    if args.service_restart_timeout <= 0:
        parser.error("--service-restart-timeout must be greater than zero")
    if launch_agent_exercise is not None or args.exercise_reconcile:
        args.live = True
    if args.require_protected_model and launch_agent_exercise is None:
        parser.error(
            "--require-protected-model requires --exercise-service-restart "
            "or --exercise-keepalive"
        )
    if args.require_protected_model:
        if args.expected_engine not in (None, "llama.cpp"):
            parser.error(
                "--require-protected-model conflicts with --expected-engine"
            )
        args.expected_engine = "llama.cpp"
    if (
        args.require_lmstudio_adoption is not None
        and args.self_test != args.require_lmstudio_adoption
    ):
        parser.error(
            "--require-lmstudio-adoption must match the --self-test alias"
        )
    if args.require_omlx_recovery:
        if args.require_protected_model or args.require_vision:
            parser.error(
                "--require-omlx-recovery conflicts with protected llama.cpp "
                "or vision requirements"
            )
        if launch_agent_exercise is None or not args.exercise_reconcile:
            parser.error(
                "--require-omlx-recovery requires a service restart/KeepAlive "
                "exercise and --exercise-reconcile"
            )
        if args.expected_engine not in (None, "omlx"):
            parser.error("--require-omlx-recovery conflicts with --expected-engine")
        args.expected_engine = "omlx"
    if args.require_runtime_lifecycle:
        if launch_agent_exercise is None:
            parser.error(
                "--require-runtime-lifecycle requires "
                "--exercise-service-restart or --exercise-keepalive"
            )
        if args.expected_engine not in (None, args.require_runtime_lifecycle):
            parser.error(
                "--require-runtime-lifecycle conflicts with --expected-engine"
            )
        args.expected_engine = args.require_runtime_lifecycle
    if args.self_test and not args.live:
        parser.error("--self-test requires --live or --require-live")

    expected = VERSION_FILE.read_text(encoding="utf-8").strip()
    app_evidence = collect_app(
        args.app,
        expected,
        distribution=args.require_distribution,
        allow_bare=args.allow_bare,
    )
    expected_build_value = app_evidence.get("build")
    expected_build = (
        expected_build_value
        if isinstance(expected_build_value, str)
        and expected_build_value.isdigit()
        else None
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname().split(".", 1)[0],
        "expected_version": expected,
        "artifact": {
            "app": app_evidence,
            "dmg": (
                collect_dmg(
                    args.dmg,
                    expected,
                    distribution=args.require_distribution,
                )
                if args.dmg
                else None
            ),
        },
        "live": (
            collect_live(
                expected_version=expected,
                expected_build=expected_build,
                control_url=args.control_url,
                public_url=args.public_url,
                admin_password=os.environ.get(args.admin_password_env),
                self_test_model=args.self_test,
                include_vision=not args.text_only,
                expected_engine=args.expected_engine,
                require_vision=args.require_vision,
                require_postgres_drain=args.require_postgres_drain,
                postgres_timeout=args.postgres_timeout,
                launch_agent_exercise=launch_agent_exercise,
                service_restart_timeout=args.service_restart_timeout,
                exercise_reconcile=args.exercise_reconcile,
                require_protected_model=args.require_protected_model,
                require_download_lifecycle=args.require_download_lifecycle,
                require_pilot_install_storage=args.require_pilot_install_storage,
                require_cold_jit=args.require_cold_jit,
                require_lmstudio_adoption=args.require_lmstudio_adoption,
                require_omlx_recovery=args.require_omlx_recovery,
                require_runtime_lifecycle=args.require_runtime_lifecycle,
                require_guided_setup=args.require_guided_setup,
                exercise_fleet_participation=args.exercise_fleet_participation,
                login_cycle_baseline=args.require_login_cycle_baseline,
            )
            if args.live
            else None
        ),
    }
    report = redact(report)
    artifact_accepted = bool(report["artifact"]["app"]["accepted"]) and (
        report["artifact"]["dmg"] is None
        or bool(report["artifact"]["dmg"]["accepted"])
    )
    live_accepted = report["live"] is None or bool(report["live"]["accepted"])
    report["accepted"] = artifact_accepted and live_accepted

    if args.output:
        _write_report(args.output, report)
        print(f"Wrote acceptance evidence to {_display_path(args.output)}")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_live and not live_accepted:
        return 2
    return 0 if artifact_accepted else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as exc:
        print(f"acceptance collection failed: {redact(str(exc))}", file=sys.stderr)
        raise SystemExit(1) from exc

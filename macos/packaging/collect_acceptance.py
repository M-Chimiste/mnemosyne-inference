#!/usr/bin/env python3
"""Collect secret-safe, machine-readable evidence for a native Mac candidate."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
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


def _run(arguments: list[str], *, timeout: float = 20) -> dict[str, Any]:
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
        "diagnostic": _redact_text(output[:4000]) if output else None,
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
        method="POST" if payload is not None else "GET",
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
        "pid": integer("pid"),
        "runs": integer("runs"),
        "last_exit_code": integer("last exit code"),
    }


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


def collect_live(
    *,
    expected_version: str,
    control_url: str,
    public_url: str,
    admin_password: str | None,
    self_test_model: str | None,
    include_vision: bool,
    expected_engine: str | None,
    require_vision: bool,
    require_postgres_drain: bool,
    postgres_timeout: float,
) -> dict[str, Any]:
    control = control_url.rstrip("/")
    public = public_url.rstrip("/")
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
    self_test = None
    self_test_started: float | None = None
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
    agent = _launch_agent()
    checks = {
        "launch_agent_running": agent["registered"] and agent["state"] == "running",
        "public_health_reachable": health["ok"],
        "control_reachable": readiness["ok"] and status["ok"],
        "product_version_matches": product_version == expected_version,
        "core_ready": core_ready,
        "ready_for_inference": ready_for_inference,
        "model_catalog_reachable": models["ok"],
        "usage_reachable": usage["ok"],
    }
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
    result = {
        "control_url": _redact_url(control),
        "public_url": _redact_url(public),
        "launch_agent": agent,
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
        "postgres_drain": postgres_drain,
        "self_test": self_test,
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.require_live:
        args.live = True
        if not args.self_test:
            parser.error("--require-live requires --self-test")
    if args.require_postgres_drain:
        args.live = True
        if not args.self_test:
            parser.error("--require-postgres-drain requires --self-test")
    if (args.expected_engine or args.require_vision) and not args.self_test:
        parser.error("--expected-engine and --require-vision require --self-test")
    if args.text_only and args.require_vision:
        parser.error("--text-only and --require-vision conflict")
    if args.postgres_timeout <= 0:
        parser.error("--postgres-timeout must be greater than zero")
    if args.self_test and not args.live:
        parser.error("--self-test requires --live or --require-live")

    expected = VERSION_FILE.read_text(encoding="utf-8").strip()
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname().split(".", 1)[0],
        "expected_version": expected,
        "artifact": {
            "app": collect_app(
                args.app,
                expected,
                distribution=args.require_distribution,
                allow_bare=args.allow_bare,
            ),
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
                control_url=args.control_url,
                public_url=args.public_url,
                admin_password=os.environ.get(args.admin_password_env),
                self_test_model=args.self_test,
                include_vision=not args.text_only,
                expected_engine=args.expected_engine,
                require_vision=args.require_vision,
                require_postgres_drain=args.require_postgres_drain,
                postgres_timeout=args.postgres_timeout,
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

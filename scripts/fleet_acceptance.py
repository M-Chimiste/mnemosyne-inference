#!/usr/bin/env python3
"""Run a bounded, content-redacted acceptance probe through Mnemosyne Fleet.

The probe reads credentials from named environment variables, never prints
request or response bodies, and uses Fleet's metadata-only route history to
prove that simultaneous requests reached more than one eligible node.  It can
also wait for the serving nodes' normal durable token-delivery path to appear
in Nyx's read-only usage view.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import httpx


SUPPORTED_ENDPOINTS = {
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/embeddings",
    "/v1/rerank",
    "/v1/images/generations",
}
SERVICE_CLASSES = frozenset({"primary", "opportunistic", "overflow"})


def _parse_required_node_service_classes(
    values: list[str],
) -> dict[str, str]:
    """Parse repeatable NODE=CLASS assertions without guessing identities."""

    required: dict[str, str] = {}
    for value in values:
        node_id, separator, service_class = value.partition("=")
        node_id = node_id.strip()
        service_class = service_class.strip()
        if (
            separator != "="
            or not node_id
            or service_class not in SERVICE_CLASSES
            or (node_id in required and required[node_id] != service_class)
        ):
            raise ValueError(
                "--require-node-service-class must be a consistent "
                "NODE=primary|opportunistic|overflow assertion"
            )
        required[node_id] = service_class
    return required


def _require_node_service_classes(
    status_nodes: Mapping[str, Mapping[str, Any]],
    required: Mapping[str, str],
) -> None:
    problems: list[str] = []
    for node_id, expected in sorted(required.items()):
        node = status_nodes.get(node_id)
        if node is None:
            problems.append(f"{node_id}: status is unavailable")
            continue
        actual = node.get("service_class")
        if actual != expected:
            problems.append(
                f"{node_id}: expected {expected!r}, observed {actual!r}"
            )
    if problems:
        raise RuntimeError(
            "required node service classes do not match: "
            + "; ".join(problems)
        )


def _secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name!r} is unset")
    return value


def _default_request(endpoint: str) -> dict[str, Any]:
    if endpoint == "/v1/chat/completions":
        return {
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 2,
        }
    if endpoint == "/v1/completions":
        return {"prompt": "Reply with OK:", "max_tokens": 2}
    if endpoint == "/v1/responses":
        return {"input": "Reply with OK.", "max_output_tokens": 2}
    if endpoint == "/v1/messages":
        return {
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 2,
        }
    if endpoint == "/v1/embeddings":
        return {"input": "fleet acceptance probe"}
    if endpoint == "/v1/rerank":
        return {
            "query": "fleet acceptance probe",
            "documents": ["fleet acceptance probe"],
        }
    if endpoint == "/v1/images/generations":
        return {"prompt": "a plain blue square", "n": 1, "size": "256x256"}
    raise ValueError(f"unsupported endpoint {endpoint!r}")


def _load_request(path: Path | None, endpoint: str) -> dict[str, Any]:
    if path is None:
        return _default_request(endpoint)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request file must contain one JSON object")
    return value


async def _admin_json(
    client: httpx.AsyncClient,
    path: str,
    admin_key: str,
) -> dict[str, Any]:
    response = await client.get(
        path,
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} returned a non-object response")
    return value


def _usage_counts(payload: dict[str, Any], model: str) -> dict[str, int]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return {}
    counts: Counter[str] = Counter()
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("public_model") == model
            and row.get("node_id") is not None
        ):
            counts[str(row["node_id"])] += int(row.get("request_count") or 0)
    return dict(counts)


def _require_drained_baseline(
    status: dict[str, Any],
    *,
    eligible_nodes: set[str],
    require_usage: bool,
) -> None:
    """Fail closed unless an exact route/token delta can be measured.

    The acceptance probe deliberately compares aggregate counters rather than
    reading prompts, outputs, or node-local databases. Existing Fleet routes,
    node work, queues, or delivery-outbox rows could cross the baseline after
    it is captured and make that delta ambiguous, so require a quiet,
    fully-drained starting point.
    """

    problems: list[str] = []
    scheduler = status.get("scheduler")
    if not isinstance(scheduler, Mapping):
        problems.append("Fleet scheduler state is unavailable")
    else:
        if scheduler.get("active_total") != 0:
            problems.append("Fleet still has active routes")
        queues = scheduler.get("queues")
        if not isinstance(queues, Mapping) or any(
            not isinstance(queue, Mapping) or queue.get("depth") != 0
            for queue in queues.values()
        ):
            problems.append("Fleet still has queued requests")

    node_rows = status.get("nodes")
    by_node = (
        {
            str(row.get("node_id")): row
            for row in node_rows
            if isinstance(row, Mapping) and row.get("node_id") is not None
        }
        if isinstance(node_rows, list)
        else {}
    )
    for node_id in sorted(eligible_nodes):
        node = by_node.get(node_id)
        if node is None:
            problems.append(f"{node_id}: status is unavailable")
            continue
        capacity = node.get("capacity")
        admission = node.get("admission")
        if not isinstance(capacity, Mapping) or capacity.get("active") != 0:
            problems.append(f"{node_id}: node still has active requests")
        if (
            not isinstance(admission, Mapping)
            or admission.get("queue_depth") != 0
        ):
            problems.append(f"{node_id}: node still has queued requests")
        if require_usage:
            delivery = node.get("usage_delivery")
            if not isinstance(delivery, Mapping):
                problems.append(f"{node_id}: usage delivery state is unavailable")
            elif (
                delivery.get("enabled") is not True
                or delivery.get("writer_ready") is not True
                or delivery.get("outbox_pending") != 0
                or delivery.get("last_error_code") is not None
            ):
                problems.append(
                    f"{node_id}: usage delivery is not healthy and drained"
                )

    if problems:
        raise RuntimeError(
            "acceptance baseline is not drained: " + "; ".join(problems)
        )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    client_key = _secret(args.client_key_env)
    admin_key = _secret(args.admin_key_env)
    if client_key == admin_key:
        raise ValueError("client and admin credentials must be different")
    required_node_service_classes = _parse_required_node_service_classes(
        args.require_node_service_class
    )

    endpoint = args.endpoint
    if endpoint not in SUPPORTED_ENDPOINTS:
        raise ValueError(f"endpoint must be one of {sorted(SUPPORTED_ENDPOINTS)}")
    if endpoint == "/v1/images/generations" and not args.skip_usage:
        raise ValueError(
            "image requests do not emit token events; pass --skip-usage"
        )
    request_payload = _load_request(args.request_file, endpoint)
    request_payload["model"] = args.model
    if endpoint in {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/responses",
        "/v1/messages",
    }:
        request_payload["stream"] = False
    else:
        request_payload.pop("stream", None)

    timeout = httpx.Timeout(args.request_timeout, connect=10.0)
    async with httpx.AsyncClient(
        base_url=args.url.rstrip("/"),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        health = await client.get("/health")
        health.raise_for_status()

        models_response = await client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {client_key}"},
        )
        models_response.raise_for_status()
        advertised = {
            item.get("id")
            for item in models_response.json().get("data", [])
            if isinstance(item, dict)
        }
        if args.model not in advertised:
            raise RuntimeError(
                f"model {args.model!r} is not currently advertised by Fleet"
            )

        before_status = await _admin_json(client, "/fleet/api/status", admin_key)
        model_rows = [
            row
            for row in before_status.get("models", [])
            if isinstance(row, dict) and row.get("name") == args.model
        ]
        if len(model_rows) != 1:
            raise RuntimeError("Fleet status does not contain exactly one model mapping")
        eligible_nodes = {
            str(node["node_id"])
            for node in model_rows[0].get("nodes", [])
            if (
                isinstance(node, dict)
                and node.get("eligible")
                and node.get("node_id") is not None
            )
        }
        if len(eligible_nodes) < args.min_eligible_nodes:
            raise RuntimeError(
                f"only {len(eligible_nodes)} eligible nodes; "
                f"{args.min_eligible_nodes} required"
            )
        required_nodes = set(args.require_node) | set(
            required_node_service_classes
        )
        missing = required_nodes - eligible_nodes
        if missing:
            raise RuntimeError(
                f"required nodes are not online and eligible: {sorted(missing)}"
            )
        status_nodes = {
            str(node["node_id"]): node
            for node in before_status.get("nodes", [])
            if isinstance(node, dict) and node.get("node_id") is not None
        }
        _require_node_service_classes(
            status_nodes,
            required_node_service_classes,
        )
        eligible_platforms = {
            str(status_nodes[node_id]["platform"])
            for node_id in eligible_nodes
            if (
                node_id in status_nodes
                and status_nodes[node_id].get("platform") is not None
            )
        }
        required_platforms = set(args.require_platform)
        missing_platforms = required_platforms - eligible_platforms
        if missing_platforms:
            raise RuntimeError(
                "required platforms have no online eligible replica: "
                f"{sorted(missing_platforms)}"
            )
        _require_drained_baseline(
            before_status,
            eligible_nodes=eligible_nodes,
            require_usage=not args.skip_usage,
        )

        before_route_ids = {
            row.get("route_id")
            for row in before_status.get("routes", [])
            if isinstance(row, dict)
        }
        usage_before: dict[str, int] = {}
        if not args.skip_usage:
            usage_payload = await _admin_json(
                client,
                f"/fleet/api/usage?hours={args.usage_hours}",
                admin_key,
            )
            if not usage_payload.get("configured"):
                raise RuntimeError("Fleet's read-only token ledger is not configured")
            usage_before = _usage_counts(usage_payload, args.model)

        gate = asyncio.Event()

        async def issue_one() -> int:
            await gate.wait()
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {client_key}"},
                json=request_payload,
            )
            # Consume the full response so the node and gateway release their
            # stream/request permits before route metadata is inspected.
            _ = response.content
            return response.status_code

        tasks = [
            asyncio.create_task(issue_one(), name=f"fleet-acceptance-{index}")
            for index in range(args.requests)
        ]
        started_at = time.time()
        gate.set()
        statuses = await asyncio.gather(*tasks)
        failed = [status for status in statuses if not 200 <= status < 300]
        if failed:
            raise RuntimeError(f"inference probes returned HTTP statuses {failed}")

        deadline = time.monotonic() + args.metadata_timeout
        new_routes: list[dict[str, Any]] = []
        final_status = before_status
        while time.monotonic() < deadline:
            status = await _admin_json(client, "/fleet/api/status", admin_key)
            final_status = status
            new_routes = [
                row
                for row in status.get("routes", [])
                if isinstance(row, dict)
                and row.get("route_id") not in before_route_ids
                and row.get("public_model") == args.model
                and row.get("endpoint") == endpoint
                and float(row.get("started_at") or 0) >= started_at - 1
            ]
            completed = [
                row
                for row in new_routes
                if row.get("completed_at") is not None
                and isinstance(row.get("status_code"), int)
                and 200 <= row["status_code"] < 300
                and row.get("failure_code") is None
            ]
            if len(completed) >= args.requests:
                new_routes = completed
                break
            await asyncio.sleep(0.5)
        if len(new_routes) < args.requests:
            raise RuntimeError("new completed routes did not appear before the deadline")
        if len(new_routes) > args.requests:
            raise RuntimeError(
                "unexpected extra completed routes made the acceptance "
                "window inconclusive"
            )

        final_status_nodes = {
            str(node["node_id"]): node
            for node in final_status.get("nodes", [])
            if isinstance(node, dict) and node.get("node_id") is not None
        }
        _require_node_service_classes(
            final_status_nodes,
            required_node_service_classes,
        )

        routed_nodes = {str(row["node_id"]) for row in new_routes}
        if len(routed_nodes) < args.min_routed_nodes:
            raise RuntimeError(
                f"requests reached {len(routed_nodes)} node(s); "
                f"{args.min_routed_nodes} required"
            )
        unrouted_required_nodes = required_nodes - routed_nodes
        if unrouted_required_nodes:
            raise RuntimeError(
                "required nodes were eligible but did not serve a request: "
                f"{sorted(unrouted_required_nodes)}"
            )
        routed_platforms = {
            str(final_status_nodes[node_id]["platform"])
            for node_id in routed_nodes
            if (
                node_id in final_status_nodes
                and final_status_nodes[node_id].get("platform") is not None
            )
        }
        unrouted_required_platforms = required_platforms - routed_platforms
        if unrouted_required_platforms:
            raise RuntimeError(
                "required platforms were eligible but did not serve a request: "
                f"{sorted(unrouted_required_platforms)}"
            )

        usage_increment: dict[str, int] | None = None
        if not args.skip_usage:
            required_usage = Counter(str(row["node_id"]) for row in new_routes)
            usage_deadline = time.monotonic() + args.usage_timeout
            while time.monotonic() < usage_deadline:
                usage_payload = await _admin_json(
                    client,
                    f"/fleet/api/usage?hours={args.usage_hours}",
                    admin_key,
                )
                current_usage = _usage_counts(usage_payload, args.model)
                usage_increment = {
                    node_id: current_usage.get(node_id, 0)
                    - usage_before.get(node_id, 0)
                    for node_id in required_usage
                }
                if all(
                    usage_increment[node_id] == count
                    for node_id, count in required_usage.items()
                ):
                    break
                if any(
                    usage_increment[node_id] > count
                    for node_id, count in required_usage.items()
                ):
                    raise RuntimeError(
                        "unexpected extra token events made the acceptance "
                        "window inconclusive"
                    )
                await asyncio.sleep(2)
            if usage_increment is None or any(
                usage_increment.get(node_id, 0) != count
                for node_id, count in required_usage.items()
            ):
                raise RuntimeError(
                    "exactly one per-route token event did not reach Nyx "
                    "before the deadline"
                )

    return {
        "status": "passed",
        "model": args.model,
        "endpoint": endpoint,
        "requests": args.requests,
        "eligible_nodes": sorted(str(value) for value in eligible_nodes),
        "eligible_platforms": sorted(eligible_platforms),
        "routed_nodes": sorted(routed_nodes),
        "routed_platforms": sorted(routed_platforms),
        "routed_service_classes": sorted(
            {
                str(final_status_nodes[node_id]["service_class"])
                for node_id in routed_nodes
                if (
                    node_id in final_status_nodes
                    and final_status_nodes[node_id].get("service_class") is not None
                )
            }
        ),
        "required_node_service_classes": dict(
            sorted(required_node_service_classes.items())
        ),
        "http_statuses": statuses,
        "usage_increment": usage_increment,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded multi-node Mnemosyne Fleet acceptance probe."
    )
    parser.add_argument("--url", default="http://127.0.0.1:17400")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--endpoint",
        default="/v1/chat/completions",
        choices=sorted(SUPPORTED_ENDPOINTS),
    )
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--requests", type=int, default=2)
    parser.add_argument("--min-eligible-nodes", type=int, default=2)
    parser.add_argument("--min-routed-nodes", type=int, default=2)
    parser.add_argument("--require-node", action="append", default=[])
    parser.add_argument(
        "--require-node-service-class",
        action="append",
        default=[],
        metavar="NODE=CLASS",
        help=(
            "require this exact eligible and routed node to retain its "
            "primary, opportunistic, or overflow service class"
        ),
    )
    parser.add_argument(
        "--require-platform",
        action="append",
        choices=("cuda", "macos"),
        default=[],
        help="require an online eligible replica on this platform",
    )
    parser.add_argument(
        "--client-key-env",
        default="MNEMOSYNE_FLEET_CLIENT_KEY",
    )
    parser.add_argument(
        "--admin-key-env",
        default="MNEMOSYNE_FLEET_ADMIN_KEY",
    )
    parser.add_argument("--request-timeout", type=float, default=900)
    parser.add_argument("--metadata-timeout", type=float, default=15)
    parser.add_argument("--usage-timeout", type=float, default=120)
    parser.add_argument("--usage-hours", type=int, default=1)
    parser.add_argument("--skip-usage", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.requests <= 100:
        print("--requests must be between 1 and 100", file=sys.stderr)
        return 2
    if args.min_eligible_nodes < 1:
        print("--min-eligible-nodes must be positive", file=sys.stderr)
        return 2
    if not 1 <= args.min_routed_nodes <= args.requests:
        print("--min-routed-nodes must be between 1 and --requests", file=sys.stderr)
        return 2
    if any(
        value <= 0
        for value in (
            args.request_timeout,
            args.metadata_timeout,
            args.usage_timeout,
            args.usage_hours,
        )
    ):
        print("timeouts and --usage-hours must be positive", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_run(args))
    except (ValueError, RuntimeError, httpx.HTTPError, OSError) as exc:
        print(f"Fleet acceptance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

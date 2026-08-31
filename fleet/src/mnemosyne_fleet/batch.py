from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from .config import BatchConfig
from .proxy import FleetProxy


class BatchAPIError(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class _ResultTooLarge(RuntimeError):
    pass


@dataclass(slots=True)
class BatchItem:
    index: int
    custom_id: str
    url: str
    capability: str
    body: dict[str, Any] = field(repr=False)
    routing_headers: dict[str, str] = field(repr=False)
    state: str = "pending"
    started_at: float | None = None
    completed_at: float | None = None
    status_code: int | None = None
    response_body: object | None = field(default=None, repr=False)
    retained_bytes: int = field(default=0, repr=False)
    error_code: str | None = None


@dataclass(slots=True)
class BatchJob:
    batch_id: str
    created_at: float
    max_concurrency: int
    items: list[BatchItem] = field(repr=False)
    state: str = "queued"
    started_at: float | None = None
    completed_at: float | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def _contains_exception(error: BaseException, expected: type[BaseException]) -> bool:
    if isinstance(error, expected):
        return True
    nested = getattr(error, "exceptions", ())
    return any(_contains_exception(item, expected) for item in nested)


class BatchManager:
    """Bounded, ephemeral batching over the ordinary Fleet proxy path.

    Request and response content exists only in this process and is removed
    after the configured retention window. SQLite receives only the same
    per-route metadata it receives for interactive requests.
    """

    def __init__(
        self,
        *,
        proxy: FleetProxy,
        routes: dict[str, str],
        config: BatchConfig,
        max_submission_bytes: int,
    ) -> None:
        self._proxy = proxy
        self._routes = dict(routes)
        self._config = config
        self._max_submission_bytes = max_submission_bytes
        self._jobs: dict[str, BatchJob] = {}
        self._lock = asyncio.Lock()
        self._global_slots = asyncio.Semaphore(config.max_concurrency)
        self._retained_result_bytes = 0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def submit(self, request: Request) -> dict[str, object]:
        if not self.enabled:
            raise BatchAPIError(404, "batch_disabled")
        payload = await self._read_payload(request)
        job = self._validate_job(payload)
        async with self._lock:
            self._prune_locked()
            self._evict_terminal_locked()
            active = sum(
                row.state in {"queued", "running"}
                for row in self._jobs.values()
            )
            if active >= self._config.max_active_jobs:
                raise BatchAPIError(429, "batch_active_limit_reached")
            self._jobs[job.batch_id] = job
            job.task = asyncio.create_task(
                self._run(job),
                name=f"fleet-batch-{job.batch_id}",
            )
        return self.status_payload(job)

    async def status(self, batch_id: str) -> dict[str, object]:
        job = await self._job(batch_id)
        return self.status_payload(job)

    async def results(self, batch_id: str) -> dict[str, object]:
        job = await self._job(batch_id)
        rows: list[dict[str, object]] = []
        for item in sorted(job.items, key=lambda row: row.index):
            row: dict[str, object] = {
                "custom_id": item.custom_id,
                "state": item.state,
            }
            if item.status_code is not None:
                row["response"] = {
                    "status_code": item.status_code,
                    "body": item.response_body,
                }
            if item.error_code is not None:
                row["error"] = {"code": item.error_code}
            rows.append(row)
        return {
            **self.status_payload(job),
            "data": rows,
        }

    async def cancel(self, batch_id: str) -> dict[str, object]:
        job = await self._job(batch_id)
        task: asyncio.Task[None] | None = None
        async with self._lock:
            if job.state not in {"completed", "cancelled"}:
                job.cancel_requested = True
                task = job.task
                if task is not None:
                    task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self.status_payload(job)

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [
                row.task
                for row in self._jobs.values()
                if row.task is not None and not row.task.done()
            ]
            for row in self._jobs.values():
                if row.state in {"queued", "running"}:
                    row.cancel_requested = True
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def summary(self) -> dict[str, object]:
        self._prune_locked()
        counts = {
            state: sum(row.state == state for row in self._jobs.values())
            for state in ("queued", "running", "completed", "cancelled")
        }
        return {
            "enabled": self.enabled,
            "ephemeral": True,
            "retention_seconds": self._config.retention_seconds,
            "jobs": counts,
            "active_items": sum(
                item.state == "running"
                for job in self._jobs.values()
                for item in job.items
            ),
            "retained_result_bytes": self._retained_result_bytes,
            "max_retained_result_bytes": self._config.max_retained_result_bytes,
        }

    async def _job(self, batch_id: str) -> BatchJob:
        try:
            if str(uuid.UUID(batch_id)) != batch_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise BatchAPIError(404, "batch_not_found") from None
        async with self._lock:
            self._prune_locked()
            job = self._jobs.get(batch_id)
        if job is None:
            raise BatchAPIError(404, "batch_not_found")
        return job

    async def _read_payload(self, request: Request) -> object:
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise BatchAPIError(415, "batch_content_encoding_unsupported")
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise BatchAPIError(415, "batch_json_required")
        output = bytearray()
        async for chunk in request.stream():
            output.extend(chunk)
            if len(output) > self._max_submission_bytes:
                raise BatchAPIError(413, "batch_request_too_large")
        try:
            return json.loads(
                bytes(output),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                parse_float=self._finite_float,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise BatchAPIError(400, "batch_invalid_request") from None

    @staticmethod
    def _finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed

    def _validate_job(self, payload: object) -> BatchJob:
        if not isinstance(payload, dict) or set(payload) - {
            "requests",
            "max_concurrency",
            "routing",
        }:
            raise BatchAPIError(400, "batch_invalid_request")
        requests = payload.get("requests")
        if (
            not isinstance(requests, list)
            or not requests
            or len(requests) > self._config.max_requests_per_job
        ):
            raise BatchAPIError(400, "batch_invalid_request")
        requested_concurrency = payload.get(
            "max_concurrency",
            self._config.max_concurrency,
        )
        if (
            isinstance(requested_concurrency, bool)
            or not isinstance(requested_concurrency, int)
            or not 1 <= requested_concurrency <= self._config.max_concurrency
        ):
            raise BatchAPIError(400, "batch_invalid_request")
        common_routing = self._routing_headers(payload.get("routing", {}))
        items: list[BatchItem] = []
        custom_ids: set[str] = set()
        for index, value in enumerate(requests):
            if not isinstance(value, dict) or set(value) - {
                "custom_id",
                "method",
                "url",
                "body",
                "routing",
            }:
                raise BatchAPIError(400, "batch_invalid_request")
            custom_id = value.get("custom_id")
            url = value.get("url")
            body = value.get("body")
            if (
                not isinstance(custom_id, str)
                or not custom_id
                or len(custom_id) > 128
                or any(ord(character) < 32 for character in custom_id)
                or custom_id in custom_ids
                or value.get("method", "POST") != "POST"
                or url not in self._routes
                or not isinstance(body, dict)
                or body.get("stream") is True
            ):
                raise BatchAPIError(400, "batch_invalid_request")
            custom_ids.add(custom_id)
            routing = dict(common_routing)
            routing.update(self._routing_headers(value.get("routing", {})))
            items.append(
                BatchItem(
                    index=index,
                    custom_id=custom_id,
                    url=url,
                    capability=self._routes[url],
                    body=body,
                    routing_headers=routing,
                )
            )
        return BatchJob(
            batch_id=str(uuid.uuid4()),
            created_at=time.time(),
            max_concurrency=requested_concurrency,
            items=items,
        )

    @staticmethod
    def _routing_headers(value: object) -> dict[str, str]:
        if not isinstance(value, dict) or set(value) - {
            "affinity",
            "fallback",
            "max_wait_ms",
        }:
            raise BatchAPIError(400, "batch_invalid_request")
        output = {"x-mnemosyne-priority": "batch"}
        affinity = value.get("affinity")
        if affinity is not None:
            if not isinstance(affinity, str):
                raise BatchAPIError(400, "batch_invalid_request")
            output["x-mnemosyne-affinity"] = affinity
        fallback = value.get("fallback")
        if fallback is not None:
            if fallback not in {"allow", "none"}:
                raise BatchAPIError(400, "batch_invalid_request")
            output["x-mnemosyne-fallback"] = fallback
        max_wait = value.get("max_wait_ms")
        if max_wait is not None:
            if (
                isinstance(max_wait, bool)
                or not isinstance(max_wait, int)
                or not 0 <= max_wait <= 86_400_000
            ):
                raise BatchAPIError(400, "batch_invalid_request")
            output["x-mnemosyne-max-wait-ms"] = str(max_wait)
        return output

    async def _run(self, job: BatchJob) -> None:
        job.state = "running"
        job.started_at = time.time()
        # Stable grouping lets compatible requests reach the same warm-first
        # scheduler together; the low-priority lane still yields to normal and
        # interactive callers at Fleet admission.
        pending = sorted(
            job.items,
            key=lambda item: (
                item.url,
                str(item.body.get("model", "")),
                item.routing_headers.get("x-mnemosyne-affinity", ""),
                item.index,
            ),
        )
        queue: asyncio.Queue[BatchItem] = asyncio.Queue()
        for item in pending:
            queue.put_nowait(item)

        async def worker() -> None:
            while not job.cancel_requested:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    async with self._global_slots:
                        if job.cancel_requested:
                            return
                        await self._execute_item(item)
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker(), name=f"fleet-batch-worker-{job.batch_id}")
            for _ in range(min(job.max_concurrency, len(job.items)))
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            now = time.time()
            if job.cancel_requested:
                for item in job.items:
                    if item.state in {"pending", "running"}:
                        item.state = "cancelled"
                        item.completed_at = now
                job.state = "cancelled"
            else:
                job.state = "completed"
            job.completed_at = now
            # Results do not require the original request. Release prompt and
            # routing content immediately instead of retaining it for the
            # result/status window.
            for item in job.items:
                item.body = {}
                item.routing_headers = {}

    async def _execute_item(self, item: BatchItem) -> None:
        item.state = "running"
        item.started_at = time.time()
        raw = json.dumps(
            item.body,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        sent = False

        async def request_receive() -> dict[str, object]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}

        header_pairs = [(b"content-type", b"application/json")]
        header_pairs.extend(
            (key.encode("ascii"), value.encode("utf-8"))
            for key, value in item.routing_headers.items()
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": item.url,
            "raw_path": item.url.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": header_pairs,
            "client": ("batch", 0),
            "server": ("fleet", 80),
        }
        request = Request(scope, request_receive)
        response = await self._proxy.handle(request, capability=item.capability)
        response_status = response.status_code
        output = bytearray()
        response_done = asyncio.Event()

        async def response_receive() -> dict[str, object]:
            await response_done.wait()
            return {"type": "http.disconnect"}

        async def response_send(message: dict[str, object]) -> None:
            if message["type"] != "http.response.body":
                return
            body = message.get("body", b"")
            if isinstance(body, bytes):
                output.extend(body)
            if len(output) > self._config.max_result_bytes_per_item:
                raise _ResultTooLarge

        try:
            await response(scope, response_receive, response_send)
        except BaseException as exc:
            if _contains_exception(exc, _ResultTooLarge):
                item.state = "failed"
                item.error_code = "batch_result_too_large"
                return
            if isinstance(exc, asyncio.CancelledError) or _contains_exception(
                exc,
                asyncio.CancelledError,
            ):
                raise asyncio.CancelledError from None
            item.state = "failed"
            item.error_code = "batch_response_failed"
            return
        finally:
            response_done.set()
            item.completed_at = time.time()
        item.status_code = response_status
        try:
            parsed_response = json.loads(
                bytes(output),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            item.state = "failed"
            item.error_code = "batch_non_json_response"
            return
        retained_bytes = len(output)
        if (
            self._retained_result_bytes + retained_bytes
            > self._config.max_retained_result_bytes
        ):
            item.state = "failed"
            item.error_code = "batch_result_capacity_reached"
            return
        self._retained_result_bytes += retained_bytes
        item.retained_bytes = retained_bytes
        item.response_body = parsed_response
        item.state = "completed" if response_status < 400 else "failed"

    def status_payload(self, job: BatchJob) -> dict[str, object]:
        counts = {
            state: sum(item.state == state for item in job.items)
            for state in ("pending", "running", "completed", "failed", "cancelled")
        }
        return {
            "id": job.batch_id,
            "object": "fleet.batch",
            "state": job.state,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "expires_at": (
                None
                if job.completed_at is None
                else job.completed_at + self._config.retention_seconds
            ),
            "request_counts": counts,
            "max_concurrency": job.max_concurrency,
            "ephemeral": True,
        }

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [
            batch_id
            for batch_id, job in self._jobs.items()
            if job.completed_at is not None
            and now - job.completed_at >= self._config.retention_seconds
        ]
        for batch_id in expired:
            self._drop_job_locked(batch_id)

    def _evict_terminal_locked(self) -> None:
        maximum_stored_jobs = self._config.max_active_jobs * 4
        if len(self._jobs) < maximum_stored_jobs:
            return
        terminal = sorted(
            (
                row
                for row in self._jobs.values()
                if row.state in {"completed", "cancelled"}
            ),
            key=lambda row: row.completed_at or row.created_at,
        )
        for job in terminal:
            if len(self._jobs) < maximum_stored_jobs:
                break
            self._drop_job_locked(job.batch_id)

    def _drop_job_locked(self, batch_id: str) -> None:
        job = self._jobs.pop(batch_id, None)
        if job is None:
            return
        self._retained_result_bytes = max(
            0,
            self._retained_result_bytes
            - sum(item.retained_bytes for item in job.items),
        )

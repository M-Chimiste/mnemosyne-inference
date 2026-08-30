"""oMLX lifecycle adapter using its authoritative admin engine-pool state."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import hashlib
import math
import os
from urllib.parse import quote

import httpx

from .base import AdapterError, CapacityHint, Deadline
from .http import HttpAdapterError, HttpEngineAdapter, JsonObjectResponse
from ..config import OMLXConfig
from ..install_launch import (
    InstallLaunchError,
    OMLXInstallLaunch,
    omlx_target_launch,
)
from ..models import (
    ContextWindowHint,
    ContextWindowProfileResult,
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)


@dataclass(frozen=True, slots=True)
class OMLXLaunchContractEvidence:
    """Exact, content-free service-global settings reported by oMLX."""

    scheduler_slots: int
    memory_guard_enabled: bool


class OMLXAdapter(HttpEngineAdapter):
    _CONTEXT_BENCHMARK_TARGETS = (
        16_384,
        32_768,
        65_536,
        131_072,
        262_144,
        524_288,
    )

    def __init__(
        self,
        config: OMLXConfig,
        *,
        client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        super().__init__(
            engine=EngineName.OMLX,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            request_timeout_seconds=config.request_timeout_seconds,
            client=client,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.admin_session_env = config.admin_session_env
        self._capacity_hint: CapacityHint | None = None
        self._capacity_diagnostic: str | None = None
        self._capacity_observation_lock = asyncio.Lock()

    def capacity_hint(self, target: ResolvedTarget) -> CapacityHint | None:
        del target
        return self._capacity_hint

    def _set_authoritative_capacity(self, slots: int) -> None:
        self._capacity_hint = CapacityHint(
            limit=slots,
            source="omlx-admin-settings",
            confidence="authoritative",
        )
        self._capacity_diagnostic = None

    def _clear_capacity(self, diagnostic: str) -> None:
        self._capacity_hint = None
        self._capacity_diagnostic = diagnostic

    async def runtime_fingerprint(self, *, deadline: Deadline) -> str | None:
        """Fingerprint the authoritative running oMLX version when exposed."""

        for endpoint in ("/health", "/api/status"):
            remaining = min(deadline.remaining(), self.request_timeout_seconds)
            if remaining <= 0:
                return None
            try:
                response = await self._client.get(
                    f"{self.base_url}{endpoint}",
                    headers=self._bearer_headers(),
                    timeout=remaining,
                )
                if response.status_code != 200:
                    continue
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            version: str | None = None
            for key in ("version", "app_version", "server_version"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    version = value
                    break
            if version is not None:
                material = f"{self.engine.value}\0{self.base_url}\0{version}"
                return hashlib.sha256(material.encode("utf-8")).hexdigest()
        return None

    async def context_window(
        self,
        target: ResolvedTarget,
        *,
        deadline: Deadline,
    ) -> ContextWindowHint:
        """Read oMLX's effective window rather than trusting its 32K fallback."""

        payload = await self._request_json(
            "GET",
            "/v1/models/status",
            operation="inspect context window",
            deadline=deadline,
            headers=self._bearer_headers(),
        )
        models = payload.get("models")
        if not isinstance(models, list):
            raise AdapterError(
                self.engine,
                "inspect context window",
                "oMLX model status omitted the models array",
            )
        ids = {target.key.canonical_model_id, target.wire_model}
        effective: int | None = None
        for item in models:
            if not isinstance(item, dict):
                continue
            if item.get("id") not in ids and item.get("source_model_id") not in ids:
                continue
            value = item.get("max_context_window")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                effective = value
                break

        native: int | None = target.native_context_length
        try:
            admin = await self._request_json(
                "GET",
                "/admin/api/models",
                operation="inspect native context window",
                deadline=deadline,
                headers=self._admin_headers(),
            )
            entries = admin.get("models")
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict) or item.get("id") not in ids:
                        continue
                    value = item.get("model_context_length")
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        native = value
                    break
        except AdapterError:
            # The bearer-authenticated status route is the serving contract.
            # Native metadata is useful UI detail, not a reason to hide an
            # otherwise authoritative effective value.
            pass

        if effective is None:
            raise AdapterError(
                self.engine,
                "inspect context window",
                f"oMLX did not report an effective context for '{target.key.canonical_model_id}'",
            )
        return ContextWindowHint(
            effective_tokens=effective,
            native_tokens=native,
            source="omlx-model-status",
            confidence="authoritative",
        )

    async def set_context_window(
        self,
        target: ResolvedTarget,
        tokens: int,
        *,
        deadline: Deadline,
    ) -> ContextWindowHint:
        """Persist an explicit oMLX per-model window through its admin API."""

        model_path = quote(target.key.canonical_model_id, safe="")
        response = await self._request_json_response(
            "PUT",
            f"/admin/api/models/{model_path}/settings",
            operation="set context window",
            deadline=deadline,
            headers=self._admin_headers(),
            json_body={"max_context_window": tokens},
            ok_statuses=(200,),
        )
        if response.payload.get("success") is not True:
            raise AdapterError(
                self.engine,
                "set context window",
                "oMLX did not confirm the context setting",
            )
        observed = await self.context_window(target, deadline=deadline)
        if observed.effective_tokens != tokens:
            raise AdapterError(
                self.engine,
                "set context window",
                "oMLX did not expose the requested effective context",
            )
        return observed

    async def profile_context_window(
        self,
        target: ResolvedTarget,
        requested_tokens: int,
        *,
        deadline: Deadline,
    ) -> ContextWindowProfileResult | None:
        """Delegate to oMLX's memory-guard-aware native context benchmark."""

        supported = [
            value
            for value in self._CONTEXT_BENCHMARK_TARGETS
            if value <= requested_tokens
        ]
        if not supported:
            return None
        benchmark_target = max(supported)
        try:
            started = await self._request_json(
                "POST",
                "/admin/api/bench/context/start",
                operation="start native context profile",
                deadline=deadline,
                headers=self._admin_headers(),
                json_body={
                    "model_id": target.key.canonical_model_id,
                    "target_tokens": benchmark_target,
                },
            )
        except HttpAdapterError as exc:
            # Older official releases do not expose this additive endpoint;
            # the portable coordinator-owned suite remains compatible.
            if exc.status_code == 404:
                return None
            raise
        bench_id = started.get("bench_id")
        if not isinstance(bench_id, str) or not bench_id:
            raise AdapterError(
                self.engine,
                "start native context profile",
                "oMLX omitted the benchmark identifier",
            )

        terminal = False
        try:
            while deadline.remaining() > 0:
                payload = await self._request_json(
                    "GET",
                    f"/admin/api/bench/context/{quote(bench_id, safe='')}/results",
                    operation="poll native context profile",
                    deadline=deadline,
                    headers=self._admin_headers(),
                )
                status = payload.get("status")
                if status == "completed":
                    terminal = True
                    result = payload.get("result")
                    if not isinstance(result, dict) or result.get("applied") is not True:
                        raise AdapterError(
                            self.engine,
                            "native context profile",
                            "oMLX completed without applying a verified context",
                        )
                    applied = result.get("applied_tokens")
                    prompt = result.get("verified_prompt_tokens")
                    if (
                        not isinstance(applied, int)
                        or isinstance(applied, bool)
                        or applied < 1
                        or applied > benchmark_target
                        or not isinstance(prompt, int)
                        or isinstance(prompt, bool)
                        or prompt < 1
                    ):
                        raise AdapterError(
                            self.engine,
                            "native context profile",
                            "oMLX returned invalid fixed token evidence",
                        )
                    return ContextWindowProfileResult(
                        requested_tokens=requested_tokens,
                        verified_tokens=applied,
                        prompt_tokens=prompt,
                        source="omlx-native-context-benchmark",
                    )
                if status in {"error", "cancelled"}:
                    terminal = True
                    raise AdapterError(
                        self.engine,
                        "native context profile",
                        "oMLX could not complete its context benchmark",
                    )
                await asyncio.sleep(min(self.poll_interval_seconds, deadline.remaining()))
            raise AdapterError(
                self.engine,
                "native context profile",
                "deadline expired",
                retryable=True,
            )
        finally:
            if not terminal:
                with contextlib.suppress(Exception):
                    await self._request_json(
                        "POST",
                        f"/admin/api/bench/context/{quote(bench_id, safe='')}/cancel",
                        operation="cancel native context profile",
                        deadline=Deadline.after(5),
                        headers=self._admin_headers(),
                    )

    @property
    def capacity_diagnostic(self) -> str | None:
        return self._capacity_diagnostic

    async def cache_health(self, *, deadline: Deadline) -> dict[str, object]:
        """Return a bounded, content-free view of oMLX's own cache metrics."""

        payload = await self._request_json(
            "GET",
            "/admin/api/stats?scope=alltime",
            operation="inspect cache health",
            deadline=deadline,
            headers=self._admin_headers(),
        )
        runtime_cache = payload.get("runtime_cache")
        if not isinstance(runtime_cache, dict):
            raise AdapterError(
                self.engine,
                "inspect cache health",
                "oMLX stats omitted runtime_cache observability",
            )

        def nonnegative_int(value: object) -> int:
            return (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else 0
            )

        total_requests = nonnegative_int(payload.get("total_requests"))
        cached_tokens = nonnegative_int(payload.get("total_cached_tokens"))
        total_size_bytes = nonnegative_int(runtime_cache.get("total_size_bytes"))
        total_num_files = nonnegative_int(runtime_cache.get("total_num_files"))
        disk_max_bytes = nonnegative_int(runtime_cache.get("disk_max_bytes"))
        hot_cache_size_bytes = nonnegative_int(
            runtime_cache.get("hot_cache_size_bytes")
        )
        hot_cache_max_bytes = nonnegative_int(
            runtime_cache.get("hot_cache_max_bytes")
        )
        cache_efficiency = payload.get("cache_efficiency")
        if not isinstance(cache_efficiency, (int, float)) or isinstance(
            cache_efficiency, bool
        ) or not math.isfinite(cache_efficiency):
            cache_efficiency = None

        # A large persistent cache with a meaningful request history and no
        # observed reuse is worth surfacing. It is only a recommendation:
        # different prompts can legitimately produce no prefix hits.
        reset_recommended = (
            total_size_bytes >= 8 * 1024**3
            and total_requests >= 10
            and cached_tokens == 0
        )
        return {
            "available": True,
            "total_requests": total_requests,
            "total_cached_tokens": cached_tokens,
            "cache_efficiency": float(cache_efficiency)
            if cache_efficiency is not None
            else None,
            "ssd_file_count": total_num_files,
            "ssd_size_bytes": total_size_bytes,
            "ssd_limit_bytes": disk_max_bytes,
            "hot_size_bytes": hot_cache_size_bytes,
            "hot_limit_bytes": hot_cache_max_bytes,
            "reset_recommended": reset_recommended,
            "diagnostic": (
                "The persistent oMLX cache is large but has recorded no prefix-cache reuse; reset it if warm requests remain slow."
                if reset_recommended
                else None
            ),
        }

    async def clear_ssd_cache(self, *, deadline: Deadline) -> int:
        """Clear oMLX's SSD KV cache through its official admin API."""

        payload = await self._request_json(
            "POST",
            "/admin/api/ssd-cache/clear",
            operation="reset SSD cache",
            deadline=deadline,
            headers=self._admin_headers(),
        )
        deleted = payload.get("total_deleted")
        if (
            payload.get("status") != "ok"
            or not isinstance(deleted, int)
            or isinstance(deleted, bool)
            or deleted < 0
        ):
            raise AdapterError(
                self.engine,
                "reset SSD cache",
                "oMLX returned an invalid cache-reset result",
            )
        return deleted

    def _admin_headers(self) -> dict[str, str]:
        headers = self._bearer_headers()
        session = os.environ.get(self.admin_session_env, "").strip()
        if session:
            headers["Cookie"] = f"omlx_admin_session={session}"
        return headers

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        try:
            payload = await self._request_json(
                "GET",
                "/admin/api/models",
                operation="inspect",
                deadline=deadline,
                headers=self._admin_headers(),
            )
        except AdapterError as exc:
            state, authoritative = self._failure_state(exc)
            diagnostic = exc.detail
            if state == ServiceState.UNAUTHORIZED:
                session = os.environ.get(self.admin_session_env, "").strip()
                if self._api_key() and not session:
                    diagnostic = (
                        "oMLX admin inventory and unload require an admin session; "
                        "a bearer API key alone only authorizes the load route"
                    )
                elif session:
                    diagnostic = "configured oMLX admin session was rejected or expired"
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=authoritative,
                service_state=state,
                diagnostic=diagnostic,
            )
        models = payload.get("models")
        if not isinstance(models, list):
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="oMLX admin response did not contain a models array",
            )
        residents: list[ResidentInstance] = []
        seen_ids: set[str] = set()
        transitioning: list[str] = []
        for model in models:
            if not isinstance(model, dict):
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic="oMLX model entry is not an object",
                )
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic="oMLX model entry has no non-empty string id",
                )
            if model_id in seen_ids:
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic=f"oMLX model inventory contains duplicate id: {model_id}",
                )
            seen_ids.add(model_id)
            if "loaded" not in model or not isinstance(model["loaded"], bool):
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic=f"oMLX model '{model_id}' has no boolean loaded state",
                )
            is_loading = model.get("is_loading", False)
            if not isinstance(is_loading, bool):
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic=f"oMLX model '{model_id}' has no boolean is_loading state",
                )
            virtual = model.get("virtual", False)
            if not isinstance(virtual, bool):
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=False,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic=f"oMLX model '{model_id}' has no boolean virtual state",
                )
            if is_loading:
                transitioning.append(model_id)
            # Built-in virtual utilities (for example markitdown) do not own a
            # Metal model allocation and must not block primary-model swaps.
            if model["loaded"] is True and not virtual:
                residents.append(
                    ResidentInstance(
                        engine=self.engine,
                        canonical_model_id=model_id,
                        instance_id=model_id,
                        raw=model,
                    )
                )
        if transitioning:
            return EngineSnapshot(
                engine=self.engine,
                residents=tuple(residents),
                authoritative=False,
                service_state=ServiceState.READY,
                diagnostic=(
                    "oMLX model inventory is transitioning: "
                    + ", ".join(sorted(transitioning))
                ),
            )
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(residents),
            authoritative=True,
            service_state=ServiceState.READY,
        )

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        snapshot = await self.inspect(deadline=deadline)
        if not snapshot.authoritative or snapshot.service_state != ServiceState.READY:
            self._capacity_hint = None
            return snapshot
        async with self._capacity_observation_lock:
            try:
                payload = await self._request_json(
                    "GET",
                    "/admin/api/global-settings",
                    operation="inspect scheduler capacity",
                    deadline=deadline,
                    headers=self._admin_headers(),
                )
                scheduler = payload.get("scheduler")
                value = (
                    scheduler.get("max_concurrent_requests")
                    if isinstance(scheduler, dict)
                    else None
                )
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 1 <= value <= 1024
                ):
                    raise ValueError(
                        "oMLX global settings omitted a valid "
                        "scheduler.max_concurrent_requests"
                    )
                self._set_authoritative_capacity(value)
            except (AdapterError, ValueError) as exc:
                # Scheduler discovery is an optimization contract. Inventory and
                # unload authority remain valid, so keep the engine callable at a
                # conservative single request and expose the reason in diagnostics.
                self._clear_capacity(str(exc))
        return snapshot

    async def inspect_launch_contract(
        self,
        *,
        deadline: Deadline,
    ) -> OMLXLaunchContractEvidence:
        """Read the two official globals used by signed install recipes.

        This is deliberately GET-only. ``max_concurrent_requests`` is a
        service-global, restart-required oMLX setting, and the prefill memory
        guard is also global. Mnemosyne may prove them but must never change
        either as a side effect of installing or loading one model.
        """

        async with self._capacity_observation_lock:
            try:
                payload = await self._request_json(
                    "GET",
                    "/admin/api/global-settings",
                    operation="inspect signed launch contract",
                    deadline=deadline,
                    headers=self._admin_headers(),
                )
                scheduler = payload.get("scheduler")
                memory = payload.get("memory")
                scheduler_slots = (
                    scheduler.get("max_concurrent_requests")
                    if isinstance(scheduler, dict)
                    else None
                )
                memory_guard_enabled = (
                    memory.get("prefill_memory_guard")
                    if isinstance(memory, dict)
                    else None
                )
                if (
                    not isinstance(scheduler_slots, int)
                    or isinstance(scheduler_slots, bool)
                    or not 1 <= scheduler_slots <= 1024
                    or not isinstance(memory_guard_enabled, bool)
                ):
                    raise AdapterError(
                        self.engine,
                        "inspect signed launch contract",
                        "oMLX global settings omitted an exact scheduler or "
                        "memory-guard state",
                    )
            except AdapterError as exc:
                self._clear_capacity(exc.detail)
                raise
            self._set_authoritative_capacity(scheduler_slots)
        return OMLXLaunchContractEvidence(
            scheduler_slots=scheduler_slots,
            memory_guard_enabled=memory_guard_enabled,
        )

    async def require_launch_contract(
        self,
        contract: OMLXInstallLaunch,
        *,
        deadline: Deadline,
    ) -> OMLXLaunchContractEvidence:
        """Fail closed unless the running external service already matches."""

        if not isinstance(contract, OMLXInstallLaunch):
            self._clear_capacity("signed oMLX launch contract has an invalid type")
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "signed oMLX launch contract has an invalid type",
            )
        evidence = await self.inspect_launch_contract(deadline=deadline)
        if evidence.scheduler_slots != contract.scheduler_slots:
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "oMLX scheduler capacity does not match the signed model contract",
            )
        if contract.memory_guard == "required" and not evidence.memory_guard_enabled:
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "oMLX prefill memory guard is disabled but the signed model contract requires it",
            )
        return evidence

    async def register_model_directories(
        self,
        directories: list[str],
        *,
        deadline: Deadline,
    ) -> None:
        """Merge roots into oMLX, rescan, and authoritatively restore emptiness.

        oMLX's official ``/admin/api/reload`` implementation unloads the
        current pool, rediscovers models, and then preloads every pinned model
        before returning. A directory update uses the related global-settings
        path, which also rebuilds the pool. This method runs only inside the
        coordinator's all-engines-empty maintenance barrier, so any residents
        created by either operation must be removed through oMLX's own admin
        unload API before the barrier may reopen admission.
        """

        payload = await self._request_json(
            "GET",
            "/admin/api/global-settings",
            operation="read model directories",
            deadline=deadline,
            headers=self._admin_headers(),
        )
        model_settings = payload.get("model")
        if not isinstance(model_settings, dict):
            raise AdapterError(
                self.engine,
                "read model directories",
                "oMLX global settings omitted the model section",
            )
        current = model_settings.get("effective_model_dirs") or model_settings.get("model_dirs")
        if not isinstance(current, list) or not all(isinstance(item, str) for item in current):
            raise AdapterError(
                self.engine,
                "read model directories",
                "oMLX global settings omitted model directories",
            )
        merged = list(dict.fromkeys([*current, *directories]))
        mutation_error: Exception | None = None
        try:
            if merged != current:
                response = await self._request_json_response(
                    "POST",
                    "/admin/api/global-settings",
                    operation="register model directories",
                    deadline=deadline,
                    headers=self._admin_headers(),
                    json_body={"model_dirs": merged},
                    ok_statuses=(200,),
                )
            else:
                response = await self._request_json_response(
                    "POST",
                    "/admin/api/reload",
                    operation="rescan model directories",
                    deadline=deadline,
                    headers=self._admin_headers(),
                    ok_statuses=(200,),
                )
            status = response.payload.get("status")
            if status not in {None, "ok", "success"}:
                raise AdapterError(
                    self.engine,
                    "register model directories",
                    f"oMLX returned unsupported status {status!r}",
                )
        except Exception as exc:
            # A timed-out or malformed mutation response has an ambiguous
            # outcome. Still inspect and clean through the authoritative admin
            # inventory; never infer that the pre-maintenance empty state held.
            mutation_error = exc

        cleanup_error: Exception | None = None
        try:
            await self.unload_all(deadline=deadline)
        except Exception as exc:
            cleanup_error = exc

        if cleanup_error is not None:
            detail = (
                "could not authoritatively restore empty residency after "
                f"the model-directory refresh: {cleanup_error}"
            )
            if mutation_error is not None:
                detail = f"refresh failed ({mutation_error}); {detail}"
            raise AdapterError(
                self.engine,
                "register model directories",
                detail,
                retryable=(
                    getattr(mutation_error, "retryable", False)
                    or getattr(cleanup_error, "retryable", False)
                ),
            ) from cleanup_error
        if mutation_error is not None:
            raise mutation_error

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        if target.key.engine != self.engine:
            raise AdapterError(self.engine, "load", "target belongs to another engine")
        try:
            signed_launch = omlx_target_launch(target.load_options)
        except InstallLaunchError as exc:
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "managed target contains an invalid signed oMLX launch contract",
            ) from exc
        if signed_launch is not None:
            # Keep this check in the adapter so coordinator-owned benchmark,
            # profiling, and JIT paths cannot bypass the runtime's public
            # request preflight.
            await self.require_launch_contract(signed_launch, deadline=deadline)
        requested_context = target.requested_context_length
        if requested_context is not None:
            observed = await self.context_window(target, deadline=deadline)
            if observed.effective_tokens != requested_context:
                await self.set_context_window(
                    target,
                    requested_context,
                    deadline=deadline,
                )
        before = await self.inspect(deadline=deadline)
        if not before.authoritative:
            raise AdapterError(self.engine, "load", before.diagnostic or "unknown state")
        if before.service_state == ServiceState.STOPPED:
            raise AdapterError(
                self.engine,
                "load",
                "oMLX local service is stopped",
                retryable=True,
            )
        exact = [r for r in before.residents if r.canonical_model_id == target.key.canonical_model_id]
        if len(before.residents) == 1 and len(exact) == 1:
            return self._handle(target, exact[0])
        if before.residents:
            raise AdapterError(self.engine, "load", "engine is not empty")

        model_path = quote(target.key.canonical_model_id, safe="")
        load_error: AdapterError | None = None
        try:
            response = await self._request_json_response(
                "POST",
                f"/admin/api/models/{model_path}/load",
                operation="load",
                deadline=deadline,
                headers=self._admin_headers(),
                ok_statuses=(200, 201, 202, 204),
            )
            self._validate_mutation_response(
                response,
                operation="load",
                expected_model_id=target.key.canonical_model_id,
            )
        except AdapterError as exc:
            load_error = exc
        return await self._await_loaded(
            target,
            mutation_error=load_error,
            deadline=deadline,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        if instance.engine != self.engine:
            raise AdapterError(self.engine, "unload", "instance belongs to another engine")
        before = await self.inspect(deadline=deadline)
        if not before.authoritative:
            raise AdapterError(
                self.engine,
                "unload",
                before.diagnostic or "resident state is not authoritative",
            )
        if before.service_state == ServiceState.STOPPED:
            return
        if all(
            resident.canonical_model_id != instance.canonical_model_id
            for resident in before.residents
        ):
            return

        model_path = quote(instance.canonical_model_id, safe="")
        unload_error: AdapterError | None = None
        try:
            response = await self._request_json_response(
                "POST",
                f"/admin/api/models/{model_path}/unload",
                operation="unload",
                deadline=deadline,
                headers=self._admin_headers(),
                ok_statuses=(200, 202, 204),
            )
            self._validate_mutation_response(
                response,
                operation="unload",
                expected_model_id=instance.canonical_model_id,
            )
        except AdapterError as exc:
            unload_error = exc
        await self._await_model_absent(
            instance.canonical_model_id,
            mutation_error=unload_error,
            deadline=deadline,
        )

    def _validate_mutation_response(
        self,
        response: JsonObjectResponse,
        *,
        operation: str,
        expected_model_id: str,
    ) -> None:
        payload = response.payload
        if response.status_code in (200, 201):
            allowed_statuses = {"ok"}
        else:
            allowed_statuses = (
                {"accepted", "loading", "ok"}
                if operation == "load"
                else {"accepted", "unloading", "ok"}
            )
        if response.status_code != 204:
            status = payload.get("status")
            if not isinstance(status, str) or status not in allowed_statuses:
                raise AdapterError(
                    self.engine,
                    operation,
                    "oMLX mutation response has an unsupported status",
                )
        if "success" in payload and payload["success"] is not True:
            raise AdapterError(
                self.engine,
                operation,
                "oMLX mutation response did not report success",
            )
        if response.status_code in (200, 201) and "model_id" not in payload:
            raise AdapterError(
                self.engine,
                operation,
                "oMLX mutation response did not contain model_id",
            )
        if "model_id" in payload and payload["model_id"] != expected_model_id:
            raise AdapterError(
                self.engine,
                operation,
                "oMLX mutation response identified a different model",
            )

    async def _await_loaded(
        self,
        target: ResolvedTarget,
        *,
        mutation_error: AdapterError | None,
        deadline: Deadline,
    ) -> LoadedHandle:
        last_diagnostic: str | None = None
        while True:
            snapshot = await self.inspect(deadline=deadline)
            last_diagnostic = snapshot.diagnostic
            if snapshot.authoritative:
                if snapshot.service_state == ServiceState.STOPPED:
                    if mutation_error is not None:
                        raise mutation_error
                    raise AdapterError(
                        self.engine,
                        "load",
                        "oMLX stopped before load convergence",
                        retryable=True,
                    )
                if mutation_error is not None and not mutation_error.retryable:
                    raise mutation_error
                exact = [
                    resident
                    for resident in snapshot.residents
                    if resident.canonical_model_id == target.key.canonical_model_id
                ]
                if len(snapshot.residents) == 1 and len(exact) == 1:
                    return self._handle(target, exact[0])
                if snapshot.residents:
                    raise AdapterError(
                        self.engine,
                        "load",
                        "oMLX reported an unexpected or additional resident model",
                    )

            if mutation_error is not None and not mutation_error.retryable:
                raise mutation_error
            if not await self._poll_delay(deadline):
                break

        detail = "oMLX did not confirm exactly one requested model before deadline"
        if last_diagnostic:
            detail += f"; last inspection: {last_diagnostic}"
        if mutation_error is not None:
            detail += f"; mutation: {mutation_error.detail}"
        raise AdapterError(self.engine, "load", detail, retryable=True)

    async def _await_model_absent(
        self,
        model_id: str,
        *,
        mutation_error: AdapterError | None,
        deadline: Deadline,
    ) -> None:
        last_diagnostic: str | None = None
        while True:
            snapshot = await self.inspect(deadline=deadline)
            last_diagnostic = snapshot.diagnostic
            if snapshot.authoritative:
                absent = snapshot.service_state == ServiceState.STOPPED or all(
                    resident.canonical_model_id != model_id
                    for resident in snapshot.residents
                )
                if absent:
                    race_safe_already_absent = (
                        isinstance(mutation_error, HttpAdapterError)
                        and mutation_error.status_code in (400, 404)
                    )
                    if (
                        mutation_error is not None
                        and not mutation_error.retryable
                        and not race_safe_already_absent
                    ):
                        raise mutation_error
                    return

            if mutation_error is not None and not mutation_error.retryable:
                if (
                    "HTTP 401" in mutation_error.detail
                    or "HTTP 403" in mutation_error.detail
                ):
                    raise AdapterError(
                        self.engine,
                        "unload",
                        "oMLX admin session is required for unload and was rejected",
                    ) from mutation_error
                raise mutation_error
            if not await self._poll_delay(deadline):
                break

        detail = f"oMLX did not confirm unload of model '{model_id}' before deadline"
        if last_diagnostic:
            detail += f"; last inspection: {last_diagnostic}"
        if mutation_error is not None:
            detail += f"; mutation: {mutation_error.detail}"
        raise AdapterError(self.engine, "unload", detail, retryable=True)

    def _handle(self, target: ResolvedTarget, instance: ResidentInstance) -> LoadedHandle:
        return LoadedHandle(
            target=target,
            instance=instance,
            base_url=self.base_url,
            wire_model=target.wire_model,
        )

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint not in handle.target.capabilities:
            raise AdapterError(self.engine, "route", f"endpoint {endpoint} is unsupported")
        return ProxyRoute(
            base_url=self.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
            headers=self._bearer_headers(),
            usage_dialect="anthropic" if endpoint == Endpoint.MESSAGES else "openai",
        )

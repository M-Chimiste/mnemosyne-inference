"""oMLX lifecycle adapter using its authoritative admin engine-pool state."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from .base import AdapterError, Deadline
from .http import HttpAdapterError, HttpEngineAdapter, JsonObjectResponse
from ..config import OMLXConfig
from ..models import (
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)


class OMLXAdapter(HttpEngineAdapter):
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
        return await self.inspect(deadline=deadline)

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        if target.key.engine != self.engine:
            raise AdapterError(self.engine, "load", "target belongs to another engine")
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

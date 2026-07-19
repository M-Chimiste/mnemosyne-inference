"""LM Studio lifecycle adapter using its native v1 model-management API."""

from __future__ import annotations

from typing import Any

import httpx

from .base import AdapterError, Deadline
from .http import HttpAdapterError, HttpEngineAdapter, JsonObjectResponse
from ..config import LMStudioConfig
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


class LMStudioAdapter(HttpEngineAdapter):
    def __init__(
        self,
        config: LMStudioConfig,
        *,
        client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        super().__init__(
            engine=EngineName.LMSTUDIO,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            request_timeout_seconds=config.request_timeout_seconds,
            client=client,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        try:
            payload = await self._request_json(
                "GET",
                "/api/v1/models",
                operation="inspect",
                deadline=deadline,
                headers=self._bearer_headers(),
            )
        except AdapterError as exc:
            state, authoritative = self._failure_state(exc)
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=authoritative,
                service_state=state,
                diagnostic=exc.detail,
            )

        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="native model list did not contain a models array",
            )

        residents: list[ResidentInstance] = []
        model_keys: set[str] = set()
        instance_ids: set[str] = set()
        try:
            for model in raw_models:
                if not isinstance(model, dict):
                    raise TypeError("model entry is not an object")
                model_key = model.get("key")
                if not isinstance(model_key, str) or not model_key.strip():
                    raise TypeError("model entry has no key")
                if model_key in model_keys:
                    raise TypeError(f"duplicate model key: {model_key}")
                model_keys.add(model_key)
                if "loaded_instances" not in model:
                    raise TypeError("model entry has no loaded_instances field")
                instances = model["loaded_instances"]
                if not isinstance(instances, list):
                    raise TypeError("loaded_instances is not an array")
                for instance in instances:
                    if not isinstance(instance, dict):
                        raise TypeError("loaded instance is not an object")
                    instance_id = instance.get("id")
                    if not isinstance(instance_id, str) or not instance_id.strip():
                        raise TypeError("loaded instance has no id")
                    if instance_id in instance_ids:
                        raise TypeError(f"duplicate loaded instance id: {instance_id}")
                    instance_ids.add(instance_id)
                    residents.append(
                        ResidentInstance(
                            engine=self.engine,
                            canonical_model_id=model_key,
                            instance_id=instance_id,
                            raw=instance,
                        )
                    )
        except TypeError as exc:
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic=str(exc),
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
            raise AdapterError(
                self.engine,
                "load",
                before.diagnostic or "resident state is not authoritative",
            )
        if before.service_state == ServiceState.STOPPED:
            raise AdapterError(
                self.engine,
                "load",
                "LM Studio local server is stopped",
                retryable=True,
            )
        exact = [
            resident
            for resident in before.residents
            if resident.canonical_model_id == target.key.canonical_model_id
        ]
        if len(before.residents) == 1 and len(exact) == 1:
            return self._handle(target, exact[0])
        if before.residents:
            raise AdapterError(self.engine, "load", "engine is not empty")

        allowed = {
            "context_length",
            "eval_batch_size",
            "flash_attention",
            "num_experts",
            "offload_kv_cache_to_gpu",
        }
        body: dict[str, Any] = {
            "model": target.key.canonical_model_id,
            **{key: value for key, value in target.load_options.items() if key in allowed},
        }
        load_error: AdapterError | None = None
        expected_instance_id: str | None = None
        try:
            response = await self._request_json_response(
                "POST",
                "/api/v1/models/load",
                operation="load",
                deadline=deadline,
                headers=self._bearer_headers(),
                json_body=body,
                ok_statuses=(200, 201, 202, 204),
            )
            expected_instance_id = self._validate_load_response(response)
        except AdapterError as exc:
            load_error = exc
        return await self._await_loaded(
            target,
            expected_instance_id=expected_instance_id,
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
        if instance.instance_id is None:
            raise AdapterError(self.engine, "unload", "instance ID is required")

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
            current.instance_id != instance.instance_id
            for current in before.residents
        ):
            return

        unload_error: AdapterError | None = None
        try:
            response = await self._request_json_response(
                "POST",
                "/api/v1/models/unload",
                operation="unload",
                deadline=deadline,
                headers=self._bearer_headers(),
                json_body={"instance_id": instance.instance_id},
                ok_statuses=(200, 202, 204),
            )
            self._validate_unload_response(
                response,
                expected_instance_id=instance.instance_id,
            )
        except AdapterError as exc:
            unload_error = exc
        await self._await_instance_absent(
            instance.instance_id,
            mutation_error=unload_error,
            deadline=deadline,
        )

    def _validate_load_response(
        self,
        response: JsonObjectResponse,
    ) -> str | None:
        payload = response.payload
        instance_id = payload.get("instance_id")
        if response.status_code in (200, 201) and "instance_id" not in payload:
            raise AdapterError(
                self.engine,
                "load",
                "LM Studio load response did not contain instance_id",
            )
        if instance_id is not None and (
            not isinstance(instance_id, str) or not instance_id
        ):
            raise AdapterError(
                self.engine,
                "load",
                "LM Studio load response instance_id must be a non-empty string",
            )

        if "status" in payload:
            allowed_statuses = (
                {"loaded"}
                if response.status_code in (200, 201)
                else {"accepted", "loading", "loaded"}
            )
            status = payload["status"]
            if not isinstance(status, str) or status not in allowed_statuses:
                raise AdapterError(
                    self.engine,
                    "load",
                    "LM Studio load response has an unsupported status",
                )
        if "type" in payload:
            model_type = payload["type"]
            if not isinstance(model_type, str) or model_type not in {
                "llm",
                "embedding",
            }:
                raise AdapterError(
                    self.engine,
                    "load",
                    "LM Studio load response has an unsupported model type",
                )
        if "load_time_seconds" in payload:
            load_time = payload["load_time_seconds"]
            if (
                isinstance(load_time, bool)
                or not isinstance(load_time, (int, float))
                or load_time < 0
            ):
                raise AdapterError(
                    self.engine,
                    "load",
                    "LM Studio load response has an invalid load_time_seconds",
                )
        return instance_id

    def _validate_unload_response(
        self,
        response: JsonObjectResponse,
        *,
        expected_instance_id: str,
    ) -> None:
        payload = response.payload
        if response.status_code == 200 and "instance_id" not in payload:
            raise AdapterError(
                self.engine,
                "unload",
                "LM Studio unload response did not contain instance_id",
            )
        if "instance_id" in payload and payload["instance_id"] != expected_instance_id:
            raise AdapterError(
                self.engine,
                "unload",
                "LM Studio unload response did not identify the requested instance",
            )

    async def _await_loaded(
        self,
        target: ResolvedTarget,
        *,
        expected_instance_id: str | None,
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
                        "LM Studio stopped before load convergence",
                        retryable=True,
                    )
                if mutation_error is not None and not mutation_error.retryable:
                    raise mutation_error
                exact = [
                    resident
                    for resident in snapshot.residents
                    if (
                        resident.canonical_model_id
                        == target.key.canonical_model_id
                        and (
                            expected_instance_id is None
                            or resident.instance_id == expected_instance_id
                        )
                    )
                ]
                if len(snapshot.residents) == 1 and len(exact) == 1:
                    return self._handle(target, exact[0])
                if snapshot.residents:
                    expected_detail = (
                        f" expected instance '{expected_instance_id}',"
                        if expected_instance_id is not None
                        else ""
                    )
                    raise AdapterError(
                        self.engine,
                        "load",
                        "LM Studio reported an unexpected or additional resident instance;"
                        f"{expected_detail} observed "
                        + ", ".join(
                            resident.instance_id or "<missing>"
                            for resident in snapshot.residents
                        ),
                    )

            if mutation_error is not None and not mutation_error.retryable:
                raise mutation_error
            if not await self._poll_delay(deadline):
                break

        detail = "LM Studio did not confirm exactly one requested model instance before deadline"
        if last_diagnostic:
            detail += f"; last inspection: {last_diagnostic}"
        if mutation_error is not None:
            detail += f"; mutation: {mutation_error.detail}"
        raise AdapterError(self.engine, "load", detail, retryable=True)

    async def _await_instance_absent(
        self,
        instance_id: str,
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
                    current.instance_id != instance_id
                    for current in snapshot.residents
                )
                if absent:
                    race_safe_not_found = (
                        isinstance(mutation_error, HttpAdapterError)
                        and mutation_error.status_code == 404
                    )
                    if (
                        mutation_error is not None
                        and not mutation_error.retryable
                        and not race_safe_not_found
                    ):
                        raise mutation_error
                    return

            if mutation_error is not None and not mutation_error.retryable:
                raise mutation_error
            if not await self._poll_delay(deadline):
                break

        detail = f"LM Studio did not confirm unload of instance '{instance_id}' before deadline"
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

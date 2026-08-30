from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping

import httpx
import pytest

from mnemosyne_macos.app import create_control_app
from mnemosyne_macos.config import MacConfig, save_config
from mnemosyne_macos.native_lifecycle import (
    HelperAuthorizationChallenge,
    NativeLifecycleIntegrityError,
    NativeLifecycleJournal,
)
from mnemosyne_macos.native_lifecycle_helper_transport import (
    LifecycleHelperTransport,
    NativeLifecycleHelperTransportError,
)
from mnemosyne_macos.native_lifecycle_runtime import NativeLifecycleManager
from mnemosyne_macos.runtime import NativeRuntime


def _config(tmp_path: Path, model_path: Path | None = None) -> MacConfig:
    payload: dict[str, object] = {
        "engines": {"llama_cpp": {"enabled": False}},
        "paths": {"state_database": str(tmp_path / "private" / "state.db")},
        "storage": {
            "default": "chosen",
            "locations": [
                {"name": "chosen", "path": str(tmp_path / "chosen-models")}
            ],
        },
    }
    if model_path is not None:
        payload["models"] = [
            {
                "alias": "retained-model",
                "engine": "llama.cpp",
                "model": str(model_path),
                "storage": "chosen",
                "enabled": False,
            }
        ]
    return MacConfig.model_validate(payload)


class _RecordingHelperTransport(LifecycleHelperTransport):
    def __init__(
        self,
        receipt: Callable[
            [HelperAuthorizationChallenge], Mapping[str, object]
        ]
        | None = None,
    ) -> None:
        self.receipt = receipt
        self.challenges: list[HelperAuthorizationChallenge] = []

    async def authorize(
        self, challenge: HelperAuthorizationChallenge
    ) -> Mapping[str, object]:
        self.challenges.append(challenge)
        if self.receipt is None:
            raise AssertionError("helper transport must not be called")
        return self.receipt(challenge)


async def _runtime(
    tmp_path: Path,
    model_path: Path | None = None,
    *,
    native_lifecycle_journal: NativeLifecycleJournal | None = None,
    native_lifecycle_helper_transport: LifecycleHelperTransport | None = None,
) -> NativeRuntime:
    config = _config(tmp_path, model_path)
    config_path = tmp_path / "private" / "config.yaml"
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        native_lifecycle_journal=native_lifecycle_journal,
        native_lifecycle_helper_transport=native_lifecycle_helper_transport,
    )

    async def installed_status() -> dict[str, dict[str, object]]:
        return {}

    runtime.runtime_updates.installed_status = installed_status  # type: ignore[method-assign]
    await runtime.start(raise_on_degraded=True)
    return runtime


@pytest.mark.asyncio
async def test_uninstall_preview_prepare_and_read_are_path_free_and_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "chosen-models"
    root.mkdir()
    exact = root / "nested" / "by-symlink-name.gguf"
    exact.parent.mkdir()
    exact.write_bytes(b"not loaded")
    runtime = await _runtime(tmp_path, exact)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        status = await client.get("/manager/native-lifecycle")
        assert status.status_code == 200
        assert status.json()["available"] is True
        assert status.json()["execution_available"] is False
        binding_before = await runtime.mac_inventory_index.active_storage_bindings()

        previews: dict[str, dict[str, object]] = {}
        for mode in (
            "app_only",
            "remove_state_runtimes_keep_weights",
            "remove_exclusive_managed",
        ):
            response = await client.post(
                "/manager/native-lifecycle/uninstall/preview",
                json={"schema_version": 1, "retention_mode": mode},
            )
            assert response.status_code == 200
            payload = response.json()
            previews[mode] = payload
            serialized = json.dumps(payload, sort_keys=True)
            assert str(exact) not in serialized
            assert str(root) not in serialized
            assert payload["execution_available"] is False
            assert payload["plan"]["retention_manifest"]["item_count"] == 1
        binding_after = await runtime.mac_inventory_index.active_storage_bindings()
        assert binding_after == binding_before

        selected = previews["remove_state_runtimes_keep_weights"]["plan"]
        transaction_id = selected["transaction_id"]
        request = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "retention_mode": "remove_state_runtimes_keep_weights",
        }
        prepared = await client.post(
            "/manager/native-lifecycle/uninstall/prepare", json=request
        )
        replay = await client.post(
            "/manager/native-lifecycle/uninstall/prepare", json=request
        )
        assert prepared.status_code == replay.status_code == 200
        assert prepared.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert prepared.json()["transaction"]["phase"] == "prepared"
        assert prepared.json()["execution_available"] is False

        read = await client.get(
            f"/manager/native-lifecycle/transactions/{transaction_id}"
        )
        assert read.status_code == 200
        assert str(exact) not in json.dumps(read.json(), sort_keys=True)

        incomplete = (await client.get("/manager/native-lifecycle")).json()
        assert incomplete["incomplete_count"] == 1
        assert incomplete["incomplete"][0]["phase"] == "prepared"

        manifest_path = runtime.native_lifecycle_journal.manifest_store.path_for(
            transaction_id
        )
        assert str(exact) in manifest_path.read_text()
        assert str(exact).encode() not in runtime.native_lifecycle_journal.path.read_bytes()
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_every_reinstall_safe_mode_preserves_an_unresolved_outbox(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)

    async def pending_usage() -> dict[str, object]:
        return {
            "node_id": "test-node",
            "node_id_source": "configured",
            "outbox_depth": 3,
        }

    runtime.usage.status = pending_usage  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        app_only = await client.post(
            "/manager/native-lifecycle/uninstall/preview",
            json={"schema_version": 1, "retention_mode": "app_only"},
        )
        assert app_only.status_code == 200
        assert app_only.json()["plan"]["outbox_decision"] == "preserve_with_state"

        keep_weights = await client.post(
            "/manager/native-lifecycle/uninstall/preview",
            json={
                "schema_version": 1,
                "retention_mode": "remove_state_runtimes_keep_weights",
            },
        )
        assert keep_weights.status_code == 200
        plan = keep_weights.json()["plan"]
        assert plan["token_outbox_count"] == 3
        assert plan["outbox_decision"] == "preserve_with_state"
        assert str(tmp_path) not in json.dumps(keep_weights.json(), sort_keys=True)
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_preview_refuses_stale_storage_authority_without_reconciling_it(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    before = await runtime.mac_inventory_index.active_storage_bindings()
    location = runtime.config.storage.locations[0].model_copy(
        update={"path": str(tmp_path / "different-folder")}
    )
    runtime.config = runtime.config.model_copy(
        update={
            "storage": runtime.config.storage.model_copy(
                update={"locations": [location]}
            )
        }
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.post(
            "/manager/native-lifecycle/uninstall/preview",
            json={"schema_version": 1, "retention_mode": "app_only"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "native_lifecycle_storage_authority_stale"
        )
        assert await runtime.mac_inventory_index.active_storage_bindings() == before
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_migration_preview_fails_closed_with_fixed_path_free_code(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.get(
            "/manager/native-lifecycle/migration/preview"
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == (
            "native_lifecycle_migration_evidence_unavailable"
        )
        assert str(tmp_path) not in json.dumps(response.json(), sort_keys=True)
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_production_authorization_api_stays_unavailable_without_peer_proof(
    tmp_path: Path,
) -> None:
    from test_native_lifecycle import (  # local normative fixture authority
        _execution_manifest_for,
        _plan,
    )

    runtime = await _runtime(tmp_path)
    journal = runtime.native_lifecycle_journal
    assert journal is not None
    prepared = journal.prepare(_plan(transaction_index=901)).transaction
    manifest = _execution_manifest_for(journal, prepared)
    journal.record_helper_staged(manifest)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    endpoint = (
        f"/manager/native-lifecycle/transactions/{prepared.transaction_id}"
        "/authorization"
    )
    try:
        status = await client.get(endpoint)
        assert status.status_code == 200
        assert status.json() == {
            "schema_version": 2,
            "transaction_id": prepared.transaction_id,
            "phase": "helper_staged",
            "state": "unavailable",
            "can_request": False,
            "execution_available": False,
        }
        issued = await client.post(
            endpoint + "/challenge", json={"schema_version": 2}
        )
        refused_again = await client.post(
            endpoint + "/challenge", json={"schema_version": 2}
        )
        assert issued.status_code == refused_again.status_code == 503
        assert issued.json()["detail"]["code"] == (
            "native_lifecycle_helper_authority_unavailable"
        )
        assert refused_again.json() == issued.json()
        serialized = json.dumps(issued.json(), sort_keys=True)
        assert manifest.application.exact_path not in serialized
        assert manifest.recovery_clone.exact_bundle_path not in serialized
        current = journal.get(prepared.transaction_id)
        assert current is not None
        assert current.phase.value == "helper_staged"
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_service_mediated_authorization_does_not_spawn_without_os_proof(
    tmp_path: Path,
) -> None:
    from test_native_lifecycle import (
        _execution_manifest_for,
        _plan,
    )

    transport = _RecordingHelperTransport()
    runtime = await _runtime(
        tmp_path,
        native_lifecycle_helper_transport=transport,
    )
    journal = runtime.native_lifecycle_journal
    assert journal is not None
    prepared = journal.prepare(_plan(transaction_index=903)).transaction
    journal.record_helper_staged(_execution_manifest_for(journal, prepared))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    endpoint = (
        f"/manager/native-lifecycle/transactions/{prepared.transaction_id}"
        "/authorization/perform"
    )
    try:
        response = await client.post(endpoint, json={"schema_version": 2})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == (
            "native_lifecycle_helper_authority_unavailable"
        )
        assert transport.challenges == []
        assert journal.helper_authorization_pending_count() == 0
        current = journal.get(prepared.transaction_id)
        assert current is not None
        assert current.phase.value == "helper_staged"
        status = await runtime.native_lifecycle_status()
        assert status["authorization_available"] is False
        assert status["execution_available"] is False
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_service_mediated_transport_can_submit_only_injected_test_proof(
    tmp_path: Path,
) -> None:
    from test_native_lifecycle import (
        Clock,
        _execution_manifest_for,
        _helper_proof_authority,
        _helper_submission,
        _plan,
    )

    def receipt(challenge: HelperAuthorizationChallenge) -> Mapping[str, object]:
        submission = _helper_submission(challenge)
        return {
            **submission.challenge_dict(),
            "authorization_digest": submission.authorization_digest,
            "authenticated_at": submission.authenticated_at,
            "authorization_proof": submission.authorization_proof,
        }

    config = _config(tmp_path)
    config_path = tmp_path / "private" / "config.yaml"
    save_config(config, config_path)
    journal = NativeLifecycleJournal(
        config_path,
        clock=Clock(),
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    transport = _RecordingHelperTransport(receipt)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        native_lifecycle_journal=journal,
        native_lifecycle_helper_transport=transport,
    )

    async def installed_status() -> dict[str, dict[str, object]]:
        return {}

    runtime.runtime_updates.installed_status = installed_status  # type: ignore[method-assign]
    await runtime.start(raise_on_degraded=True)
    prepared = journal.prepare(_plan(transaction_index=904)).transaction
    manifest = _execution_manifest_for(journal, prepared)
    journal.record_helper_staged(manifest)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    endpoint = (
        f"/manager/native-lifecycle/transactions/{prepared.transaction_id}"
        "/authorization/perform"
    )
    try:
        response = await client.post(endpoint, json={"schema_version": 2})
        assert response.status_code == 200
        payload = response.json()
        assert payload["authorized"] is True
        assert payload["transaction"]["phase"] == "authorized"
        assert payload["execution_available"] is False
        assert len(transport.challenges) == 1
        assert manifest.application.exact_path not in json.dumps(
            payload, sort_keys=True
        )
        status = await runtime.native_lifecycle_status()
        assert status["authorization_available"] is True
        assert status["execution_available"] is False
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_service_transport_failure_cancels_only_its_exact_challenge(
    tmp_path: Path,
) -> None:
    from test_native_lifecycle import (
        Clock,
        _execution_manifest_for,
        _helper_proof_authority,
        _plan,
    )

    def fail(
        _challenge: HelperAuthorizationChallenge,
    ) -> Mapping[str, object]:
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_unavailable"
        )

    config = _config(tmp_path)
    config_path = tmp_path / "private" / "config.yaml"
    save_config(config, config_path)
    journal = NativeLifecycleJournal(
        config_path,
        clock=Clock(),
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    transport = _RecordingHelperTransport(fail)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        native_lifecycle_journal=journal,
        native_lifecycle_helper_transport=transport,
    )

    async def installed_status() -> dict[str, dict[str, object]]:
        return {}

    runtime.runtime_updates.installed_status = installed_status  # type: ignore[method-assign]
    await runtime.start(raise_on_degraded=True)
    prepared = journal.prepare(_plan(transaction_index=905)).transaction
    journal.record_helper_staged(_execution_manifest_for(journal, prepared))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    endpoint = (
        f"/manager/native-lifecycle/transactions/{prepared.transaction_id}"
        "/authorization"
    )
    try:
        response = await client.post(
            endpoint + "/perform", json={"schema_version": 2}
        )
        assert response.status_code == 503
        assert len(transport.challenges) == 1
        assert journal.helper_authorization_pending_count() == 0
        status = (await client.get(endpoint)).json()
        assert status["state"] == "challenge_cancelled"
        assert status["can_request"] is True
        current = journal.get(prepared.transaction_id)
        assert current is not None
        assert current.phase.value == "helper_staged"
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_unissued_receipt_cancel_and_duplicate_keys_fail_closed(
    tmp_path: Path,
) -> None:
    from test_native_lifecycle import (
        _execution_manifest_for,
        _helper_submission,
        _plan,
    )
    from mnemosyne_macos.native_lifecycle import HelperAuthorizationChallenge

    runtime = await _runtime(tmp_path)
    journal = runtime.native_lifecycle_journal
    assert journal is not None
    prepared = journal.prepare(_plan(transaction_index=902)).transaction
    journal.record_helper_staged(_execution_manifest_for(journal, prepared))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    endpoint = (
        f"/manager/native-lifecycle/transactions/{prepared.transaction_id}"
        "/authorization"
    )
    try:
        challenge = HelperAuthorizationChallenge(
            schema_version=2,
            helper_protocol_version=2,
            nonce="99999999-9999-4999-8999-999999999999",
            transaction_id=prepared.transaction_id,
            transaction_authority_digest=f"sha256:{prepared.authority_digest}",
            execution_manifest_digest=(
                "sha256:" + _execution_manifest_for(
                    journal, prepared
                ).receipt.manifest_digest
            ),
            recovery_clone_identity_digest="sha256:" + "3" * 64,
            expected_helper_identifier=(
                "com.mnemosyne.inference.lifecycle-helper"
            ),
            expected_helper_build_digest="sha256:" + "4" * 64,
            expected_team_identifier="TEAM123456",
            expected_code_requirement_digest="sha256:" + "5" * 64,
            expected_app_build_digest="sha256:" + "6" * 64,
            expected_authorization_proof_algorithm="test-hmac-sha256-v1",
            expected_authorization_key_id="sha256:" + "7" * 64,
            session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            issued_at=1_788_100_000,
            expires_at=1_788_100_090,
        )
        receipt = _helper_submission(challenge)
        payload = {
            **receipt.challenge_dict(),
            "authorization_digest": receipt.authorization_digest,
            "authenticated_at": receipt.authenticated_at,
            "authorization_proof": receipt.authorization_proof,
        }
        encoded = json.dumps(payload, sort_keys=True)
        duplicate = encoded.replace(
            "{",
            '{"nonce":"00000000-0000-4000-8000-000000000001",',
            1,
        )
        invalid = await client.post(
            endpoint + "/receipt",
            content=duplicate,
            headers={"Content-Type": "application/json"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == (
            "native_lifecycle_helper_authority_invalid"
        )

        cancelled = await client.post(
            endpoint + "/cancel",
            json={
                "schema_version": 2,
                "nonce": challenge.nonce,
                "session_id": challenge.session_id,
            },
        )
        assert cancelled.status_code == 409
        rejected = await client.post(endpoint + "/receipt", json=payload)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == (
            "native_lifecycle_helper_authority_mismatch"
        )
        assert (await client.get(endpoint)).json()["state"] == "unavailable"
    finally:
        await client.aclose()
        await runtime.stop()


class _BrokenJournal:
    def initialize(self) -> None:
        raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_configless_lifecycle_manager_initialize_close_is_restart_safe() -> None:
    manager = NativeLifecycleManager(SimpleNamespace(), None)
    await manager.initialize()
    await manager.close()
    await manager.initialize()
    status = await manager.status()
    assert status["available"] is False
    assert status["error_code"] == "native_lifecycle_journal_unavailable"


@pytest.mark.asyncio
async def test_broken_lifecycle_journal_does_not_degrade_runtime_startup_or_jit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "private" / "config.yaml"
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        native_lifecycle_journal=_BrokenJournal(),  # type: ignore[arg-type]
    )

    async def installed_status() -> dict[str, dict[str, object]]:
        return {}

    runtime.runtime_updates.installed_status = installed_status  # type: ignore[method-assign]
    try:
        await runtime.start(raise_on_degraded=True)
        assert runtime.startup_error is None
        assert (await runtime.coordinator.status()).initialized is True
        status = await runtime.native_lifecycle_status()
        assert status["available"] is False
        assert status["error_code"] == "native_lifecycle_journal_corrupt"
    finally:
        await runtime.stop()

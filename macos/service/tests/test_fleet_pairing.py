from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import threading

import pytest

from mnemosyne_macos.fleet_pairing import (
    FLEET_DISPATCH_KEY,
    FLEET_MANAGEMENT_KEY,
    FLEET_SNAPSHOT_KEY,
    FleetPairingStore,
    InvalidPairingTransition,
    LegacyFleetCredentialsPresent,
    PairingCredentials,
    PairingErrorCode,
    PairingState,
    PairingStoreClosed,
    PrivateEnvironmentInvalid,
    PrivateEnvironmentWriteFailed,
    _await_blocking_outcome,
)


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ATTEMPT_ID = "11111111-1111-4111-8111-111111111112"
PAIRING_ID = "22222222-2222-4222-8222-222222222222"
CREDENTIALS = PairingCredentials(
    snapshot_key="snapshot-secret=one",
    dispatch_key="dispatch-secret=two",
    management_key="management-secret=three",
)


def _store(
    tmp_path: Path,
    *,
    process_environment: dict[str, str] | None = None,
) -> tuple[FleetPairingStore, Path, Path]:
    database = tmp_path / "state" / "pairing.db"
    environment = tmp_path / "private" / ".env"
    return (
        FleetPairingStore(
            database,
            environment,
            process_environment=(
                {} if process_environment is None else process_environment
            ),
        ),
        database,
        environment,
    )


def test_private_environment_writers_serialize_the_locked_read_replace_cycle(
    tmp_path: Path,
) -> None:
    first, _, environment = _store(tmp_path / "first")
    second = FleetPairingStore(
        tmp_path / "second" / "pairing.db",
        environment,
        process_environment={},
    )
    environment.parent.mkdir(parents=True, mode=0o700)
    environment.write_text("UNRELATED=preserved\n", encoding="utf-8")
    entered_render = threading.Event()
    allow_first = threading.Event()
    second_finished = threading.Event()
    failures: list[BaseException] = []
    original_render = first._render_environment  # noqa: SLF001

    def blocked_render(*args: object, **kwargs: object) -> str:
        entered_render.set()
        assert allow_first.wait(timeout=2)
        return original_render(*args, **kwargs)  # type: ignore[arg-type]

    def update_first() -> None:
        try:
            first._update_environment_file(  # noqa: SLF001
                replacements={FLEET_SNAPSHOT_KEY: "snapshot"}
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def update_second() -> None:
        try:
            second._update_environment_file(  # noqa: SLF001
                replacements={FLEET_DISPATCH_KEY: "dispatch"}
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            second_finished.set()

    first._render_environment = blocked_render  # type: ignore[method-assign]  # noqa: SLF001
    first_thread = threading.Thread(target=update_first)
    second_thread = threading.Thread(target=update_second)
    first_thread.start()
    assert entered_render.wait(timeout=2)
    second_thread.start()
    assert not second_finished.wait(timeout=0.1)
    allow_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert failures == []
    assert environment.read_text(encoding="utf-8") == (
        "UNRELATED=preserved\n"
        f"{FLEET_SNAPSHOT_KEY}=snapshot\n"
        f"{FLEET_DISPATCH_KEY}=dispatch\n"
    )
    lock_path = environment.with_name(f"{environment.name}.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_symlinked_private_environment_lock_is_rejected(tmp_path: Path) -> None:
    store, _, environment = _store(tmp_path)
    environment.parent.mkdir(parents=True, mode=0o700)
    environment.write_text("UNRELATED=preserved\n", encoding="utf-8")
    victim = tmp_path / "victim.lock"
    victim.write_text("VICTIM=unchanged\n", encoding="utf-8")
    environment.with_name(f"{environment.name}.lock").symlink_to(victim)

    with pytest.raises(PrivateEnvironmentInvalid):
        store._update_environment_file(  # noqa: SLF001
            replacements={FLEET_SNAPSHOT_KEY: "snapshot"}
        )

    assert environment.read_text(encoding="utf-8") == "UNRELATED=preserved\n"
    assert victim.read_text(encoding="utf-8") == "VICTIM=unchanged\n"


def test_private_environment_swap_during_update_fails_closed(tmp_path: Path) -> None:
    store, _, environment = _store(tmp_path)
    environment.parent.mkdir(parents=True, mode=0o700)
    environment.write_text("UNRELATED=preserved\n", encoding="utf-8")
    original = environment.with_name("original.env")
    victim = tmp_path / "victim.env"
    victim.write_text("VICTIM=unchanged\n", encoding="utf-8")
    original_render = store._render_environment  # noqa: SLF001

    def swap_before_replace(*args: object, **kwargs: object) -> str:
        environment.rename(original)
        environment.symlink_to(victim)
        return original_render(*args, **kwargs)  # type: ignore[arg-type]

    store._render_environment = swap_before_replace  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(PrivateEnvironmentInvalid):
        store._update_environment_file(  # noqa: SLF001
            replacements={FLEET_SNAPSHOT_KEY: "snapshot"}
        )

    assert environment.is_symlink()
    assert original.read_text(encoding="utf-8") == "UNRELATED=preserved\n"
    assert victim.read_text(encoding="utf-8") == "VICTIM=unchanged\n"
    assert not list(environment.parent.glob(f".{environment.name}.*.tmp"))


async def _assigned(store: FleetPairingStore) -> None:
    await store.begin_attempt(
        hub_origin="https://nyx.example",
        node_url="https://mac.example",
        attempt_id=ATTEMPT_ID,
    )
    await store.record_assignment(
        pairing_id=PAIRING_ID,
        node_id="mac-studio",
        credential_epoch=1,
    )


async def _paired(store: FleetPairingStore) -> None:
    await _assigned(store)
    await store.activate_credentials(CREDENTIALS)
    await store.mark_paired()


@pytest.mark.asyncio
async def test_full_lifecycle_persists_across_restart_and_preserves_env(
    tmp_path: Path,
) -> None:
    store, database, environment = _store(tmp_path)
    environment.parent.mkdir(parents=True, mode=0o755)
    os.chmod(environment.parent, 0o755)
    environment.write_text(
        "# retained comment\nUNRELATED_SETTING=keep-me\nFLEET_API_KEY=\n",
        encoding="utf-8",
    )
    os.chmod(environment, 0o644)

    initial = await store.initialize()
    assert initial.state == PairingState.UNPAIRED
    assert initial.attempt_id is None

    pending = await store.begin_attempt(
        hub_origin="https://nyx.example/",
        node_url="https://mac.example/",
        attempt_id=ATTEMPT_ID,
    )
    replay = await store.begin_attempt(
        hub_origin="https://nyx.example",
        node_url="https://mac.example",
        attempt_id=ATTEMPT_ID,
    )
    assert replay == pending

    assigned = await store.record_assignment(
        pairing_id=PAIRING_ID,
        node_id="mac-studio",
        credential_epoch=1,
    )
    assert (
        await store.record_assignment(
            pairing_id=PAIRING_ID,
            node_id="mac-studio",
            credential_epoch=1,
        )
    ) == assigned

    activated = await store.activate_credentials(CREDENTIALS)
    assert activated.credentials_owned is True
    assert activated.credential_write_pending is False
    assert await store.activate_credentials(CREDENTIALS) == activated
    paired = await store.mark_paired()
    assert paired.state == PairingState.PAIRED
    assert paired.paired_at is not None
    with pytest.raises(InvalidPairingTransition):
        await store.clear_pairing()

    contents = environment.read_text(encoding="utf-8")
    assert "# retained comment\nUNRELATED_SETTING=keep-me\n" in contents
    for key, value in CREDENTIALS.environment().items():
        assert contents.count(f"{key}={value}\n") == 1
    assert stat.S_IMODE(environment.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600
    await store.close()

    restarted = FleetPairingStore(database, environment, process_environment={})
    restored = await restarted.initialize()
    assert restored.state == PairingState.PAIRED
    assert restored.device_id == initial.device_id
    assert restored.created_at == initial.created_at
    assert restored.attempt_id == ATTEMPT_ID
    assert restored.pairing_id == PAIRING_ID
    assert await restarted.activate_credentials(CREDENTIALS) == restored

    revoked = await restarted.mark_revoked()
    assert revoked.state == PairingState.REVOKED
    assert revoked.revoked_at is not None
    cleared = await restarted.clear_pairing()
    assert cleared.state == PairingState.UNPAIRED
    assert cleared.device_id == initial.device_id
    assert cleared.attempt_id is None
    assert cleared.credentials_owned is False
    assert environment.read_text(encoding="utf-8") == (
        "# retained comment\nUNRELATED_SETTING=keep-me\n"
    )
    assert stat.S_IMODE(environment.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600
    await restarted.close()
    with pytest.raises(PairingStoreClosed):
        await restarted.status()


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        '"quoted',
        "quoted'",
        "x" * 4097,
    ],
)
def test_credentials_must_be_stripped_bounded_and_env_round_trip_safe(
    invalid: str,
) -> None:
    with pytest.raises(ValueError):
        PairingCredentials(invalid, "dispatch", "management").validated()

    maximum = PairingCredentials("x" * 4096, "dispatch=ok", "management")
    assert maximum.validated() is maximum
    with pytest.raises(ValueError):
        PairingCredentials("same", "same", "different").validated()

    representation = repr(CREDENTIALS)
    assert representation == "PairingCredentials()"
    assert all(value not in representation for value in CREDENTIALS.environment().values())


@pytest.mark.asyncio
async def test_attempt_assignment_and_credential_replays_cannot_change_identity(
    tmp_path: Path,
) -> None:
    store, _, environment = _store(tmp_path)
    await store.initialize()
    pending = await store.begin_attempt(
        hub_origin="https://nyx.example",
        node_url="https://mac.example",
        attempt_id=ATTEMPT_ID,
    )

    for changed in (
        {"hub_origin": "https://other.example", "node_url": "https://mac.example"},
        {"hub_origin": "https://nyx.example", "node_url": "https://other.example"},
    ):
        with pytest.raises(InvalidPairingTransition):
            await store.begin_attempt(attempt_id=ATTEMPT_ID, **changed)
    with pytest.raises(InvalidPairingTransition):
        await store.begin_attempt(
            hub_origin="https://nyx.example",
            node_url="https://mac.example",
            attempt_id=OTHER_ATTEMPT_ID,
        )
    assert await store.status() == pending

    assignment = await store.record_assignment(
        pairing_id=PAIRING_ID,
        node_id="mac-studio",
        credential_epoch=1,
    )
    replacements = (
        {
            "pairing_id": "22222222-2222-4222-8222-222222222223",
            "node_id": "mac-studio",
            "credential_epoch": 1,
        },
        {
            "pairing_id": PAIRING_ID,
            "node_id": "other-mac",
            "credential_epoch": 1,
        },
        {
            "pairing_id": PAIRING_ID,
            "node_id": "mac-studio",
            "credential_epoch": 2,
        },
    )
    for replacement in replacements:
        with pytest.raises(InvalidPairingTransition):
            await store.record_assignment(**replacement)
    assert await store.status() == assignment

    activated = await store.activate_credentials(CREDENTIALS)
    original_file = environment.read_bytes()
    assert await store.activate_credentials(CREDENTIALS) == activated
    with pytest.raises(InvalidPairingTransition):
        await store.activate_credentials(
            PairingCredentials(
                snapshot_key="replacement-snapshot",
                dispatch_key=CREDENTIALS.dispatch_key,
                management_key=CREDENTIALS.management_key,
            )
        )
    assert environment.read_bytes() == original_file
    assert await store.status() == activated


@pytest.mark.parametrize("source", ["file", "process"])
@pytest.mark.asyncio
async def test_legacy_credentials_are_never_overwritten_without_ownership(
    tmp_path: Path,
    source: str,
) -> None:
    process_environment: dict[str, str] = {}
    store, _, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    if source == "file":
        environment.parent.mkdir(parents=True)
        environment.write_text(
            "UNRELATED=keep\nFLEET_API_KEY=legacy-file-secret\n",
            encoding="utf-8",
        )
        original = environment.read_bytes()
    else:
        process_environment[FLEET_DISPATCH_KEY] = "legacy-process-secret"
        original = None

    initial = await store.initialize()
    assert initial.state == PairingState.UNPAIRED
    assert initial.legacy_credentials_present is True
    with pytest.raises(LegacyFleetCredentialsPresent):
        await store.begin_attempt(
            hub_origin="https://nyx.example",
            node_url="https://mac.example",
            attempt_id=ATTEMPT_ID,
        )
    refused = await store.status()
    assert refused.state == PairingState.UNPAIRED
    assert refused.attempt_id is None
    assert refused.last_error_code == (
        PairingErrorCode.LEGACY_FLEET_CREDENTIALS_PRESENT
    )
    assert refused.legacy_credentials_present is True
    if original is not None:
        assert environment.read_bytes() == original


@pytest.mark.asyncio
async def test_symlinked_environment_is_rejected_without_touching_its_target(
    tmp_path: Path,
) -> None:
    store, _, environment = _store(tmp_path)
    await store.initialize()
    await _assigned(store)
    environment.parent.mkdir(parents=True)
    target = tmp_path / "victim.env"
    target.write_text("VICTIM=unchanged\n", encoding="utf-8")
    environment.symlink_to(target)

    with pytest.raises(PrivateEnvironmentInvalid):
        await store.activate_credentials(CREDENTIALS)
    failed = await store.status()
    assert failed.state == PairingState.RECOVERY_REQUIRED
    assert failed.last_error_code == PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
    assert failed.credentials_owned is False
    assert target.read_text(encoding="utf-8") == "VICTIM=unchanged\n"
    assert environment.is_symlink()

    environment.unlink()
    recovered = await store.activate_credentials(CREDENTIALS)
    assert recovered.state == PairingState.PENDING
    assert recovered.credentials_owned is True
    assert target.read_text(encoding="utf-8") == "VICTIM=unchanged\n"


@pytest.mark.asyncio
async def test_process_environment_override_refuses_activation_and_confirmation(
    tmp_path: Path,
) -> None:
    process_environment: dict[str, str] = {}
    store, _, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    await store.initialize()
    await _assigned(store)
    await store.activate_credentials(CREDENTIALS)
    original = environment.read_bytes()

    process_environment[FLEET_SNAPSHOT_KEY] = "external-override"
    with pytest.raises(PrivateEnvironmentInvalid):
        await store.activate_credentials(CREDENTIALS)
    refused = await store.status()
    assert refused.state == PairingState.RECOVERY_REQUIRED
    assert refused.last_error_code == PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE
    assert environment.read_bytes() == original
    with pytest.raises(PrivateEnvironmentInvalid):
        await store.mark_paired()

    process_environment.update(CREDENTIALS.environment())
    assert (await store.activate_credentials(CREDENTIALS)).state == (
        PairingState.RECOVERY_REQUIRED
    )
    confirmed = await store.mark_paired()
    assert confirmed.state == PairingState.PAIRED
    assert confirmed.last_error_code is None


@pytest.mark.asyncio
async def test_invalid_transitions_and_clear_require_revocation(tmp_path: Path) -> None:
    store, _, _ = _store(tmp_path)
    await store.initialize()
    with pytest.raises(InvalidPairingTransition):
        await store.record_assignment(
            pairing_id=PAIRING_ID,
            node_id="mac-studio",
            credential_epoch=1,
        )
    with pytest.raises(InvalidPairingTransition):
        await store.activate_credentials(CREDENTIALS)
    with pytest.raises(InvalidPairingTransition):
        await store.mark_paired()
    with pytest.raises(InvalidPairingTransition):
        await store.mark_revoked()
    with pytest.raises(InvalidPairingTransition):
        await store.mark_recovery_required(PairingErrorCode.PAIRING_EXPIRED)

    await store.begin_attempt(
        hub_origin="https://nyx.example",
        node_url="https://mac.example",
        attempt_id=ATTEMPT_ID,
    )
    with pytest.raises(InvalidPairingTransition):
        await store.activate_credentials(CREDENTIALS)
    assert (await store.clear_pairing()).state == PairingState.UNPAIRED

    await _paired(store)
    with pytest.raises(InvalidPairingTransition):
        await store.clear_pairing()
    revoked = await store.mark_revoked()
    assert revoked.state == PairingState.REVOKED
    with pytest.raises(InvalidPairingTransition):
        await store.mark_recovery_required(PairingErrorCode.STATE_INCONSISTENT)
    with pytest.raises(InvalidPairingTransition):
        await store.begin_attempt(
            hub_origin="https://nyx.example",
            node_url="https://mac.example",
            attempt_id=OTHER_ATTEMPT_ID,
        )
    assert (await store.clear_pairing()).state == PairingState.UNPAIRED

    await _assigned(store)
    await store.mark_recovery_required(PairingErrorCode.PAIRING_EXPIRED)
    assert (await store.clear_pairing()).state == PairingState.UNPAIRED


@pytest.mark.asyncio
async def test_revoked_credentials_retire_idempotently_without_erasing_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_environment: dict[str, str] = {}
    store, database, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    await store.initialize()
    await _paired(store)
    revoked = await store.mark_revoked()
    original_update = store._update_environment_file  # noqa: SLF001
    failures = 0

    def fail_once(**kwargs: object) -> None:
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("injected private environment failure")
        original_update(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_update_environment_file", fail_once)
    with pytest.raises(PrivateEnvironmentWriteFailed):
        await store.retire_revoked_credentials()
    failed = await store.status()
    assert failed.state == PairingState.REVOKED
    assert failed.pairing_id == revoked.pairing_id
    assert failed.node_id == revoked.node_id
    assert failed.credential_epoch == revoked.credential_epoch
    assert failed.revoked_at == revoked.revoked_at
    assert failed.credentials_owned is True
    assert failed.credential_write_pending is True
    assert failed.last_error_code == PairingErrorCode.PRIVATE_ENVIRONMENT_WRITE_FAILED
    assert all(
        f"{key}={value}\n" in environment.read_text(encoding="utf-8")
        for key, value in CREDENTIALS.environment().items()
    )

    await store.close()
    restarted = FleetPairingStore(
        database,
        environment,
        process_environment=process_environment,
    )
    restored = await restarted.initialize()
    assert restored.state == PairingState.REVOKED
    assert restored.credential_write_pending is True
    retired = await restarted.retire_revoked_credentials()
    assert retired.state == PairingState.REVOKED
    assert retired.pairing_id == revoked.pairing_id
    assert retired.node_id == revoked.node_id
    assert retired.credential_epoch == revoked.credential_epoch
    assert retired.revoked_at == revoked.revoked_at
    assert retired.credentials_owned is False
    assert retired.credential_write_pending is False
    assert all(key not in process_environment for key in CREDENTIALS.environment())
    assert all(
        f"{key}=" not in environment.read_text(encoding="utf-8")
        for key in CREDENTIALS.environment()
    )
    assert await restarted.retire_revoked_credentials() == retired


@pytest.mark.asyncio
async def test_revoked_retirement_never_deletes_changed_static_credentials(
    tmp_path: Path,
) -> None:
    process_environment: dict[str, str] = {}
    store, _, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    await store.initialize()
    await _paired(store)
    revoked = await store.mark_revoked()
    static_values = {
        FLEET_SNAPSHOT_KEY: "static-snapshot",
        FLEET_DISPATCH_KEY: "static-dispatch",
        FLEET_MANAGEMENT_KEY: "static-management",
    }
    rendered = "".join(f"{key}={value}\n" for key, value in static_values.items())
    environment.write_text(rendered, encoding="utf-8")

    with pytest.raises(PrivateEnvironmentInvalid):
        await store.retire_revoked_credentials()

    refused = await store.status()
    assert refused.state == PairingState.REVOKED
    assert refused.pairing_id == revoked.pairing_id
    assert refused.credentials_owned is True
    assert refused.credential_write_pending is True
    assert environment.read_text(encoding="utf-8") == rendered


@pytest.mark.asyncio
async def test_revoked_retirement_rejects_duplicate_changed_managed_bundle(
    tmp_path: Path,
) -> None:
    process_environment: dict[str, str] = {}
    store, _, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    await store.initialize()
    await _paired(store)
    revoked = await store.mark_revoked()
    original = environment.read_text(encoding="utf-8")
    changed = (
        f"{FLEET_SNAPSHOT_KEY}=later-static-snapshot\n"
        f"{FLEET_DISPATCH_KEY}=later-static-dispatch\n"
        f"{FLEET_MANAGEMENT_KEY}=later-static-management\n"
    )
    environment.write_text(original + changed, encoding="utf-8")
    before = environment.read_bytes()

    with pytest.raises(PrivateEnvironmentInvalid):
        await store.retire_revoked_credentials()

    refused = await store.status()
    assert refused.state == PairingState.REVOKED
    assert refused.pairing_id == revoked.pairing_id
    assert refused.credentials_owned is True
    assert refused.credential_write_pending is True
    assert environment.read_bytes() == before
    assert all(value.encode("utf-8") in before for value in CREDENTIALS.environment().values())
    assert b"later-static-snapshot" in before
    assert b"later-static-dispatch" in before
    assert b"later-static-management" in before


@pytest.mark.asyncio
async def test_restart_detects_a_truncated_owned_environment(tmp_path: Path) -> None:
    store, database, environment = _store(tmp_path)
    await store.initialize()
    await _paired(store)
    device_id = (await store.status()).device_id
    await store.close()

    retained = [
        line
        for line in environment.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{FLEET_MANAGEMENT_KEY}=")
    ]
    environment.write_text("\n".join(retained) + "\n", encoding="utf-8")

    restarted = FleetPairingStore(database, environment, process_environment={})
    recovered = await restarted.initialize()
    assert recovered.device_id == device_id
    assert recovered.state == PairingState.RECOVERY_REQUIRED
    assert recovered.last_error_code == PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID


@pytest.mark.asyncio
async def test_cancelled_activation_reaches_one_complete_durable_outcome(
    tmp_path: Path,
) -> None:
    process_environment: dict[str, str] = {}
    store, database, environment = _store(
        tmp_path,
        process_environment=process_environment,
    )
    await store.initialize()
    await _assigned(store)
    entered = threading.Event()
    allow_write = threading.Event()
    update_environment = store._update_environment_file  # noqa: SLF001

    def blocked_update(**kwargs: object) -> None:
        entered.set()
        assert allow_write.wait(timeout=2)
        update_environment(**kwargs)  # type: ignore[arg-type]

    store._update_environment_file = blocked_update  # type: ignore[method-assign]  # noqa: SLF001
    activation = asyncio.create_task(store.activate_credentials(CREDENTIALS))
    assert await asyncio.to_thread(entered.wait, 2)
    activation.cancel()
    await asyncio.sleep(0)
    assert not activation.done()
    allow_write.set()
    with pytest.raises(asyncio.CancelledError):
        await activation

    completed = await store.status()
    assert completed.state == PairingState.PENDING
    assert completed.credentials_owned is True
    assert completed.credential_write_pending is False
    contents = environment.read_text(encoding="utf-8")
    assert all(
        f"{key}={value}\n" in contents
        for key, value in CREDENTIALS.environment().items()
    )
    await store.close()
    restarted = FleetPairingStore(database, environment, process_environment={})
    assert (await restarted.initialize()).state == PairingState.PENDING


@pytest.mark.asyncio
async def test_caller_cancellation_precedes_a_later_worker_failure() -> None:
    entered = threading.Event()
    allow_failure = threading.Event()

    def fail_after_release() -> None:
        entered.set()
        assert allow_failure.wait(timeout=2)
        raise RuntimeError("worker failed after caller cancellation")

    operation = asyncio.create_task(_await_blocking_outcome(fail_after_release))
    assert await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    allow_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await operation


@pytest.mark.asyncio
async def test_cancelled_revoked_clear_reaches_one_complete_durable_outcome(
    tmp_path: Path,
) -> None:
    store, _, environment = _store(tmp_path)
    await store.initialize()
    await _paired(store)
    await store.mark_revoked()
    entered = threading.Event()
    allow_write = threading.Event()
    update_environment = store._update_environment_file  # noqa: SLF001

    def blocked_update(**kwargs: object) -> None:
        entered.set()
        assert allow_write.wait(timeout=2)
        update_environment(**kwargs)  # type: ignore[arg-type]

    store._update_environment_file = blocked_update  # type: ignore[method-assign]  # noqa: SLF001
    clearing = asyncio.create_task(store.clear_pairing())
    assert await asyncio.to_thread(entered.wait, 2)
    clearing.cancel()
    await asyncio.sleep(0)
    assert not clearing.done()
    allow_write.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    cleared = await store.status()
    assert cleared.state == PairingState.UNPAIRED
    assert cleared.credentials_owned is False
    contents = environment.read_text(encoding="utf-8")
    assert all(f"{key}=" not in contents for key in CREDENTIALS.environment())

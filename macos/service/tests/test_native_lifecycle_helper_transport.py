from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from mnemosyne_macos.native_lifecycle import HelperAuthorizationChallenge
from mnemosyne_macos.native_lifecycle_helper_transport import (
    BundledLifecycleHelperTransport,
    NativeLifecycleHelperTransportError,
)


def _challenge() -> HelperAuthorizationChallenge:
    now = int(time.time())
    return HelperAuthorizationChallenge(
        schema_version=2,
        helper_protocol_version=2,
        nonce="11111111-1111-4111-8111-111111111111",
        transaction_id="22222222-2222-4222-8222-222222222222",
        transaction_authority_digest="sha256:" + "1" * 64,
        execution_manifest_digest="sha256:" + "2" * 64,
        recovery_clone_identity_digest="sha256:" + "3" * 64,
        expected_helper_identifier=(
            "com.mnemosyne.inference.lifecycle-helper"
        ),
        expected_helper_build_digest="sha256:" + "4" * 64,
        expected_team_identifier="ABCDE12345",
        expected_code_requirement_digest="sha256:" + "5" * 64,
        expected_app_build_digest="sha256:" + "6" * 64,
        expected_authorization_proof_algorithm="test-hmac-sha256-v1",
        expected_authorization_key_id="sha256:" + "7" * 64,
        session_id="33333333-3333-4333-8333-333333333333",
        issued_at=now,
        expires_at=now + 60,
    )


def _fixture_helper(tmp_path: Path, *, trailing_byte: bool = False) -> Path:
    executable = (
        tmp_path
        / "Unified Inference.app"
        / "Contents"
        / "Helpers"
        / "MnemosyneLifecycleAuthorization.app"
        / "Contents"
        / "MacOS"
        / "mnemosyne-lifecycle-helper"
    )
    executable.parent.mkdir(parents=True)
    trailing = "peer.sendall(b'x')" if trailing_byte else ""
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import socket
import struct
import sys

descriptor = int(sys.argv[2])
peer = socket.socket(fileno=descriptor)
header = peer.recv(4)
size = struct.unpack(">I", header)[0]
chunks = []
while sum(map(len, chunks)) < size:
    chunks.append(peer.recv(size - sum(map(len, chunks))))
request = json.loads(b"".join(chunks))
payload = json.dumps(
    {{
        "secret_present": "MNEMOSYNE_TRANSPORT_TEST_SECRET" in os.environ,
        "transaction_id": request["transaction_id"],
    }},
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
peer.sendall(struct.pack(">I", len(payload)) + payload)
{trailing}
peer.close()
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.asyncio
async def test_service_transport_exchanges_one_bounded_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fixture_helper(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_TRANSPORT_TEST_SECRET", "must-not-leak")

    result = await BundledLifecycleHelperTransport(executable).authorize(
        _challenge()
    )

    assert result == {
        "secret_present": False,
        "transaction_id": "22222222-2222-4222-8222-222222222222",
    }


@pytest.mark.asyncio
async def test_service_transport_rejects_non_bundled_or_symlink_helper(
    tmp_path: Path,
) -> None:
    bundled = _fixture_helper(tmp_path)
    wrong = tmp_path / "mnemosyne-lifecycle-helper"
    wrong.symlink_to(bundled)

    with pytest.raises(NativeLifecycleHelperTransportError) as caught:
        await BundledLifecycleHelperTransport(wrong).authorize(_challenge())

    assert caught.value.code == "native_lifecycle_helper_authority_unavailable"

    direct = (
        tmp_path
        / "Direct.app"
        / "Contents"
        / "MacOS"
        / "mnemosyne-lifecycle-helper"
    )
    direct.parent.mkdir(parents=True)
    direct.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    direct.chmod(0o700)
    with pytest.raises(NativeLifecycleHelperTransportError) as caught:
        await BundledLifecycleHelperTransport(direct).authorize(_challenge())

    assert caught.value.code == "native_lifecycle_helper_authority_unavailable"


@pytest.mark.asyncio
async def test_service_transport_rejects_a_second_helper_frame(
    tmp_path: Path,
) -> None:
    executable = _fixture_helper(tmp_path, trailing_byte=True)

    with pytest.raises(NativeLifecycleHelperTransportError) as caught:
        await BundledLifecycleHelperTransport(executable).authorize(_challenge())

    assert caught.value.code == "native_lifecycle_helper_authority_conflict"

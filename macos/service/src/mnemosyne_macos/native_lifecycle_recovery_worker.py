"""Inert entrypoint skeleton for the future signed lifecycle recovery runner.

The worker accepts one closed protocol-v2 registration over an already-open
unnamed Unix socketpair.  It opens the product's fixed lifecycle journal,
proves the registration names the sole active transaction and an exact durable
grant, and then refuses execution because no signed OS-effects adapter is
wired.  There is intentionally no path, command, PID, port, launch-label,
argument-vector, or credential input surface.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import stat
import struct
import sys
from uuid import uuid4

from .lifecycle_execution_protocol import (
    MAXIMUM_EXECUTION_JSON_BYTES,
    LifecycleExecutionMessageType,
    LifecycleExecutionProtocolError,
    LifecycleExecutionRefusedV2,
    LifecycleRunnerRegistrationV2,
    decode_lifecycle_execution_frame,
    encode_lifecycle_execution_message,
)
from .native_lifecycle import (
    NativeLifecycleError,
    NativeLifecycleJournal,
)


_MAXIMUM_SESSION_FD = 1_024


def refusal_for_registration(
    journal: NativeLifecycleJournal,
    registration: LifecycleRunnerRegistrationV2,
    *,
    peer_attested: bool = False,
) -> LifecycleExecutionRefusedV2:
    """Validate the sole active grant, then return the fixed inert refusal."""

    incomplete = journal.list_incomplete()
    if not peer_attested:
        # The protocol fields are self-asserted. Until LOCAL_PEERTOKEN and the
        # exact signed runner identity are verified, a same-user process gets
        # no registration authority even if it knows every public digest.
        code = "execution_disabled"
    elif (
        len(incomplete) != 1
        or incomplete[0].transaction_id != registration.transaction_id
    ):
        code = "execution_grant_invalid"
    else:
        try:
            grant = journal.require_execution_start_grant(
                transaction_id=registration.transaction_id,
                grant_id=registration.grant_id,
            )
            code = (
                "runner_adapter_unavailable"
                if registration.grant_digest
                == f"sha256:{grant.grant_digest}"
                else "execution_grant_invalid"
            )
        except NativeLifecycleError:
            code = "execution_grant_invalid"
    return LifecycleExecutionRefusedV2(
        protocol_version=2,
        message_type=LifecycleExecutionMessageType.REFUSED,
        transaction_id=registration.transaction_id,
        grant_id=registration.grant_id,
        runner_session_id=registration.runner_session_id,
        sequence=registration.sequence,
        nonce=str(uuid4()),
        request_nonce=registration.nonce,
        error_code=code,
    )


def process_one_frame(
    journal: NativeLifecycleJournal,
    frame: bytes,
    *,
    peer_attested: bool = False,
) -> bytes:
    message = decode_lifecycle_execution_frame(frame)
    if not isinstance(message, LifecycleRunnerRegistrationV2):
        raise LifecycleExecutionProtocolError()
    return encode_lifecycle_execution_message(
        refusal_for_registration(
            journal, message, peer_attested=peer_attested
        )
    )


def run_session(descriptor: int, journal: NativeLifecycleJournal) -> None:
    peer = _validated_socket(descriptor)
    try:
        header = _read_exact(peer, 4)
        size = struct.unpack(">I", header)[0]
        if size == 0 or size > MAXIMUM_EXECUTION_JSON_BYTES:
            raise LifecycleExecutionProtocolError("execution_protocol_oversized")
        frame = header + _read_exact(peer, size)
        response = process_one_frame(journal, frame)
        peer.sendall(response)
    finally:
        peer.close()


def _validated_socket(descriptor: int) -> socket.socket:
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or not 3 <= descriptor <= _MAXIMUM_SESSION_FD
    ):
        raise LifecycleExecutionProtocolError()
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISSOCK(status.st_mode):
            raise OSError
        peer = socket.socket(fileno=descriptor)
        if peer.family != socket.AF_UNIX or peer.type & socket.SOCK_STREAM == 0:
            peer.detach()
            raise OSError
        # An unnamed socketpair has no filesystem pathname at either end.
        if peer.getsockname() not in ("", b"") or peer.getpeername() not in ("", b""):
            peer.detach()
            raise OSError
        peer.settimeout(5.0)
        return peer
    except OSError:
        raise LifecycleExecutionProtocolError() from None


def _read_exact(peer: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = peer.recv(remaining)
        if not chunk:
            raise LifecycleExecutionProtocolError()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mnemosyne inert native lifecycle recovery worker"
    )
    parser.add_argument("--session-fd", required=True, type=int)
    parser.add_argument("--state-anchor-fd", required=True, type=int)
    return parser


def _journal_from_state_anchor_descriptor(
    descriptor: int,
) -> NativeLifecycleJournal:
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or not 3 <= descriptor <= _MAXIMUM_SESSION_FD
    ):
        raise LifecycleExecutionProtocolError()
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
        ):
            raise OSError
    except OSError:
        raise LifecycleExecutionProtocolError() from None
    try:
        if hasattr(fcntl, "F_GETPATH"):
            raw_path = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                b"\0" * 1_024,
            )
            anchor = bytes(raw_path).split(b"\0", 1)[0].decode("utf-8")
        else:  # pragma: no cover - Darwin production uses F_GETPATH
            anchor = os.readlink(f"/proc/self/fd/{descriptor}")
        if not anchor or not os.path.isabs(anchor) or "\0" in anchor:
            raise OSError
        anchored_status = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(anchored_status.st_mode)
            or anchored_status.st_dev != status.st_dev
            or anchored_status.st_ino != status.st_ino
            or anchored_status.st_uid != status.st_uid
        ):
            raise OSError
    except (OSError, UnicodeError, ValueError):
        raise LifecycleExecutionProtocolError() from None
    # The path is recovered from and cross-checked against the inherited
    # directory capability. No argv, environment, cwd, or home default can
    # select lifecycle state. Destructive effects remain disabled until the
    # worker also has exact signed-peer attestation and descriptor-relative IO.
    return NativeLifecycleJournal(os.path.join(anchor, "config.yaml"))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        journal = _journal_from_state_anchor_descriptor(
            args.state_anchor_fd
        )
        if not journal.path.exists():
            return 78
        journal.initialize()
        run_session(args.session_fd, journal)
        return 78  # Effects remain unavailable even after a valid refusal.
    except (LifecycleExecutionProtocolError, NativeLifecycleError, OSError):
        return 78


if __name__ == "__main__":  # pragma: no cover - exercised through module entrypoint
    sys.exit(main())


__all__ = ["main", "process_one_frame", "refusal_for_registration", "run_session"]

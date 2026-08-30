#!/usr/bin/env python3
"""Offline key and publication ceremony for compatibility catalog v1.

The private-key operations in this module are deliberately separate from the
Fleet and native runtime packages.  They never contact a network, never accept
a passphrase on the command line, and never write a private key below the
repository checkout.  Catalog consumers continue to use ``catalog.py`` only.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:
    from .catalog import (
        BUILT_IN_CATALOG_ID,
        BUILT_IN_PUBLISHER,
        MAX_CATALOG_BYTES,
        CatalogError,
        CatalogVerifier,
        TrustedCatalogKey,
        canonical_json,
        catalog_digest,
        parse_catalog_json,
        signing_message,
    )
except ImportError:  # Direct execution from a checked-out repository.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from compatibility_catalog.catalog import (  # type: ignore[no-redef]
        BUILT_IN_CATALOG_ID,
        BUILT_IN_PUBLISHER,
        MAX_CATALOG_BYTES,
        CatalogError,
        CatalogVerifier,
        TrustedCatalogKey,
        canonical_json,
        catalog_digest,
        parse_catalog_json,
        signing_message,
    )


CEREMONY_SCHEMA_VERSION = 1
MAX_TIMESTAMP = 4_102_444_800
MAX_SEQUENCE = 9_007_199_254_740_991
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_TRUST_KEY_BYTES = 16 * 1024
MAX_SIGNATURE_BYTES = 8 * 1024
MIN_PASSPHRASE_BYTES = 16
MAX_PASSPHRASE_BYTES = 1024

# This deterministic key exists only for golden-vector tests.  Keeping the
# deny-list here makes it impossible to accidentally turn the fixture into a
# publication authority through this tool.
_FORBIDDEN_TEST_KEY_IDS = frozenset({"test-catalog-2026-a"})
_FORBIDDEN_TEST_PUBLIC_KEYS = frozenset(
    {"ebVWLo_mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ"}
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CeremonyError(RuntimeError):
    """A fixed-code failure that does not disclose key material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TrustKeyDocument:
    catalog_id: str
    publisher: str
    key: TrustedCatalogKey
    public_key_base64url: str


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _safe_read(
    path: Path,
    *,
    maximum: int,
    private: bool = False,
) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError:
        raise CeremonyError("ceremony_input_unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size > maximum
    ):
        raise CeremonyError("ceremony_input_invalid")
    if private and (
        metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
    ):
        raise CeremonyError("ceremony_private_permissions_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size > maximum
            ):
                raise CeremonyError("ceremony_input_invalid")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > maximum:
                raise CeremonyError("ceremony_input_invalid")
            return payload
        finally:
            os.close(descriptor)
    except CeremonyError:
        raise
    except OSError:
        raise CeremonyError("ceremony_input_unavailable") from None


def _prepare_new_output(path: Path, *, private: bool) -> Path:
    path = Path(path)
    try:
        requested_parent = Path(os.path.abspath(path.parent))
        parent = path.parent.resolve(strict=True)
        parent_metadata = parent.lstat()
    except OSError:
        raise CeremonyError("ceremony_output_parent_invalid") from None
    if (
        (private and requested_parent != parent)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
    ):
        raise CeremonyError("ceremony_output_parent_invalid")
    if private:
        try:
            parent.relative_to(_REPOSITORY_ROOT)
        except ValueError:
            pass
        else:
            raise CeremonyError("ceremony_private_key_in_repository")
        if parent_metadata.st_uid != os.getuid() or parent_metadata.st_mode & 0o022:
            raise CeremonyError("ceremony_private_parent_permissions_invalid")
    destination = parent / path.name
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError:
        raise CeremonyError("ceremony_output_unavailable") from None
    raise CeremonyError("ceremony_output_exists")


def _write_new(path: Path, payload: bytes, *, mode: int, private: bool) -> None:
    destination = _prepare_new_output(path, private=private)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    succeeded = False
    try:
        descriptor = os.open(destination, flags, mode)
        created = True
        os.fchmod(descriptor, mode)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o777 != mode
        ):
            raise CeremonyError(
                "ceremony_private_permissions_invalid"
                if private
                else "ceremony_output_permissions_invalid"
            )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        succeeded = True
    except CeremonyError:
        raise
    except OSError:
        raise CeremonyError("ceremony_output_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not succeeded:
            try:
                destination.unlink()
            except OSError:
                pass


def _parse_json_file(path: Path, *, maximum: int) -> dict[str, Any]:
    try:
        return parse_catalog_json(_safe_read(path, maximum=maximum))
    except CatalogError as exc:
        raise CeremonyError("ceremony_json_invalid") from exc


def _validate_passphrase(passphrase: bytes) -> bytes:
    if (
        not MIN_PASSPHRASE_BYTES <= len(passphrase) <= MAX_PASSPHRASE_BYTES
        or b"\x00" in passphrase
        or b"\r" in passphrase
        or b"\n" in passphrase
    ):
        raise CeremonyError("ceremony_passphrase_invalid")
    return passphrase


def _read_passphrase_file(path: Path) -> bytes:
    value = _safe_read(path, maximum=MAX_PASSPHRASE_BYTES + 2, private=True)
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    return _validate_passphrase(value)


def _prompt_passphrase(*, confirm: bool) -> bytes:
    try:
        first = getpass.getpass("Catalog private-key passphrase: ").encode("utf-8")
        if confirm:
            second = getpass.getpass("Confirm passphrase: ").encode("utf-8")
            if first != second:
                raise CeremonyError("ceremony_passphrase_mismatch")
    except (EOFError, KeyboardInterrupt, UnicodeError):
        raise CeremonyError("ceremony_passphrase_unavailable") from None
    return _validate_passphrase(first)


def _trust_key_payload(
    *,
    key_id: str,
    public_key: bytes,
    valid_from: int,
    valid_until: int,
    minimum_catalog_sequence: int,
    maximum_catalog_sequence: int,
) -> dict[str, Any]:
    numeric_values = (
        valid_from,
        valid_until,
        minimum_catalog_sequence,
        maximum_catalog_sequence,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric_values):
        raise CeremonyError("ceremony_trust_key_invalid")
    encoded = _base64url(public_key)
    try:
        key = TrustedCatalogKey.from_base64url(
            key_id=key_id,
            public_key=encoded,
            valid_from=valid_from,
            valid_until=valid_until,
            minimum_catalog_sequence=minimum_catalog_sequence,
            maximum_catalog_sequence=maximum_catalog_sequence,
        )
    except (CatalogError, TypeError, ValueError):
        raise CeremonyError("ceremony_trust_key_invalid") from None
    _reject_test_key(key.key_id, encoded)
    return {
        "schema_version": CEREMONY_SCHEMA_VERSION,
        "catalog_id": BUILT_IN_CATALOG_ID,
        "publisher": BUILT_IN_PUBLISHER,
        "key_id": key.key_id,
        "algorithm": "Ed25519",
        "public_key": encoded,
        "public_key_digest": _sha256(public_key),
        "valid_from": key.valid_from,
        "valid_until": key.valid_until,
        "minimum_catalog_sequence": key.minimum_catalog_sequence,
        "maximum_catalog_sequence": key.maximum_catalog_sequence,
    }


def generate_key(
    *,
    private_key_path: Path,
    trust_key_path: Path,
    key_id: str,
    passphrase: bytes,
    valid_from: int = 0,
    valid_until: int = MAX_TIMESTAMP,
    minimum_catalog_sequence: int = 1,
    maximum_catalog_sequence: int = MAX_SEQUENCE,
) -> dict[str, Any]:
    """Create one encrypted offline key and its public trust document."""

    passphrase = _validate_passphrase(passphrase)
    private_destination = _prepare_new_output(private_key_path, private=True)
    trust_destination = _prepare_new_output(trust_key_path, private=False)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trust_payload = _trust_key_payload(
        key_id=key_id,
        public_key=public_key,
        valid_from=valid_from,
        valid_until=valid_until,
        minimum_catalog_sequence=minimum_catalog_sequence,
        maximum_catalog_sequence=maximum_catalog_sequence,
    )
    encrypted = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    _write_new(private_destination, encrypted, mode=0o600, private=True)
    _write_new(
        trust_destination,
        canonical_json(trust_payload),
        mode=0o644,
        private=False,
    )
    return trust_payload


def _reject_test_key(key_id: str, encoded_public_key: str) -> None:
    if (
        key_id.startswith("test-")
        or key_id in _FORBIDDEN_TEST_KEY_IDS
        or encoded_public_key in _FORBIDDEN_TEST_PUBLIC_KEYS
    ):
        raise CeremonyError("ceremony_test_key_forbidden")


def load_trust_key(path: Path) -> TrustKeyDocument:
    value = _parse_json_file(path, maximum=MAX_TRUST_KEY_BYTES)
    expected = {
        "schema_version",
        "catalog_id",
        "publisher",
        "key_id",
        "algorithm",
        "public_key",
        "public_key_digest",
        "valid_from",
        "valid_until",
        "minimum_catalog_sequence",
        "maximum_catalog_sequence",
    }
    if set(value) != expected:
        raise CeremonyError("ceremony_trust_key_invalid")
    if (
        value.get("schema_version") != CEREMONY_SCHEMA_VERSION
        or value.get("catalog_id") != BUILT_IN_CATALOG_ID
        or value.get("publisher") != BUILT_IN_PUBLISHER
        or value.get("algorithm") != "Ed25519"
        or not isinstance(value.get("public_key"), str)
        or any(
            isinstance(value.get(field), bool)
            or not isinstance(value.get(field), int)
            for field in (
                "valid_from",
                "valid_until",
                "minimum_catalog_sequence",
                "maximum_catalog_sequence",
            )
        )
    ):
        raise CeremonyError("ceremony_trust_key_invalid")
    try:
        key = TrustedCatalogKey.from_base64url(
            key_id=value["key_id"],
            public_key=value["public_key"],
            valid_from=value["valid_from"],
            valid_until=value["valid_until"],
            minimum_catalog_sequence=value["minimum_catalog_sequence"],
            maximum_catalog_sequence=value["maximum_catalog_sequence"],
        )
    except (CatalogError, TypeError, ValueError, KeyError):
        raise CeremonyError("ceremony_trust_key_invalid") from None
    if value["public_key_digest"] != _sha256(key.public_key):
        raise CeremonyError("ceremony_trust_key_invalid")
    _reject_test_key(key.key_id, value["public_key"])
    return TrustKeyDocument(
        catalog_id=value["catalog_id"],
        publisher=value["publisher"],
        key=key,
        public_key_base64url=value["public_key"],
    )


def load_trust_keys(paths: Iterable[Path]) -> dict[str, TrustedCatalogKey]:
    result: dict[str, TrustedCatalogKey] = {}
    for path in paths:
        document = load_trust_key(path)
        if document.key.key_id in result:
            raise CeremonyError("ceremony_trust_key_duplicate")
        result[document.key.key_id] = document.key
    if not result:
        raise CeremonyError("ceremony_trust_key_required")
    return result


def _load_private_key(path: Path, passphrase: bytes) -> Ed25519PrivateKey:
    try:
        resolved = Path(path).resolve(strict=True)
        resolved.relative_to(_REPOSITORY_ROOT)
    except OSError:
        raise CeremonyError("ceremony_input_unavailable") from None
    except ValueError:
        pass
    else:
        raise CeremonyError("ceremony_private_key_in_repository")
    raw = _safe_read(path, maximum=MAX_PRIVATE_KEY_BYTES, private=True)
    try:
        key = serialization.load_pem_private_key(
            raw,
            password=_validate_passphrase(passphrase),
        )
    except (TypeError, ValueError):
        raise CeremonyError("ceremony_private_key_decryption_failed") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise CeremonyError("ceremony_private_key_invalid")
    return key


def _load_catalog_body(path: Path) -> dict[str, Any]:
    value = _parse_json_file(path, maximum=MAX_CATALOG_BYTES)
    if set(value) == {"schema_version", "catalog_digest", "catalog", "signatures"}:
        raise CeremonyError("ceremony_catalog_body_required")
    return value


def create_detached_signature(
    *,
    catalog_path: Path,
    private_key_path: Path,
    trust_key_path: Path,
    signature_path: Path,
    passphrase: bytes,
) -> dict[str, Any]:
    """Sign a catalog body and emit a digest-bound detached signature."""

    catalog = _load_catalog_body(catalog_path)
    trust = load_trust_key(trust_key_path)
    private_key = _load_private_key(private_key_path, passphrase)
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if public != trust.key.public_key:
        raise CeremonyError("ceremony_private_key_mismatch")
    try:
        digest = catalog_digest(catalog)
        signature = _base64url(private_key.sign(signing_message(catalog)))
        envelope = {
            "schema_version": 1,
            "catalog_digest": digest,
            "catalog": catalog,
            "signatures": [
                {
                    "key_id": trust.key.key_id,
                    "algorithm": "Ed25519",
                    "signature": signature,
                }
            ],
        }
        verified = CatalogVerifier({trust.key.key_id: trust.key}).verify(
            envelope,
            now=catalog["issued_at"],
        )
    except (CatalogError, KeyError, TypeError, ValueError):
        raise CeremonyError("ceremony_catalog_invalid") from None
    detached = {
        "schema_version": CEREMONY_SCHEMA_VERSION,
        "catalog_id": verified.catalog_id,
        "catalog_version": verified.catalog_version,
        "catalog_sequence": verified.catalog_sequence,
        "catalog_digest": verified.catalog_digest,
        "key_id": trust.key.key_id,
        "algorithm": "Ed25519",
        "signature": signature,
    }
    _write_new(
        signature_path,
        canonical_json(detached),
        mode=0o644,
        private=False,
    )
    return detached


def _load_detached_signature(path: Path) -> dict[str, Any]:
    value = _parse_json_file(path, maximum=MAX_SIGNATURE_BYTES)
    if set(value) != {
        "schema_version",
        "catalog_id",
        "catalog_version",
        "catalog_sequence",
        "catalog_digest",
        "key_id",
        "algorithm",
        "signature",
    } or value.get("schema_version") != 1 or value.get("algorithm") != "Ed25519":
        raise CeremonyError("ceremony_signature_invalid")
    return value


def _assemble_envelope(
    *,
    catalog: Mapping[str, Any],
    detached_signatures: Iterable[Mapping[str, Any]],
    trusted_keys: Mapping[str, TrustedCatalogKey],
) -> tuple[dict[str, Any], Any]:
    digest = catalog_digest(catalog)
    expected = (
        catalog.get("catalog_id"),
        catalog.get("catalog_version"),
        catalog.get("catalog_sequence"),
        digest,
    )
    signatures: list[dict[str, Any]] = []
    for row in detached_signatures:
        if (
            (
                row.get("catalog_id"),
                row.get("catalog_version"),
                row.get("catalog_sequence"),
                row.get("catalog_digest"),
            )
            != expected
        ):
            raise CeremonyError("ceremony_signature_catalog_mismatch")
        signatures.append(
            {
                "key_id": row.get("key_id"),
                "algorithm": row.get("algorithm"),
                "signature": row.get("signature"),
            }
        )
    signatures.sort(key=lambda item: str(item["key_id"]))
    if not signatures:
        raise CeremonyError("ceremony_signature_required")
    envelope = {
        "schema_version": 1,
        "catalog_digest": digest,
        "catalog": dict(catalog),
        "signatures": signatures,
    }
    try:
        verified = CatalogVerifier(trusted_keys).verify(
            envelope,
            now=catalog["issued_at"],
        )
    except (CatalogError, KeyError, TypeError, ValueError):
        raise CeremonyError("ceremony_envelope_invalid") from None
    signature_ids = tuple(row["key_id"] for row in signatures)
    if verified.signing_key_ids != signature_ids:
        raise CeremonyError("ceremony_signature_untrusted")
    return envelope, verified


def assemble_envelope(
    *,
    catalog_path: Path,
    signature_paths: Iterable[Path],
    trust_key_paths: Iterable[Path],
    output_path: Path,
) -> dict[str, Any]:
    catalog = _load_catalog_body(catalog_path)
    detached = [_load_detached_signature(path) for path in signature_paths]
    trusted = load_trust_keys(trust_key_paths)
    envelope, verified = _assemble_envelope(
        catalog=catalog,
        detached_signatures=detached,
        trusted_keys=trusted,
    )
    payload = canonical_json(envelope)
    _write_new(output_path, payload, mode=0o644, private=False)
    return _receipt(verified, payload, previous_checked=False)


def verify_publication(
    *,
    envelope_path: Path,
    trust_key_paths: Iterable[Path],
    now: int | None = None,
    required_key_ids: Iterable[str] = (),
    previous_envelope_path: Path | None = None,
    expected_envelope_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify exact publication bytes, signatures, and an optional rollback base."""

    trusted = load_trust_keys(trust_key_paths)
    raw = _safe_read(envelope_path, maximum=MAX_CATALOG_BYTES)
    try:
        verified = CatalogVerifier(trusted).verify(raw, now=now)
        parsed = parse_catalog_json(raw)
    except CatalogError as exc:
        raise CeremonyError("ceremony_publication_invalid") from exc
    signature_ids = tuple(row["key_id"] for row in parsed["signatures"])
    if verified.signing_key_ids != signature_ids:
        raise CeremonyError("ceremony_signature_untrusted")
    required = tuple(sorted(set(required_key_ids)))
    if any(key_id not in verified.signing_key_ids for key_id in required):
        raise CeremonyError("ceremony_required_signature_missing")
    envelope_sha256 = _sha256(raw)
    if expected_envelope_sha256 is not None and expected_envelope_sha256 != envelope_sha256:
        raise CeremonyError("ceremony_publication_digest_mismatch")

    previous_checked = False
    if previous_envelope_path is not None:
        previous_raw = _safe_read(previous_envelope_path, maximum=MAX_CATALOG_BYTES)
        try:
            previous_value = parse_catalog_json(previous_raw)
            previous_issued_at = previous_value["catalog"]["issued_at"]
            previous = CatalogVerifier(trusted).verify(
                previous_raw,
                now=previous_issued_at,
            )
        except (CatalogError, KeyError, TypeError, ValueError):
            raise CeremonyError("ceremony_previous_publication_invalid") from None
        previous_signature_ids = tuple(
            row["key_id"] for row in previous_value["signatures"]
        )
        if previous.signing_key_ids != previous_signature_ids:
            raise CeremonyError("ceremony_previous_publication_invalid")
        if verified.catalog_sequence < previous.catalog_sequence:
            raise CeremonyError("ceremony_publication_downgrade")
        if (
            verified.catalog_sequence == previous.catalog_sequence
            and verified.catalog_digest != previous.catalog_digest
        ):
            raise CeremonyError("ceremony_publication_conflict")
        previous_checked = True
    return _receipt(
        verified,
        raw,
        previous_checked=previous_checked,
        envelope_sha256=envelope_sha256,
    )


def _receipt(
    verified: Any,
    raw: bytes,
    *,
    previous_checked: bool,
    envelope_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": CEREMONY_SCHEMA_VERSION,
        "outcome": "verified",
        "catalog_id": verified.catalog_id,
        "catalog_version": verified.catalog_version,
        "catalog_sequence": verified.catalog_sequence,
        "issued_at": verified.issued_at,
        "expires_at": verified.expires_at,
        "catalog_digest": verified.catalog_digest,
        "envelope_sha256": envelope_sha256 or _sha256(raw),
        "signing_key_ids": list(verified.signing_key_ids),
        "previous_publication_checked": previous_checked,
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _add_passphrase_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        help="mode-0600 file; omit to use a no-echo terminal prompt",
    )


def _passphrase(arguments: argparse.Namespace, *, confirm: bool) -> bytes:
    if arguments.passphrase_file is not None:
        return _read_passphrase_file(arguments.passphrase_file)
    return _prompt_passphrase(confirm=confirm)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Mnemosyne compatibility-catalog ceremony",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("generate-key")
    keygen.add_argument("--private-key", type=Path, required=True)
    keygen.add_argument("--trust-key", type=Path, required=True)
    keygen.add_argument("--key-id", required=True)
    keygen.add_argument("--valid-from", type=_positive_int, default=0)
    keygen.add_argument("--valid-until", type=_positive_int, default=MAX_TIMESTAMP)
    keygen.add_argument("--minimum-catalog-sequence", type=_positive_int, default=1)
    keygen.add_argument(
        "--maximum-catalog-sequence",
        type=_positive_int,
        default=MAX_SEQUENCE,
    )
    _add_passphrase_argument(keygen)

    sign = subparsers.add_parser("sign")
    sign.add_argument("--catalog", type=Path, required=True)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--trust-key", type=Path, required=True)
    sign.add_argument("--signature", type=Path, required=True)
    _add_passphrase_argument(sign)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--catalog", type=Path, required=True)
    assemble.add_argument("--signature", type=Path, action="append", required=True)
    assemble.add_argument("--trust-key", type=Path, action="append", required=True)
    assemble.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--envelope", type=Path, required=True)
    verify.add_argument("--trust-key", type=Path, action="append", required=True)
    verify.add_argument("--required-key-id", action="append", default=[])
    verify.add_argument("--previous-envelope", type=Path)
    verify.add_argument("--expected-envelope-sha256")
    verify.add_argument("--now", type=_positive_int)
    verify.add_argument("--receipt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate-key":
            result = generate_key(
                private_key_path=arguments.private_key,
                trust_key_path=arguments.trust_key,
                key_id=arguments.key_id,
                passphrase=_passphrase(arguments, confirm=True),
                valid_from=arguments.valid_from,
                valid_until=arguments.valid_until,
                minimum_catalog_sequence=arguments.minimum_catalog_sequence,
                maximum_catalog_sequence=arguments.maximum_catalog_sequence,
            )
        elif arguments.command == "sign":
            result = create_detached_signature(
                catalog_path=arguments.catalog,
                private_key_path=arguments.private_key,
                trust_key_path=arguments.trust_key,
                signature_path=arguments.signature,
                passphrase=_passphrase(arguments, confirm=False),
            )
        elif arguments.command == "assemble":
            result = assemble_envelope(
                catalog_path=arguments.catalog,
                signature_paths=arguments.signature,
                trust_key_paths=arguments.trust_key,
                output_path=arguments.output,
            )
        else:
            result = verify_publication(
                envelope_path=arguments.envelope,
                trust_key_paths=arguments.trust_key,
                now=arguments.now,
                required_key_ids=arguments.required_key_id,
                previous_envelope_path=arguments.previous_envelope,
                expected_envelope_sha256=arguments.expected_envelope_sha256,
            )
            if arguments.receipt is not None:
                _write_new(
                    arguments.receipt,
                    canonical_json(result),
                    mode=0o644,
                    private=False,
                )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (CeremonyError, CatalogError) as exc:
        code = getattr(exc, "code", "ceremony_failed")
        print(code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CeremonyError",
    "TrustKeyDocument",
    "assemble_envelope",
    "create_detached_signature",
    "generate_key",
    "load_trust_key",
    "load_trust_keys",
    "main",
    "verify_publication",
]

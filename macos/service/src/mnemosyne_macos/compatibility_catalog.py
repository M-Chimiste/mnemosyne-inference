"""Pure verification and last-known-good storage for signed catalogs.

This file is vendored byte-for-byte into the independent Fleet and native Mac
packages. Keep it free of service, networking, engine, and downloader imports.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import fcntl
import hashlib
import importlib.resources
import json
import os
from pathlib import Path
import stat
import time
from types import MappingProxyType
from typing import Any, Final, Literal, Mapping
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError


CATALOG_SCHEMA_VERSION: Final[int] = 1
MAX_CATALOG_BYTES: Final[int] = 8 * 1024 * 1024
MAX_CATALOG_VALIDITY_SECONDS: Final[int] = 366 * 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS: Final[int] = 5 * 60
SIGNATURE_DOMAIN: Final[bytes] = b"mnemosyne-compatibility-catalog-v1\x00"
BUILT_IN_CATALOG_ID: Final[str] = "mnemosyne-apple-silicon"
BUILT_IN_PUBLISHER: Final[str] = "mnemosyne"
_MAX_SEQUENCE: Final[int] = 9_007_199_254_740_991
_MAX_TIMESTAMP: Final[int] = 4_102_444_800


class CatalogError(RuntimeError):
    """A fixed-code error that never includes signed or caller-provided data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CatalogParseError(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


class CatalogTrustError(CatalogError):
    pass


class CatalogRollbackError(CatalogError):
    pass


class CatalogStoreError(CatalogError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedCatalogKey:
    """One locally pinned Ed25519 key and its rotation validity window."""

    key_id: str
    public_key: bytes = field(repr=False)
    valid_from: int = 0
    valid_until: int = _MAX_TIMESTAMP
    minimum_catalog_sequence: int = 1
    maximum_catalog_sequence: int = _MAX_SEQUENCE

    def __post_init__(self) -> None:
        if not _safe_identifier(self.key_id) or len(self.public_key) != 32:
            raise ValueError("invalid trusted catalog key")
        if not (
            0 <= self.valid_from < self.valid_until <= _MAX_TIMESTAMP
            and 1 <= self.minimum_catalog_sequence
            <= self.maximum_catalog_sequence
            <= _MAX_SEQUENCE
        ):
            raise ValueError("invalid trusted catalog key window")
        try:
            Ed25519PublicKey.from_public_bytes(self.public_key)
        except ValueError as exc:
            raise ValueError("invalid trusted catalog key") from exc

    @classmethod
    def from_base64url(
        cls,
        *,
        key_id: str,
        public_key: str,
        valid_from: int = 0,
        valid_until: int = _MAX_TIMESTAMP,
        minimum_catalog_sequence: int = 1,
        maximum_catalog_sequence: int = _MAX_SEQUENCE,
    ) -> "TrustedCatalogKey":
        return cls(
            key_id=key_id,
            public_key=_decode_base64url(
                public_key,
                expected_size=32,
                code="catalog_invalid_trust_key",
            ),
            valid_from=valid_from,
            valid_until=valid_until,
            minimum_catalog_sequence=minimum_catalog_sequence,
            maximum_catalog_sequence=maximum_catalog_sequence,
        )


@dataclass(frozen=True, slots=True)
class VerifiedCatalog:
    """Immutable verified metadata plus canonical bytes for safe persistence."""

    schema_version: int
    catalog_id: str
    catalog_version: str
    catalog_sequence: int
    issued_at: int
    expires_at: int | None
    catalog_digest: str
    signing_key_ids: tuple[str, ...]
    source: Literal["signed", "built_in"]
    canonical_catalog: bytes = field(repr=False)
    canonical_envelope: bytes = field(repr=False)

    def catalog(self) -> dict[str, Any]:
        return json.loads(self.canonical_catalog)

    def envelope(self) -> dict[str, Any]:
        return json.loads(self.canonical_envelope)


@dataclass(frozen=True, slots=True)
class CatalogActivation:
    catalog: VerifiedCatalog
    changed: bool


def canonical_json(value: Any) -> bytes:
    """RFC-8259 JSON with deterministic key ordering and no insignificant bytes."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CatalogParseError("catalog_invalid_json") from exc


def catalog_digest(catalog: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(catalog)).hexdigest()


def artifact_manifest_digest(files: list[Mapping[str, Any]]) -> str:
    manifest = [
        {
            "path": item["path"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        for item in files
    ]
    return "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()


def signing_message(catalog: Mapping[str, Any]) -> bytes:
    return SIGNATURE_DOMAIN + canonical_json(catalog)


def parse_catalog_json(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeError as exc:
            raise CatalogParseError("catalog_invalid_json") from exc
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise CatalogParseError("catalog_invalid_json")
    if len(encoded) > MAX_CATALOG_BYTES:
        raise CatalogParseError("catalog_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate member")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise CatalogParseError("catalog_invalid_json") from None
    if not isinstance(value, dict):
        raise CatalogParseError("catalog_invalid_json")
    return value


class CatalogVerifier:
    """Verify a closed catalog envelope against locally pinned public keys."""

    def __init__(
        self,
        trusted_keys: Mapping[str, TrustedCatalogKey],
        *,
        expected_catalog_id: str = BUILT_IN_CATALOG_ID,
        expected_publisher: str = BUILT_IN_PUBLISHER,
    ) -> None:
        if not _safe_identifier(expected_catalog_id) or not _safe_identifier(
            expected_publisher
        ):
            raise ValueError("invalid expected catalog identity")
        normalized: dict[str, TrustedCatalogKey] = {}
        for key_id, key in trusted_keys.items():
            if key_id != key.key_id or key_id in normalized:
                raise ValueError("invalid trusted catalog key map")
            normalized[key_id] = key
        self._trusted_keys = MappingProxyType(normalized)
        self._expected_catalog_id = expected_catalog_id
        self._expected_publisher = expected_publisher
        self._validator = Draft202012Validator(_load_schema())

    def verify(
        self,
        raw: bytes | str | Mapping[str, Any],
        *,
        now: int | float | None = None,
    ) -> VerifiedCatalog:
        if isinstance(raw, Mapping):
            value = parse_catalog_json(canonical_json(dict(raw)))
        else:
            value = parse_catalog_json(raw)
        try:
            self._validator.validate(value)
        except SchemaValidationError:
            raise CatalogValidationError("catalog_schema_invalid") from None

        catalog = value["catalog"]
        canonical_catalog = canonical_json(catalog)
        digest = "sha256:" + hashlib.sha256(canonical_catalog).hexdigest()
        if value["catalog_digest"] != digest:
            raise CatalogTrustError("catalog_digest_mismatch")

        self._validate_content(catalog)
        signature_key_ids = [row["key_id"] for row in value["signatures"]]
        if signature_key_ids != sorted(set(signature_key_ids)):
            raise CatalogValidationError("catalog_order_invalid")
        timestamp = _coerce_now(now)
        issued_at = int(catalog["issued_at"])
        expires_at = int(catalog["expires_at"])
        if timestamp + MAX_CLOCK_SKEW_SECONDS < issued_at:
            raise CatalogTrustError("catalog_not_yet_valid")
        if timestamp >= expires_at:
            raise CatalogTrustError("catalog_expired")

        sequence = int(catalog["catalog_sequence"])
        recognized = False
        valid_signers: list[str] = []
        message = SIGNATURE_DOMAIN + canonical_catalog
        for signature in value["signatures"]:
            key = self._trusted_keys.get(signature["key_id"])
            if key is None or not (
                key.valid_from <= issued_at < key.valid_until
                and key.minimum_catalog_sequence
                <= sequence
                <= key.maximum_catalog_sequence
            ):
                continue
            recognized = True
            signature_bytes = _decode_base64url(
                signature["signature"],
                expected_size=64,
                code="catalog_signature_invalid",
            )
            try:
                Ed25519PublicKey.from_public_bytes(key.public_key).verify(
                    signature_bytes,
                    message,
                )
            except InvalidSignature:
                continue
            valid_signers.append(key.key_id)
        if not recognized:
            raise CatalogTrustError("catalog_unknown_key")
        if not valid_signers:
            raise CatalogTrustError("catalog_signature_invalid")

        canonical_envelope = canonical_json(value)
        if len(canonical_envelope) > MAX_CATALOG_BYTES:
            raise CatalogParseError("catalog_too_large")
        return VerifiedCatalog(
            schema_version=CATALOG_SCHEMA_VERSION,
            catalog_id=catalog["catalog_id"],
            catalog_version=catalog["catalog_version"],
            catalog_sequence=sequence,
            issued_at=issued_at,
            expires_at=expires_at,
            catalog_digest=digest,
            signing_key_ids=tuple(sorted(valid_signers)),
            source="signed",
            canonical_catalog=canonical_catalog,
            canonical_envelope=canonical_envelope,
        )

    def _validate_content(self, catalog: dict[str, Any]) -> None:
        if (
            catalog["catalog_id"] != self._expected_catalog_id
            or catalog["publisher"] != self._expected_publisher
        ):
            raise CatalogValidationError("catalog_identity_mismatch")
        issued_at = catalog["issued_at"]
        expires_at = catalog["expires_at"]
        if not (
            issued_at < expires_at
            and expires_at - issued_at <= MAX_CATALOG_VALIDITY_SECONDS
        ):
            raise CatalogValidationError("catalog_validity_invalid")

        logical_models = catalog["logical_models"]
        artifacts = catalog["artifacts"]
        recipes = catalog["recipes"]
        _require_canonical_rows(logical_models, "logical_model_id")
        _require_canonical_rows(artifacts, "artifact_id")
        _require_canonical_rows(recipes, "recipe_id")
        models_by_id = {row["logical_model_id"]: row for row in logical_models}
        artifacts_by_id = {row["artifact_id"]: row for row in artifacts}

        for model in logical_models:
            _require_sorted_unique(model["capabilities"])
        for artifact in artifacts:
            model = models_by_id.get(artifact["logical_model_id"])
            if model is None:
                raise CatalogValidationError("catalog_reference_invalid")
            files = artifact["files"]
            if [row["path"] for row in files] != sorted(
                {row["path"] for row in files}
            ) or any(not _safe_relative_path(row["path"]) for row in files):
                raise CatalogValidationError("catalog_order_invalid")
            if artifact["total_size_bytes"] != sum(
                row["size_bytes"] for row in files
            ):
                raise CatalogValidationError("catalog_artifact_size_invalid")
            if artifact["manifest_digest"] != artifact_manifest_digest(files):
                raise CatalogValidationError("catalog_manifest_digest_invalid")
            _validate_gguf_layout(artifact, model)

        allowed_formats = {
            "llama.cpp": "gguf",
            "omlx": "mlx",
            "ds4": "ds4-weights",
        }
        for recipe in recipes:
            _require_sorted_unique(recipe["capabilities"])
            _require_sorted_unique(recipe["hardware"]["soc_families"])
            _require_sorted_unique(recipe["hardware"]["required_features"])
            runtime = recipe["runtime"]
            _require_sorted_unique(runtime["known_bad_versions"])
            _require_sorted_unique(runtime["allowed_runtime_fingerprints"])
            _require_sorted_unique(runtime["known_bad_runtime_fingerprints"])
            _require_sorted_unique(runtime["required_features"])
            if set(runtime["allowed_runtime_fingerprints"]) & set(
                runtime["known_bad_runtime_fingerprints"]
            ):
                raise CatalogValidationError("catalog_runtime_constraint_invalid")

            model = models_by_id.get(recipe["logical_model_id"])
            artifact = artifacts_by_id.get(recipe["artifact_id"])
            if (
                model is None
                or artifact is None
                or artifact["logical_model_id"] != recipe["logical_model_id"]
                or not set(recipe["capabilities"]).issubset(
                    model["capabilities"]
                )
            ):
                raise CatalogValidationError("catalog_reference_invalid")
            engine = recipe["engine"]
            if (
                runtime["engine"] != engine
                or recipe["launch"]["engine"] != engine
                or artifact["format"] != allowed_formats[engine]
            ):
                raise CatalogValidationError("catalog_recipe_invalid")
            context = recipe["context"]
            declared = model["declared_max_context_tokens"]
            native = context["native_max_tokens"]
            if (
                (declared is not None and context["guaranteed_tokens"] > declared)
                or (native is not None and context["guaranteed_tokens"] > native)
            ):
                raise CatalogValidationError("catalog_context_invalid")
            memory = recipe["memory"]
            if memory["weights_bytes"] < artifact["total_size_bytes"]:
                raise CatalogValidationError("catalog_memory_invalid")
            if memory["estimate_class"] != memory["evidence"]["evidence_class"]:
                raise CatalogValidationError("catalog_memory_invalid")
            slots = {
                "llama.cpp": recipe["launch"].get("parallel_slots"),
                "omlx": recipe["launch"].get("scheduler_slots"),
                "ds4": recipe["launch"].get("batched_sessions"),
            }[engine]
            if slots > memory["max_recommended_concurrency"]:
                raise CatalogValidationError("catalog_memory_invalid")
            for evidence in (
                recipe["evidence"],
                context["evidence"],
                memory["evidence"],
            ):
                observed = evidence["observed_at"]
                evidence_expiry = evidence["expires_at"]
                if (
                    observed is not None
                    and evidence_expiry is not None
                    and evidence_expiry <= observed
                ):
                    raise CatalogValidationError("catalog_evidence_invalid")


def _validate_gguf_layout(
    artifact: Mapping[str, Any],
    model: Mapping[str, Any],
) -> None:
    """Validate one exact, signed GGUF launch selection when present.

    Version-1 artifacts without this additive member retain their original
    meaning.  A layout is an exact file set: one launch file, every required
    sibling shard, and either one already-selected projector or no projector.
    Alternative projectors therefore use distinct signed artifacts/recipes;
    neither Fleet nor a Mac chooses a filename by convention.

    The content-only ``manifest_digest`` intentionally remains compatible
    with filesystem ownership proofs.  Layout authority is covered by the
    catalog digest/signature and by the executor's signed source identity.
    """

    layout = artifact.get("gguf_layout")
    if layout is None:
        if (
            artifact.get("format") in {"gguf", "ds4-weights"}
            and len(artifact.get("files", ())) != 1
        ):
            raise CatalogValidationError("catalog_artifact_layout_invalid")
        return
    try:
        primary = layout["primary_file"]
        shards = layout["required_shards"]
        projector = layout["selected_projector_file"]
        if (
            artifact["format"] not in {"gguf", "ds4-weights"}
            or layout["kind"] != "gguf-file-set"
            or not isinstance(primary, str)
            or not isinstance(shards, list)
            or any(not isinstance(item, str) for item in shards)
            or shards != sorted(set(shards))
            or (projector is not None and not isinstance(projector, str))
        ):
            raise ValueError
        selected = [primary, *shards]
        if projector is not None:
            selected.append(projector)
        file_paths = [item["path"] for item in artifact["files"]]
        if (
            len(selected) != len(set(selected))
            or set(selected) != set(file_paths)
            or any(not path.casefold().endswith(".gguf") for path in selected)
            or (
                projector is not None
                and (
                    artifact["format"] != "gguf"
                    or model["kind"] != "generation"
                )
            )
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CatalogValidationError("catalog_artifact_layout_invalid") from None

class CatalogStore:
    """Private atomic store for one signed active catalog and rollback fence."""

    def __init__(self, directory: Path, verifier: CatalogVerifier) -> None:
        self._directory = Path(directory)
        self._verifier = verifier
        self._active = self._directory / "active.catalog.json"
        self._previous = self._directory / "previous.catalog.json"
        self._state = self._directory / "catalog.state.json"
        self._lock = self._directory / "catalog.lock"

    def load(self, *, now: int | float | None = None) -> VerifiedCatalog:
        self._prepare_directory()
        with self._locked():
            watermark = self._read_watermark()
            candidate = self._read_verified(self._active, now=now)
            if candidate is None:
                return built_in_empty_catalog()
            if not _catalog_matches_watermark(candidate, watermark):
                return built_in_empty_catalog()
            if candidate.catalog_sequence > watermark[0]:
                self._write_watermark(candidate)
            return candidate

    def activate(
        self,
        raw: bytes | str | Mapping[str, Any],
        *,
        now: int | float | None = None,
    ) -> CatalogActivation:
        candidate = self._verifier.verify(raw, now=now)
        self._prepare_directory()
        with self._locked():
            watermark = self._read_watermark()
            current = self._read_verified(self._active, now=now)
            if current is not None and current.catalog_sequence > watermark[0]:
                watermark = (
                    current.catalog_sequence,
                    current.catalog_digest,
                )
            if candidate.catalog_sequence < watermark[0]:
                raise CatalogRollbackError("catalog_downgrade_rejected")
            if (
                candidate.catalog_sequence == watermark[0]
                and watermark[1] is not None
                and candidate.catalog_digest != watermark[1]
            ):
                raise CatalogRollbackError("catalog_version_conflict")
            if (
                current is not None
                and current.catalog_sequence == candidate.catalog_sequence
                and current.catalog_digest == candidate.catalog_digest
            ):
                return CatalogActivation(catalog=current, changed=False)

            if current is not None and _catalog_matches_watermark(
                current, watermark
            ):
                self._atomic_write(self._previous, current.canonical_envelope)
            self._atomic_write(self._active, candidate.canonical_envelope)
            self._write_watermark(candidate)
            return CatalogActivation(catalog=candidate, changed=True)

    def _prepare_directory(self) -> None:
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self._directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CatalogStoreError("catalog_store_path_invalid")
            os.chmod(self._directory, 0o700)
        except CatalogStoreError:
            raise
        except OSError:
            raise CatalogStoreError("catalog_store_unavailable") from None

    def _locked(self):
        return _CatalogLock(self._lock)

    def _read_verified(
        self,
        path: Path,
        *,
        now: int | float | None,
    ) -> VerifiedCatalog | None:
        raw = self._read_bounded(path, maximum=MAX_CATALOG_BYTES)
        if raw is None:
            return None
        try:
            return self._verifier.verify(raw, now=now)
        except CatalogError:
            return None

    def _read_watermark(self) -> tuple[int, str | None]:
        raw = self._read_bounded(self._state, maximum=512)
        if raw is None:
            return (0, None)
        try:
            value = parse_catalog_json(raw)
        except CatalogError:
            raise CatalogStoreError("catalog_store_state_invalid") from None
        if set(value) != {"schema_version", "highest_sequence", "catalog_digest"}:
            raise CatalogStoreError("catalog_store_state_invalid")
        sequence = value.get("highest_sequence")
        digest = value.get("catalog_digest")
        if (
            value.get("schema_version") != 1
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence <= _MAX_SEQUENCE
            or (
                digest is not None
                and (
                    not isinstance(digest, str)
                    or len(digest) != 71
                    or not digest.startswith("sha256:")
                    or any(c not in "0123456789abcdef" for c in digest[7:])
                )
            )
            or (sequence == 0) != (digest is None)
        ):
            raise CatalogStoreError("catalog_store_state_invalid")
        return (sequence, digest)

    def _write_watermark(self, catalog: VerifiedCatalog) -> None:
        self._atomic_write(
            self._state,
            canonical_json(
                {
                    "schema_version": 1,
                    "highest_sequence": catalog.catalog_sequence,
                    "catalog_digest": catalog.catalog_digest,
                }
            ),
        )

    @staticmethod
    def _read_bounded(path: Path, *, maximum: int) -> bytes | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise CatalogStoreError("catalog_store_unavailable") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_size > maximum
        ):
            raise CatalogStoreError("catalog_store_path_invalid")
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
                    raise CatalogStoreError("catalog_store_path_invalid")
                chunks: list[bytes] = []
                remaining = maximum + 1
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if len(raw) > maximum:
                    raise CatalogStoreError("catalog_store_path_invalid")
                return raw
            finally:
                os.close(descriptor)
        except CatalogStoreError:
            raise
        except OSError:
            raise CatalogStoreError("catalog_store_unavailable") from None

    def _atomic_write(self, path: Path, data: bytes) -> None:
        temporary = self._directory / f".{path.name}.{uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise CatalogStoreError("catalog_store_write_failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


class _CatalogLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "_CatalogLock":
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(self._path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CatalogStoreError("catalog_store_path_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._descriptor = descriptor
            descriptor = None
            return self
        except CatalogStoreError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise CatalogStoreError("catalog_store_unavailable") from None

    def __exit__(self, _type, _value, _traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def built_in_empty_catalog() -> VerifiedCatalog:
    catalog = {
        "catalog_id": BUILT_IN_CATALOG_ID,
        "catalog_version": "offline-empty",
        "catalog_sequence": 0,
        "issued_at": 0,
        "expires_at": _MAX_TIMESTAMP,
        "publisher": BUILT_IN_PUBLISHER,
        "build_revision": "0" * 40,
        "logical_models": [],
        "artifacts": [],
        "recipes": [],
    }
    canonical_catalog = canonical_json(catalog)
    digest = "sha256:" + hashlib.sha256(canonical_catalog).hexdigest()
    envelope = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_digest": digest,
        "catalog": catalog,
        "signatures": [],
    }
    return VerifiedCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        catalog_id=BUILT_IN_CATALOG_ID,
        catalog_version="offline-empty",
        catalog_sequence=0,
        issued_at=0,
        expires_at=None,
        catalog_digest=digest,
        signing_key_ids=(),
        source="built_in",
        canonical_catalog=canonical_catalog,
        canonical_envelope=canonical_json(envelope),
    )


def _load_schema() -> dict[str, Any]:
    for package in ("mnemosyne_fleet", "mnemosyne_macos"):
        try:
            resource = importlib.resources.files(package).joinpath(
                "schemas", "compatibility_catalog.schema.json"
            )
            if resource.is_file():
                value = json.loads(resource.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(value)
                return value
        except (ModuleNotFoundError, FileNotFoundError):
            continue
    for parent in Path(__file__).resolve().parents:
        path = parent / "compatibility_catalog" / "v1" / "catalog.schema.json"
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(value)
            return value
    raise CatalogStoreError("catalog_schema_unavailable")


def _decode_base64url(value: str, *, expected_size: int, code: str) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise CatalogTrustError(code)
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise CatalogTrustError(code) from None
    if len(decoded) != expected_size or _base64url(decoded) != value:
        raise CatalogTrustError(code)
    return decoded


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _safe_identifier(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    return value[0].isalnum() and all(
        character.isascii() and (character.isalnum() or character in "._:+-")
        for character in value
    )


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return False
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    segments = value.split("/")
    return all(segment not in {"", ".", ".."} for segment in segments)


def _coerce_now(value: int | float | None) -> int:
    if value is None:
        value = time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a timestamp")
    result = int(value)
    if not 0 <= result <= _MAX_TIMESTAMP:
        raise ValueError("now must be a timestamp")
    return result


def _require_canonical_rows(rows: list[dict[str, Any]], key: str) -> None:
    identifiers = [row[key] for row in rows]
    if identifiers != sorted(set(identifiers)):
        raise CatalogValidationError("catalog_order_invalid")


def _require_sorted_unique(values: list[Any]) -> None:
    if values != sorted(set(values)):
        raise CatalogValidationError("catalog_order_invalid")


def _catalog_matches_watermark(
    catalog: VerifiedCatalog,
    watermark: tuple[int, str | None],
) -> bool:
    sequence, digest = watermark
    return catalog.catalog_sequence > sequence or (
        catalog.catalog_sequence == sequence
        and (digest is None or catalog.catalog_digest == digest)
    )


__all__ = [
    "BUILT_IN_CATALOG_ID",
    "BUILT_IN_PUBLISHER",
    "CATALOG_SCHEMA_VERSION",
    "CatalogActivation",
    "CatalogError",
    "CatalogParseError",
    "CatalogRollbackError",
    "CatalogStore",
    "CatalogStoreError",
    "CatalogTrustError",
    "CatalogValidationError",
    "CatalogVerifier",
    "TrustedCatalogKey",
    "VerifiedCatalog",
    "artifact_manifest_digest",
    "built_in_empty_catalog",
    "canonical_json",
    "catalog_digest",
    "parse_catalog_json",
    "signing_message",
]

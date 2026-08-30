"""Pure, local-only ownership evidence for native model installations.

Operational install state is not deletion authority.  This module deliberately
does not accept aliases, download status alone, destinations alone, or the
legacy ``files_json`` selection as cleanup proof.  Only one exact installation
ID plus a complete immutable evidence record can be eligible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import posixpath
import re
from typing import Any, Mapping, Sequence
from uuid import UUID


# Revision 1 is the original signed-catalog-only proof.  Revision 2 adds an
# explicit artifact authority plus the identity of the directory created by
# the install transaction.  Cleanup keeps accepting already-recorded revision
# 1 proofs; new production capture must use revision 2.
LEGACY_PROVENANCE_REVISION = 1
PROVENANCE_REVISION = 2
MAX_OWNED_FILES = 4096
MAX_EVIDENCE_ENTRIES = 4096
MAX_PROVENANCE_JSON_BYTES = 1024 * 1024
MANAGED_CREATION_MARKER_PATH = (
    ".cache/huggingface/.mnemosyne-managed-creation-v1.json"
)
HF_LOCAL_METADATA_ROOT = ".cache/huggingface"
_MAX_IDENTIFIER_BYTES = 128
_MAX_PATH_BYTES = 4096
_MAX_RELATIVE_PATH_BYTES = 512
_MAX_BINDING_GENERATION = 2_147_483_647
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SCOPE_ID_RE = re.compile(r"[0-9a-f]{64}")


class SourceKind(StrEnum):
    MANAGED_DOWNLOAD = "managed_download"
    LOCAL_IMPORT = "local_import"
    LEGACY_MIGRATION = "legacy_migration"
    EXTERNAL_REFERENCE = "external_reference"


class OwnershipClass(StrEnum):
    EXCLUSIVE_MANAGED = "exclusive_managed"
    USER_OWNED = "user_owned"
    EXTERNAL_OWNED = "external_owned"
    SHARED = "shared"
    UNKNOWN = "unknown"


class ArtifactAuthority(StrEnum):
    SIGNED_CATALOG = "signed_catalog"
    LOCAL_PINNED_DISCOVERY = "local_pinned_discovery"


class DestinationStateBefore(StrEnum):
    ABSENT = "absent"
    EMPTY = "empty"
    NONEMPTY = "nonempty"
    UNKNOWN = "unknown"


class CleanupReason(StrEnum):
    ELIGIBLE = "eligible"
    INSTALLATION_MISSING = "installation_missing"
    INSTALLATION_ID_MISMATCH = "installation_id_mismatch"
    PROVENANCE_MISSING = "provenance_missing"
    PROVENANCE_MALFORMED = "provenance_malformed"
    SOURCE_NOT_MANAGED_DOWNLOAD = "source_not_managed_download"
    OWNERSHIP_NOT_EXCLUSIVE_MANAGED = "ownership_not_exclusive_managed"
    PROVENANCE_REVISION_UNSUPPORTED = "provenance_revision_unsupported"
    INSTALL_NOT_COMPLETE = "install_not_complete"
    STORAGE_BINDING_MISSING = "storage_binding_missing"
    CATALOG_IDENTITY_MISSING = "catalog_identity_missing"
    ARTIFACT_AUTHORITY_MISSING = "artifact_authority_missing"
    ARTIFACT_AUTHORITY_INVALID = "artifact_authority_invalid"
    DIGEST_EVIDENCE_MISSING = "digest_evidence_missing"
    DESTINATION_BINDING_MISSING = "destination_binding_missing"
    DESTINATION_BINDING_MISMATCH = "destination_binding_mismatch"
    DESTINATION_BINDING_DIGEST_MISMATCH = (
        "destination_binding_digest_mismatch"
    )
    REVISION_MISMATCH = "revision_mismatch"
    OWNED_MANIFEST_MISSING = "owned_manifest_missing"
    OWNED_MANIFEST_INVALID = "owned_manifest_invalid"
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    DESTINATION_STATE_UNKNOWN = "destination_state_unknown"
    DESTINATION_PREEXISTED = "destination_preexisted"
    DESTINATION_CREATION_UNPROVEN = "destination_creation_unproven"
    PATH_EVIDENCE_MISSING = "path_evidence_missing"
    EVIDENCE_OVERLAP = "evidence_overlap"
    PREEXISTING_ENTRIES_PRESENT = "preexisting_entries_present"
    EXTRA_ENTRIES_PRESENT = "extra_entries_present"
    CREATION_TRANSACTION_MISSING = "creation_transaction_missing"
    DIRECTORY_IDENTITY_MISSING = "directory_identity_missing"


class ProvenanceError(RuntimeError):
    """Fixed-code provenance failure without caller-controlled diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProvenanceDataError(ProvenanceError):
    pass


class ProvenanceConflictError(ProvenanceError):
    pass


class ProvenanceProofRejected(ProvenanceError):
    def __init__(self, reason: CleanupReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class OwnedFile:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CleanupInstallation:
    """The only operational fields relevant to pure cleanup eligibility."""

    installation_id: str
    status: str
    destination: str
    resolved_revision: str | None
    total_bytes: int | None


@dataclass(frozen=True, slots=True)
class InstallationProvenance:
    installation_id: str
    source_kind: SourceKind = SourceKind.MANAGED_DOWNLOAD
    ownership_class: OwnershipClass = OwnershipClass.UNKNOWN
    storage_location_id: str | None = None
    storage_binding_generation: int | None = None
    storage_lexical_root: str | None = None
    storage_volume_uuid: str | None = None
    storage_scope_id: str | None = None
    lexical_destination: str | None = None
    destination_binding_digest: str | None = None
    catalog_id: str | None = None
    logical_model_id: str | None = None
    artifact_id: str | None = None
    recipe_id: str | None = None
    resolved_revision: str | None = None
    catalog_digest: str | None = None
    artifact_authority: ArtifactAuthority | None = None
    source_identity_digest: str | None = None
    manifest_digest: str | None = None
    owned_files: tuple[OwnedFile, ...] | None = None
    destination_state_before: DestinationStateBefore = (
        DestinationStateBefore.UNKNOWN
    )
    destination_created_by_transaction: bool | None = None
    preexisting_entries: tuple[str, ...] | None = None
    extra_entries: tuple[str, ...] | None = None
    creation_transaction_id: str | None = None
    directory_device: int | None = None
    directory_inode: int | None = None
    provenance_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_kind"] = self.source_kind.value
        payload["ownership_class"] = self.ownership_class.value
        payload["destination_state_before"] = self.destination_state_before.value
        payload["artifact_authority"] = (
            self.artifact_authority.value
            if self.artifact_authority is not None
            else None
        )
        return payload


@dataclass(frozen=True, slots=True)
class ExclusiveManagedProof:
    installation_id: str
    storage_location_id: str
    storage_binding_generation: int
    storage_lexical_root: str
    lexical_destination: str
    destination_binding_digest: str
    catalog_id: str
    logical_model_id: str
    artifact_id: str
    recipe_id: str
    resolved_revision: str
    catalog_digest: str
    manifest_digest: str
    owned_files: tuple[OwnedFile, ...]
    destination_state_before: DestinationStateBefore
    destination_created_by_transaction: bool
    preexisting_entries: tuple[str, ...]
    extra_entries: tuple[str, ...]
    creation_transaction_id: str
    storage_volume_uuid: str | None = None
    storage_scope_id: str | None = None
    # Defaults retain compatibility with complete proofs written before
    # production capture existed. New capture supplies every field below and
    # explicitly records ``PROVENANCE_REVISION``.
    artifact_authority: ArtifactAuthority | None = None
    source_identity_digest: str | None = None
    directory_device: int | None = None
    directory_inode: int | None = None
    provenance_revision: int = LEGACY_PROVENANCE_REVISION


@dataclass(frozen=True, slots=True)
class ManagedCreationClaim:
    """Immutable evidence captured when one managed destination is created."""

    installation_id: str
    storage_location_id: str
    storage_binding_generation: int
    storage_lexical_root: str
    lexical_destination: str
    destination_binding_digest: str
    resolved_revision: str
    artifact_authority: ArtifactAuthority
    source_identity_digest: str
    creation_transaction_id: str
    directory_device: int
    directory_inode: int
    storage_volume_uuid: str | None = None
    storage_scope_id: str | None = None
    catalog_id: str | None = None
    logical_model_id: str | None = None
    artifact_id: str | None = None
    recipe_id: str | None = None
    catalog_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedCreationIntent:
    """Durable authority written before the first destination filesystem effect."""

    installation_id: str
    storage_location_id: str
    storage_binding_generation: int
    storage_lexical_root: str
    lexical_destination: str
    destination_binding_digest: str
    resolved_revision: str
    artifact_authority: ArtifactAuthority
    source_identity_digest: str
    creation_transaction_id: str
    require_exclusive_proof: bool
    storage_volume_uuid: str | None = None
    storage_scope_id: str | None = None
    catalog_id: str | None = None
    logical_model_id: str | None = None
    artifact_id: str | None = None
    recipe_id: str | None = None
    catalog_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupEligibility:
    eligible: bool
    reason: CleanupReason


def _deny(reason: CleanupReason) -> CleanupEligibility:
    return CleanupEligibility(eligible=False, reason=reason)


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProvenanceDataError("provenance_malformed") from exc
    if len(encoded.encode("utf-8")) > MAX_PROVENANCE_JSON_BYTES:
        raise ProvenanceDataError("provenance_too_large")
    return encoded


def _safe_identifier(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES
    ):
        return False
    return value[0].isalnum() and all(
        character.isascii() and (character.isalnum() or character in "._:+-")
        for character in value
    )


def _safe_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _safe_source_label(value: object, *, max_bytes: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= max_bytes
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _safe_optional_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _safe_absolute_path(value: object) -> bool:
    if not (
        isinstance(value, str)
        and value.startswith("/")
        and value != "/"
        and 1 <= len(value.encode("utf-8")) <= _MAX_PATH_BYTES
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    ):
        return False
    return posixpath.normpath(value) == value


def _safe_relative_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= _MAX_RELATIVE_PATH_BYTES
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        return False
    return all(segment not in {"", ".", ".."} for segment in value.split("/"))


def _canonical_owned_files(files: Sequence[OwnedFile]) -> tuple[OwnedFile, ...]:
    if not 1 <= len(files) <= MAX_OWNED_FILES:
        raise ProvenanceDataError("owned_manifest_invalid")
    normalized: list[OwnedFile] = []
    for item in files:
        if (
            not isinstance(item, OwnedFile)
            or not _safe_relative_path(item.path)
            or isinstance(item.size_bytes, bool)
            or not isinstance(item.size_bytes, int)
            or item.size_bytes < 0
            or not _safe_digest(item.sha256)
        ):
            raise ProvenanceDataError("owned_manifest_invalid")
        normalized.append(item)
    normalized.sort(key=lambda item: item.path)
    paths = [item.path for item in normalized]
    if len(paths) != len(set(paths)) or any(
        _paths_overlap(left, right)
        for index, left in enumerate(paths)
        for right in paths[index + 1 :]
    ):
        raise ProvenanceDataError("owned_manifest_invalid")
    return tuple(normalized)


def canonical_owned_files_json(files: Sequence[OwnedFile]) -> str:
    normalized = _canonical_owned_files(files)
    return _canonical_json(
        [
            {"path": item.path, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in normalized
        ]
    )


def canonical_owned_files(files: Sequence[OwnedFile]) -> tuple[OwnedFile, ...]:
    """Return the exact canonical immutable file tuple or reject it."""

    return _canonical_owned_files(files)


def owned_manifest_digest(files: Sequence[OwnedFile]) -> str:
    encoded = canonical_owned_files_json(files).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def is_hf_local_metadata_path(path: str) -> bool:
    """Identify the downloader-owned Hugging Face ``local_dir`` namespace."""

    return path == HF_LOCAL_METADATA_ROOT or path.startswith(
        HF_LOCAL_METADATA_ROOT + "/"
    )


def allowed_hf_local_metadata_paths(
    payload_paths: Sequence[str],
) -> frozenset[str]:
    """Return the closed successful-download metadata set for HF ``local_dir``.

    Incomplete blobs are intentionally absent: ownership proof is captured only
    after the worker reports success. Every per-file metadata path is derived
    from an exact payload path, so the private cache namespace cannot hide an
    arbitrary model or user file.
    """

    normalized = tuple(payload_paths)
    if (
        not normalized
        or any(
            not _safe_relative_path(path)
            or is_hf_local_metadata_path(path)
            for path in normalized
        )
        or normalized != tuple(sorted(set(normalized)))
    ):
        raise ProvenanceDataError("managed_payload_paths_invalid")
    allowed = {
        f"{HF_LOCAL_METADATA_ROOT}/.gitignore",
        f"{HF_LOCAL_METADATA_ROOT}/.gitignore.lock",
        f"{HF_LOCAL_METADATA_ROOT}/CACHEDIR.TAG",
        MANAGED_CREATION_MARKER_PATH,
    }
    for payload_path in normalized:
        local_metadata = f"{HF_LOCAL_METADATA_ROOT}/download/{payload_path}"
        allowed.add(f"{local_metadata}.metadata")
        allowed.add(f"{local_metadata}.lock")
    return frozenset(allowed)


def _canonical_entries(
    entries: Sequence[str],
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if (
        len(entries) > MAX_EVIDENCE_ENTRIES
        or (not allow_empty and not entries)
        or any(not isinstance(item, str) for item in entries)
    ):
        raise ProvenanceDataError("path_evidence_invalid")
    normalized = sorted(entries)
    if (
        any(not _safe_relative_path(item) for item in normalized)
        or len(normalized) != len(set(normalized))
    ):
        raise ProvenanceDataError("path_evidence_invalid")
    return tuple(normalized)


def canonical_entries_json(entries: Sequence[str]) -> str:
    return _canonical_json(list(_canonical_entries(entries, allow_empty=True)))


def destination_binding_digest(
    *,
    storage_location_id: str,
    storage_binding_generation: int,
    storage_lexical_root: str,
    lexical_destination: str,
    storage_volume_uuid: str | None = None,
    storage_scope_id: str | None = None,
) -> str:
    if not _valid_binding_fields(
        storage_location_id=storage_location_id,
        storage_binding_generation=storage_binding_generation,
        storage_lexical_root=storage_lexical_root,
        lexical_destination=lexical_destination,
        storage_volume_uuid=storage_volume_uuid,
        storage_scope_id=storage_scope_id,
    ):
        raise ProvenanceDataError("destination_binding_invalid")
    encoded = _canonical_json(
        {
            "lexical_destination": lexical_destination,
            "storage_binding_generation": storage_binding_generation,
            "storage_lexical_root": storage_lexical_root,
            "storage_location_id": storage_location_id,
            "storage_scope_id": storage_scope_id,
            "storage_volume_uuid": storage_volume_uuid,
        }
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_binding_fields(
    *,
    storage_location_id: object,
    storage_binding_generation: object,
    storage_lexical_root: object,
    lexical_destination: object,
    storage_volume_uuid: object,
    storage_scope_id: object,
) -> bool:
    if (
        not _safe_identifier(storage_location_id)
        or isinstance(storage_binding_generation, bool)
        or not isinstance(storage_binding_generation, int)
        or not 1 <= storage_binding_generation <= _MAX_BINDING_GENERATION
        or not _safe_absolute_path(storage_lexical_root)
        or not _safe_absolute_path(lexical_destination)
        or (
            storage_volume_uuid is not None
            and not _safe_optional_label(storage_volume_uuid)
        )
        or (
            storage_scope_id is not None
            and (
                not isinstance(storage_scope_id, str)
                or _SCOPE_ID_RE.fullmatch(storage_scope_id) is None
            )
        )
    ):
        return False
    try:
        relative = posixpath.relpath(lexical_destination, storage_lexical_root)
    except (TypeError, ValueError):
        return False
    return (
        relative not in {"", "."}
        and relative != ".."
        and not relative.startswith("../")
    )


def _valid_transaction_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _valid_directory_identity(device: object, inode: object) -> bool:
    return all(
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= (1 << 63) - 1
        for value in (device, inode)
    )


def local_pinned_source_digest(
    *,
    repo_id: str,
    engine: str,
    resolved_revision: str,
    download_files: Sequence[str],
) -> str:
    """Bind an ordinary local install to its exact immutable Hub selection.

    This is deliberately labelled local pinned discovery. It is not a signed
    compatibility-catalog assertion and cannot be upgraded into one by naming
    conventions or a successful download.
    """

    if (
        not _safe_source_label(repo_id, max_bytes=300)
        or not _safe_identifier(engine)
        or not _safe_identifier(resolved_revision)
    ):
        raise ProvenanceDataError("source_identity_invalid")
    files = _canonical_entries(download_files, allow_empty=True)
    encoded = _canonical_json(
        {
            "authority": ArtifactAuthority.LOCAL_PINNED_DISCOVERY.value,
            "download_files": list(files),
            "engine": engine,
            "repo_id": repo_id,
            "resolved_revision": resolved_revision,
            "selection": "files" if files else "snapshot",
        }
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_managed_creation_claim(
    claim: ManagedCreationClaim,
) -> ManagedCreationClaim:
    if not isinstance(claim, ManagedCreationClaim):
        raise ProvenanceDataError("creation_claim_invalid")
    if (
        not _valid_transaction_id(claim.installation_id)
        or not _valid_binding_fields(
            storage_location_id=claim.storage_location_id,
            storage_binding_generation=claim.storage_binding_generation,
            storage_lexical_root=claim.storage_lexical_root,
            lexical_destination=claim.lexical_destination,
            storage_volume_uuid=claim.storage_volume_uuid,
            storage_scope_id=claim.storage_scope_id,
        )
        or not _safe_digest(claim.destination_binding_digest)
        or claim.destination_binding_digest
        != destination_binding_digest(
            storage_location_id=claim.storage_location_id,
            storage_binding_generation=claim.storage_binding_generation,
            storage_lexical_root=claim.storage_lexical_root,
            lexical_destination=claim.lexical_destination,
            storage_volume_uuid=claim.storage_volume_uuid,
            storage_scope_id=claim.storage_scope_id,
        )
        or not _safe_identifier(claim.resolved_revision)
        or not _safe_digest(claim.source_identity_digest)
        or not _valid_transaction_id(claim.creation_transaction_id)
        or not _valid_directory_identity(
            claim.directory_device,
            claim.directory_inode,
        )
    ):
        raise ProvenanceDataError("creation_claim_invalid")
    catalog_values = (
        claim.catalog_id,
        claim.logical_model_id,
        claim.artifact_id,
        claim.recipe_id,
    )
    if claim.artifact_authority is ArtifactAuthority.SIGNED_CATALOG:
        if not all(_safe_identifier(value) for value in catalog_values) or not (
            _safe_digest(claim.catalog_digest)
        ):
            raise ProvenanceDataError("creation_claim_invalid")
    elif claim.artifact_authority is ArtifactAuthority.LOCAL_PINNED_DISCOVERY:
        if any(value is not None for value in catalog_values) or (
            claim.catalog_digest is not None
        ):
            raise ProvenanceDataError("creation_claim_invalid")
    else:
        raise ProvenanceDataError("creation_claim_invalid")
    return claim


def validate_managed_creation_intent(
    intent: ManagedCreationIntent,
) -> ManagedCreationIntent:
    if not isinstance(intent, ManagedCreationIntent) or not isinstance(
        intent.require_exclusive_proof, bool
    ):
        raise ProvenanceDataError("creation_intent_invalid")
    # Reuse the claim's complete binding/catalog validation with a harmless
    # positive placeholder identity. Directory identity does not exist yet by
    # definition; it is supplied only after the isolated helper proves the
    # marker-bound filesystem effect.
    try:
        validate_managed_creation_claim(
            ManagedCreationClaim(
                installation_id=intent.installation_id,
                storage_location_id=intent.storage_location_id,
                storage_binding_generation=intent.storage_binding_generation,
                storage_lexical_root=intent.storage_lexical_root,
                storage_volume_uuid=intent.storage_volume_uuid,
                storage_scope_id=intent.storage_scope_id,
                lexical_destination=intent.lexical_destination,
                destination_binding_digest=intent.destination_binding_digest,
                resolved_revision=intent.resolved_revision,
                artifact_authority=intent.artifact_authority,
                source_identity_digest=intent.source_identity_digest,
                catalog_id=intent.catalog_id,
                logical_model_id=intent.logical_model_id,
                artifact_id=intent.artifact_id,
                recipe_id=intent.recipe_id,
                catalog_digest=intent.catalog_digest,
                creation_transaction_id=intent.creation_transaction_id,
                directory_device=1,
                directory_inode=1,
            )
        )
    except ProvenanceDataError as exc:
        raise ProvenanceDataError("creation_intent_invalid") from exc
    return intent


def validate_claim_for_intent(
    claim: ManagedCreationClaim,
    intent: ManagedCreationIntent,
) -> ManagedCreationClaim:
    claim = validate_managed_creation_claim(claim)
    intent = validate_managed_creation_intent(intent)
    comparable_claim = (
        claim.installation_id,
        claim.storage_location_id,
        claim.storage_binding_generation,
        claim.storage_lexical_root,
        claim.storage_volume_uuid,
        claim.storage_scope_id,
        claim.lexical_destination,
        claim.destination_binding_digest,
        claim.resolved_revision,
        claim.artifact_authority,
        claim.source_identity_digest,
        claim.catalog_id,
        claim.logical_model_id,
        claim.artifact_id,
        claim.recipe_id,
        claim.catalog_digest,
        claim.creation_transaction_id,
    )
    comparable_intent = (
        intent.installation_id,
        intent.storage_location_id,
        intent.storage_binding_generation,
        intent.storage_lexical_root,
        intent.storage_volume_uuid,
        intent.storage_scope_id,
        intent.lexical_destination,
        intent.destination_binding_digest,
        intent.resolved_revision,
        intent.artifact_authority,
        intent.source_identity_digest,
        intent.catalog_id,
        intent.logical_model_id,
        intent.artifact_id,
        intent.recipe_id,
        intent.catalog_digest,
        intent.creation_transaction_id,
    )
    if comparable_claim != comparable_intent:
        raise ProvenanceDataError("creation_claim_intent_mismatch")
    return claim


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = left.split("/")
    right_parts = right.split("/")
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def provenance_from_exclusive_proof(
    proof: ExclusiveManagedProof,
) -> InstallationProvenance:
    owned_files = _canonical_owned_files(proof.owned_files)
    preexisting = _canonical_entries(proof.preexisting_entries, allow_empty=True)
    extra = _canonical_entries(proof.extra_entries, allow_empty=True)
    return InstallationProvenance(
        installation_id=proof.installation_id,
        source_kind=SourceKind.MANAGED_DOWNLOAD,
        ownership_class=OwnershipClass.EXCLUSIVE_MANAGED,
        storage_location_id=proof.storage_location_id,
        storage_binding_generation=proof.storage_binding_generation,
        storage_lexical_root=proof.storage_lexical_root,
        storage_volume_uuid=proof.storage_volume_uuid,
        storage_scope_id=proof.storage_scope_id,
        lexical_destination=proof.lexical_destination,
        destination_binding_digest=proof.destination_binding_digest,
        catalog_id=proof.catalog_id,
        logical_model_id=proof.logical_model_id,
        artifact_id=proof.artifact_id,
        recipe_id=proof.recipe_id,
        resolved_revision=proof.resolved_revision,
        catalog_digest=proof.catalog_digest,
        artifact_authority=proof.artifact_authority,
        source_identity_digest=proof.source_identity_digest,
        manifest_digest=proof.manifest_digest,
        owned_files=owned_files,
        destination_state_before=proof.destination_state_before,
        destination_created_by_transaction=proof.destination_created_by_transaction,
        preexisting_entries=preexisting,
        extra_entries=extra,
        creation_transaction_id=proof.creation_transaction_id,
        directory_device=proof.directory_device,
        directory_inode=proof.directory_inode,
        provenance_revision=proof.provenance_revision,
    )


def exclusive_proof_from_claim(
    claim: ManagedCreationClaim,
    owned_files: Sequence[OwnedFile],
) -> ExclusiveManagedProof:
    """Complete an immutable creation claim with a freshly captured tree."""

    claim = validate_managed_creation_claim(claim)
    canonical_files = _canonical_owned_files(owned_files)
    return ExclusiveManagedProof(
        installation_id=claim.installation_id,
        storage_location_id=claim.storage_location_id,
        storage_binding_generation=claim.storage_binding_generation,
        storage_lexical_root=claim.storage_lexical_root,
        storage_volume_uuid=claim.storage_volume_uuid,
        storage_scope_id=claim.storage_scope_id,
        lexical_destination=claim.lexical_destination,
        destination_binding_digest=claim.destination_binding_digest,
        catalog_id=claim.catalog_id,
        logical_model_id=claim.logical_model_id,
        artifact_id=claim.artifact_id,
        recipe_id=claim.recipe_id,
        resolved_revision=claim.resolved_revision,
        catalog_digest=claim.catalog_digest,
        artifact_authority=claim.artifact_authority,
        source_identity_digest=claim.source_identity_digest,
        manifest_digest=owned_manifest_digest(canonical_files),
        owned_files=canonical_files,
        destination_state_before=DestinationStateBefore.ABSENT,
        destination_created_by_transaction=True,
        preexisting_entries=(),
        extra_entries=(),
        creation_transaction_id=claim.creation_transaction_id,
        directory_device=claim.directory_device,
        directory_inode=claim.directory_inode,
        provenance_revision=PROVENANCE_REVISION,
    )


def decide_cleanup_eligibility(
    installation_id: str,
    installation: CleanupInstallation | None,
    provenance: InstallationProvenance | None,
) -> CleanupEligibility:
    """Return a fixed, fail-closed decision for one exact installation ID."""

    if installation is None:
        return _deny(CleanupReason.INSTALLATION_MISSING)
    if installation.installation_id != installation_id:
        return _deny(CleanupReason.INSTALLATION_ID_MISMATCH)
    if provenance is None:
        return _deny(CleanupReason.PROVENANCE_MISSING)
    if provenance.installation_id != installation_id:
        return _deny(CleanupReason.INSTALLATION_ID_MISMATCH)
    if provenance.source_kind is not SourceKind.MANAGED_DOWNLOAD:
        return _deny(CleanupReason.SOURCE_NOT_MANAGED_DOWNLOAD)
    if provenance.ownership_class is not OwnershipClass.EXCLUSIVE_MANAGED:
        return _deny(CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED)
    if provenance.provenance_revision not in {
        LEGACY_PROVENANCE_REVISION,
        PROVENANCE_REVISION,
    }:
        return _deny(CleanupReason.PROVENANCE_REVISION_UNSUPPORTED)
    if installation.status != "installed":
        return _deny(CleanupReason.INSTALL_NOT_COMPLETE)

    if not _valid_binding_fields(
        storage_location_id=provenance.storage_location_id,
        storage_binding_generation=provenance.storage_binding_generation,
        storage_lexical_root=provenance.storage_lexical_root,
        lexical_destination=provenance.lexical_destination,
        storage_volume_uuid=provenance.storage_volume_uuid,
        storage_scope_id=provenance.storage_scope_id,
    ):
        return _deny(CleanupReason.STORAGE_BINDING_MISSING)
    catalog_values = (
        provenance.catalog_id,
        provenance.logical_model_id,
        provenance.artifact_id,
        provenance.recipe_id,
    )
    if provenance.provenance_revision == LEGACY_PROVENANCE_REVISION:
        if provenance.artifact_authority is not None or (
            provenance.source_identity_digest is not None
        ):
            return _deny(CleanupReason.ARTIFACT_AUTHORITY_INVALID)
        if not all(_safe_identifier(value) for value in catalog_values):
            return _deny(CleanupReason.CATALOG_IDENTITY_MISSING)
        if not _safe_digest(provenance.catalog_digest):
            return _deny(CleanupReason.DIGEST_EVIDENCE_MISSING)
    else:
        if provenance.artifact_authority is None or not _safe_digest(
            provenance.source_identity_digest
        ):
            return _deny(CleanupReason.ARTIFACT_AUTHORITY_MISSING)
        if provenance.artifact_authority is ArtifactAuthority.SIGNED_CATALOG:
            if not all(_safe_identifier(value) for value in catalog_values):
                return _deny(CleanupReason.CATALOG_IDENTITY_MISSING)
            if not _safe_digest(provenance.catalog_digest):
                return _deny(CleanupReason.DIGEST_EVIDENCE_MISSING)
        elif (
            provenance.artifact_authority
            is ArtifactAuthority.LOCAL_PINNED_DISCOVERY
        ):
            if any(value is not None for value in catalog_values) or (
                provenance.catalog_digest is not None
            ):
                return _deny(CleanupReason.ARTIFACT_AUTHORITY_INVALID)
        else:
            return _deny(CleanupReason.ARTIFACT_AUTHORITY_INVALID)
        if not _valid_directory_identity(
            provenance.directory_device,
            provenance.directory_inode,
        ):
            return _deny(CleanupReason.DIRECTORY_IDENTITY_MISSING)
    if not _safe_digest(provenance.manifest_digest):
        return _deny(CleanupReason.DIGEST_EVIDENCE_MISSING)
    if not _safe_digest(provenance.destination_binding_digest):
        return _deny(CleanupReason.DESTINATION_BINDING_MISSING)
    if provenance.lexical_destination != installation.destination:
        return _deny(CleanupReason.DESTINATION_BINDING_MISMATCH)
    try:
        expected_binding_digest = destination_binding_digest(
            storage_location_id=provenance.storage_location_id,
            storage_binding_generation=provenance.storage_binding_generation,
            storage_lexical_root=provenance.storage_lexical_root,
            lexical_destination=provenance.lexical_destination,
            storage_volume_uuid=provenance.storage_volume_uuid,
            storage_scope_id=provenance.storage_scope_id,
        )
    except ProvenanceDataError:
        return _deny(CleanupReason.DESTINATION_BINDING_MISSING)
    if provenance.destination_binding_digest != expected_binding_digest:
        return _deny(CleanupReason.DESTINATION_BINDING_DIGEST_MISMATCH)
    if (
        not _safe_identifier(provenance.resolved_revision)
        or provenance.resolved_revision != installation.resolved_revision
    ):
        return _deny(CleanupReason.REVISION_MISMATCH)

    if provenance.owned_files is None:
        return _deny(CleanupReason.OWNED_MANIFEST_MISSING)
    try:
        owned_files = _canonical_owned_files(provenance.owned_files)
    except ProvenanceDataError:
        return _deny(CleanupReason.OWNED_MANIFEST_INVALID)
    if owned_files != provenance.owned_files:
        return _deny(CleanupReason.OWNED_MANIFEST_INVALID)
    if provenance.manifest_digest != owned_manifest_digest(owned_files):
        return _deny(CleanupReason.MANIFEST_DIGEST_MISMATCH)
    owned_total = sum(item.size_bytes for item in owned_files)
    if owned_total <= 0:
        return _deny(CleanupReason.SIZE_MISMATCH)
    if provenance.provenance_revision == LEGACY_PROVENANCE_REVISION and (
        installation.total_bytes is None
        or isinstance(installation.total_bytes, bool)
        or installation.total_bytes <= 0
        or owned_total != installation.total_bytes
    ):
        return _deny(CleanupReason.SIZE_MISMATCH)

    if provenance.destination_state_before is DestinationStateBefore.UNKNOWN:
        return _deny(CleanupReason.DESTINATION_STATE_UNKNOWN)
    if provenance.destination_state_before is not DestinationStateBefore.ABSENT:
        return _deny(CleanupReason.DESTINATION_PREEXISTED)
    if provenance.destination_created_by_transaction is not True:
        return _deny(CleanupReason.DESTINATION_CREATION_UNPROVEN)
    if provenance.preexisting_entries is None or provenance.extra_entries is None:
        return _deny(CleanupReason.PATH_EVIDENCE_MISSING)
    try:
        preexisting = _canonical_entries(
            provenance.preexisting_entries,
            allow_empty=True,
        )
        extra = _canonical_entries(provenance.extra_entries, allow_empty=True)
    except ProvenanceDataError:
        return _deny(CleanupReason.PROVENANCE_MALFORMED)
    owned_paths = tuple(item.path for item in owned_files)
    if any(
        _paths_overlap(path, owned)
        for path in (*preexisting, *extra)
        for owned in owned_paths
    ):
        return _deny(CleanupReason.EVIDENCE_OVERLAP)
    if preexisting:
        return _deny(CleanupReason.PREEXISTING_ENTRIES_PRESENT)
    if extra:
        return _deny(CleanupReason.EXTRA_ENTRIES_PRESENT)
    if not _valid_transaction_id(provenance.creation_transaction_id):
        return _deny(CleanupReason.CREATION_TRANSACTION_MISSING)
    return CleanupEligibility(eligible=True, reason=CleanupReason.ELIGIBLE)


def default_provenance(installation_id: str) -> InstallationProvenance:
    return InstallationProvenance(installation_id=installation_id)


def is_default_unknown(provenance: InstallationProvenance) -> bool:
    return provenance == default_provenance(provenance.installation_id)


def provenance_database_fields(
    provenance: InstallationProvenance,
) -> dict[str, Any]:
    owned_files_json = (
        canonical_owned_files_json(provenance.owned_files)
        if provenance.owned_files is not None
        else None
    )
    preexisting_json = (
        canonical_entries_json(provenance.preexisting_entries)
        if provenance.preexisting_entries is not None
        else None
    )
    extra_json = (
        canonical_entries_json(provenance.extra_entries)
        if provenance.extra_entries is not None
        else None
    )
    return {
        "installation_id": provenance.installation_id,
        "source_kind": provenance.source_kind.value,
        "ownership_class": provenance.ownership_class.value,
        "storage_location_id": provenance.storage_location_id,
        "storage_binding_generation": provenance.storage_binding_generation,
        "storage_lexical_root": provenance.storage_lexical_root,
        "storage_volume_uuid": provenance.storage_volume_uuid,
        "storage_scope_id": provenance.storage_scope_id,
        "lexical_destination": provenance.lexical_destination,
        "destination_binding_digest": provenance.destination_binding_digest,
        "catalog_id": provenance.catalog_id,
        "logical_model_id": provenance.logical_model_id,
        "artifact_id": provenance.artifact_id,
        "recipe_id": provenance.recipe_id,
        "resolved_revision": provenance.resolved_revision,
        "catalog_digest": provenance.catalog_digest,
        "artifact_authority": (
            provenance.artifact_authority.value
            if provenance.artifact_authority is not None
            else None
        ),
        "source_identity_digest": provenance.source_identity_digest,
        "manifest_digest": provenance.manifest_digest,
        "owned_files_json": owned_files_json,
        "destination_state_before": provenance.destination_state_before.value,
        "destination_created_by_transaction": (
            None
            if provenance.destination_created_by_transaction is None
            else int(provenance.destination_created_by_transaction)
        ),
        "preexisting_entries_json": preexisting_json,
        "extra_entries_json": extra_json,
        "creation_transaction_id": provenance.creation_transaction_id,
        "directory_device": provenance.directory_device,
        "directory_inode": provenance.directory_inode,
        "provenance_revision": provenance.provenance_revision,
    }


def _decode_canonical_json(value: object) -> Any:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > MAX_PROVENANCE_JSON_BYTES
    ):
        raise ProvenanceDataError("provenance_malformed")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProvenanceDataError("provenance_malformed") from exc
    if _canonical_json(decoded) != value:
        raise ProvenanceDataError("provenance_malformed")
    return decoded


def _decode_owned_files(value: object) -> tuple[OwnedFile, ...] | None:
    if value is None:
        return None
    decoded = _decode_canonical_json(value)
    if not isinstance(decoded, list):
        raise ProvenanceDataError("provenance_malformed")
    files: list[OwnedFile] = []
    for row in decoded:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}:
            raise ProvenanceDataError("provenance_malformed")
        files.append(
            OwnedFile(
                path=row["path"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
            )
        )
    normalized = _canonical_owned_files(files)
    if tuple(files) != normalized:
        raise ProvenanceDataError("provenance_malformed")
    return normalized


def owned_files_from_canonical_json(value: str) -> tuple[OwnedFile, ...]:
    """Decode one non-null canonical manifest persisted by the installer."""

    decoded = _decode_owned_files(value)
    if decoded is None:
        raise ProvenanceDataError("owned_manifest_invalid")
    return decoded


def _decode_entries(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    decoded = _decode_canonical_json(value)
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ProvenanceDataError("provenance_malformed")
    normalized = _canonical_entries(decoded, allow_empty=True)
    if tuple(decoded) != normalized:
        raise ProvenanceDataError("provenance_malformed")
    return normalized


def provenance_from_database_row(
    row: Mapping[str, Any],
) -> InstallationProvenance:
    try:
        installation_id = row["installation_id"]
        source_kind = SourceKind(row["source_kind"])
        ownership_class = OwnershipClass(row["ownership_class"])
        destination_state = DestinationStateBefore(row["destination_state_before"])
        artifact_authority = (
            ArtifactAuthority(row["artifact_authority"])
            if row["artifact_authority"] is not None
            else None
        )
        binding_generation = row["storage_binding_generation"]
        created = row["destination_created_by_transaction"]
        directory_device = row["directory_device"]
        directory_inode = row["directory_inode"]
        provenance_revision = row["provenance_revision"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceDataError("provenance_malformed") from exc
    if (
        not isinstance(installation_id, str)
        or not installation_id
        or len(installation_id.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
        or "\x00" in installation_id
        or (
            binding_generation is not None
            and (
                isinstance(binding_generation, bool)
                or not isinstance(binding_generation, int)
                or not 1 <= binding_generation <= _MAX_BINDING_GENERATION
            )
        )
        or created not in {None, 0, 1}
        or (
            (directory_device is None) != (directory_inode is None)
            or (
                directory_device is not None
                and not _valid_directory_identity(
                    directory_device,
                    directory_inode,
                )
            )
        )
        or isinstance(provenance_revision, bool)
        or not isinstance(provenance_revision, int)
        or not 0 <= provenance_revision <= _MAX_BINDING_GENERATION
    ):
        raise ProvenanceDataError("provenance_malformed")
    optional_identifiers = (
        row["storage_location_id"],
        row["catalog_id"],
        row["logical_model_id"],
        row["artifact_id"],
        row["recipe_id"],
        row["resolved_revision"],
    )
    if any(
        value is not None and not _safe_identifier(value)
        for value in optional_identifiers
    ):
        raise ProvenanceDataError("provenance_malformed")
    optional_digests = (
        row["destination_binding_digest"],
        row["catalog_digest"],
        row["source_identity_digest"],
        row["manifest_digest"],
    )
    if any(value is not None and not _safe_digest(value) for value in optional_digests):
        raise ProvenanceDataError("provenance_malformed")
    for value in (row["storage_lexical_root"], row["lexical_destination"]):
        if value is not None and not _safe_absolute_path(value):
            raise ProvenanceDataError("provenance_malformed")
    volume_uuid = row["storage_volume_uuid"]
    if volume_uuid is not None and not _safe_optional_label(volume_uuid):
        raise ProvenanceDataError("provenance_malformed")
    scope_id = row["storage_scope_id"]
    if scope_id is not None and (
        not isinstance(scope_id, str) or _SCOPE_ID_RE.fullmatch(scope_id) is None
    ):
        raise ProvenanceDataError("provenance_malformed")
    transaction_id = row["creation_transaction_id"]
    if transaction_id is not None and not _valid_transaction_id(transaction_id):
        raise ProvenanceDataError("provenance_malformed")
    return InstallationProvenance(
        installation_id=installation_id,
        source_kind=source_kind,
        ownership_class=ownership_class,
        storage_location_id=row["storage_location_id"],
        storage_binding_generation=binding_generation,
        storage_lexical_root=row["storage_lexical_root"],
        storage_volume_uuid=volume_uuid,
        storage_scope_id=scope_id,
        lexical_destination=row["lexical_destination"],
        destination_binding_digest=row["destination_binding_digest"],
        catalog_id=row["catalog_id"],
        logical_model_id=row["logical_model_id"],
        artifact_id=row["artifact_id"],
        recipe_id=row["recipe_id"],
        resolved_revision=row["resolved_revision"],
        catalog_digest=row["catalog_digest"],
        artifact_authority=artifact_authority,
        source_identity_digest=row["source_identity_digest"],
        manifest_digest=row["manifest_digest"],
        owned_files=_decode_owned_files(row["owned_files_json"]),
        destination_state_before=destination_state,
        destination_created_by_transaction=(None if created is None else bool(created)),
        preexisting_entries=_decode_entries(row["preexisting_entries_json"]),
        extra_entries=_decode_entries(row["extra_entries_json"]),
        creation_transaction_id=transaction_id,
        directory_device=directory_device,
        directory_inode=directory_inode,
        provenance_revision=provenance_revision,
    )


__all__ = [
    "ArtifactAuthority",
    "CleanupEligibility",
    "CleanupInstallation",
    "CleanupReason",
    "DestinationStateBefore",
    "ExclusiveManagedProof",
    "HF_LOCAL_METADATA_ROOT",
    "InstallationProvenance",
    "LEGACY_PROVENANCE_REVISION",
    "MAX_EVIDENCE_ENTRIES",
    "MAX_OWNED_FILES",
    "ManagedCreationClaim",
    "ManagedCreationIntent",
    "MANAGED_CREATION_MARKER_PATH",
    "OwnershipClass",
    "OwnedFile",
    "PROVENANCE_REVISION",
    "ProvenanceConflictError",
    "ProvenanceDataError",
    "ProvenanceProofRejected",
    "SourceKind",
    "allowed_hf_local_metadata_paths",
    "canonical_owned_files",
    "canonical_entries_json",
    "canonical_owned_files_json",
    "decide_cleanup_eligibility",
    "default_provenance",
    "destination_binding_digest",
    "exclusive_proof_from_claim",
    "is_default_unknown",
    "is_hf_local_metadata_path",
    "local_pinned_source_digest",
    "owned_manifest_digest",
    "owned_files_from_canonical_json",
    "provenance_database_fields",
    "provenance_from_database_row",
    "provenance_from_exclusive_proof",
    "validate_managed_creation_claim",
    "validate_managed_creation_intent",
    "validate_claim_for_intent",
]

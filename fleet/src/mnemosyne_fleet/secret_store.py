from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CredentialRole = Literal["snapshot", "dispatch", "management"]
PairingMaterialRole = Literal["invitation", "locator"]

_ROLES: Final[tuple[CredentialRole, ...]] = (
    "snapshot",
    "dispatch",
    "management",
)
_PAIRING_MATERIAL_ROLES: Final[tuple[PairingMaterialRole, ...]] = (
    "invitation",
    "locator",
)
_SCHEMA_VERSION: Final[int] = 1
_NONCE_BYTES: Final[int] = 12
_KEY_CHECK_PLAINTEXT: Final[bytes] = (
    b"mnemosyne-fleet-secret-store-key-check-v1"
)
_MAX_STORE_ID_BYTES: Final[int] = 128
_MAX_PAIRING_ID_BYTES: Final[int] = 256
_MAX_SECRET_REF_BYTES: Final[int] = 512
_MAX_SECRET_BYTES: Final[int] = 4096
_MAX_PRIVATE_VALUE_BYTES: Final[int] = 4096
_MAX_GENERATION: Final[int] = (1 << 63) - 1
_MASTER_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{43}=?\Z")

_T = TypeVar("_T")


class SecretStoreError(RuntimeError):
    """A fixed-code failure that never includes credential material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SecretStoreConfigurationError(SecretStoreError):
    pass


class SecretStorePathError(SecretStoreError):
    pass


class SecretStoreValidationError(SecretStoreError):
    pass


class SecretStoreConflictError(SecretStoreError):
    pass


class SecretStoreIntegrityError(SecretStoreError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialSecret:
    secret_ref: str
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialBundle:
    pairing_id: str
    generation: int
    snapshot: CredentialSecret
    dispatch: CredentialSecret
    management: CredentialSecret

    def credential(self, role: CredentialRole) -> CredentialSecret:
        if role == "snapshot":
            return self.snapshot
        if role == "dispatch":
            return self.dispatch
        if role == "management":
            return self.management
        raise SecretStoreValidationError("secret_store_invalid_bundle")


@dataclass(frozen=True, slots=True)
class PrivateValue:
    value_ref: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PairingMaterial:
    pairing_id: str
    invitation_id: str
    invitation: PrivateValue
    locator: PrivateValue

    def material(self, role: PairingMaterialRole) -> PrivateValue:
        if role == "invitation":
            return self.invitation
        if role == "locator":
            return self.locator
        raise SecretStoreValidationError("secret_store_invalid_pairing_material")


class SecretStore:
    """Encrypted pairing credentials in a dedicated private SQLite file.

    The caller owns key acquisition and configuration. This class never writes
    the master key or a plaintext credential to disk. All blocking filesystem,
    SQLite, and AEAD work runs in a worker thread, serialized so a cancelled
    caller observes only a fully committed or fully rolled-back transaction.
    """

    def __init__(
        self,
        path: Path,
        *,
        store_id: str,
        master_key: str,
    ) -> None:
        self._path = Path(path)
        self._store_id = _validate_text(
            store_id,
            maximum=_MAX_STORE_ID_BYTES,
            code="secret_store_invalid_store_id",
        )
        self._key = _decode_master_key(master_key)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        await self._run(self._initialize_sync)
        self._initialized = True

    async def create_bundle(self, bundle: CredentialBundle) -> CredentialBundle:
        """Atomically create all three credentials or replay the exact bundle.

        An existing pairing/generation is idempotent only when every role,
        opaque reference, and secret is identical. A differing replay fails
        without changing either the existing bundle or any individual role.
        """

        self._require_initialized()
        _validate_bundle(bundle)
        return await self._run(lambda: self._create_bundle_sync(bundle))

    async def load_bundle(
        self,
        pairing_id: str,
        generation: int,
    ) -> CredentialBundle | None:
        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        generation = _validate_generation(generation)
        return await self._run(
            lambda: self._load_bundle_sync(pairing_id, generation)
        )

    async def delete_bundle(self, pairing_id: str, generation: int) -> bool:
        """Atomically and securely delete one complete credential bundle."""

        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        generation = _validate_generation(generation)
        return await self._run(
            lambda: self._delete_bundle_sync(pairing_id, generation)
        )

    async def create_pairing_material(
        self,
        material: PairingMaterial,
    ) -> PairingMaterial:
        """Atomically create the invitation verifier and encrypted locator."""

        self._require_initialized()
        _validate_pairing_material(material)
        return await self._run(
            lambda: self._create_pairing_material_sync(material)
        )

    async def load_pairing_material(
        self,
        pairing_id: str,
        invitation_id: str,
    ) -> PairingMaterial | None:
        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        invitation_id = _validate_pairing_id(invitation_id)
        return await self._run(
            lambda: self._load_pairing_material_sync(pairing_id, invitation_id)
        )

    async def verify_pairing_material(
        self,
        pairing_id: str,
        invitation_id: str,
        role: PairingMaterialRole,
        value_ref: str,
        candidate: str,
    ) -> bool:
        """Constant-time verification without returning stored private data."""

        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        invitation_id = _validate_pairing_id(invitation_id)
        if role not in _PAIRING_MATERIAL_ROLES:
            raise SecretStoreValidationError(
                "secret_store_invalid_pairing_material"
            )
        value_ref = _validate_secret_ref(value_ref)
        candidate = _validate_private_value(candidate)
        return await self._run(
            lambda: self._verify_pairing_material_sync(
                pairing_id,
                invitation_id,
                role,
                value_ref,
                candidate,
            )
        )

    async def load_locator(self, pairing_id: str, locator_ref: str) -> str:
        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        locator_ref = _validate_secret_ref(locator_ref)
        return await self._run(
            lambda: self._load_locator_sync(pairing_id, locator_ref)
        )

    async def delete_invitation_material(
        self,
        pairing_id: str,
        invitation_id: str,
    ) -> bool:
        """Delete only the one-time invitation value, retaining the locator."""

        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        invitation_id = _validate_pairing_id(invitation_id)
        return await self._run(
            lambda: self._delete_pairing_material_sync(
                pairing_id,
                invitation_id,
                invitation_only=True,
            )
        )

    async def delete_pairing_material(
        self,
        pairing_id: str,
        invitation_id: str,
    ) -> bool:
        self._require_initialized()
        pairing_id = _validate_pairing_id(pairing_id)
        invitation_id = _validate_pairing_id(invitation_id)
        return await self._run(
            lambda: self._delete_pairing_material_sync(
                pairing_id,
                invitation_id,
                invitation_only=False,
            )
        )

    async def _run(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            worker = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # asyncio.to_thread cannot stop a transaction already running.
                # Wait for its atomic outcome before exposing cancellation.
                try:
                    await worker
                except BaseException:
                    pass
                raise

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise SecretStoreConfigurationError("secret_store_not_initialized")

    def _initialize_sync(self) -> None:
        self._prepare_private_path()
        with self._connect() as conn:
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS secret_store_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        store_id TEXT NOT NULL,
                        key_check_nonce BLOB NOT NULL CHECK (
                            length(key_check_nonce) = 12
                        ),
                        key_check_ciphertext BLOB NOT NULL CHECK (
                            length(key_check_ciphertext) >= 16
                        )
                    );
                    CREATE TABLE IF NOT EXISTS pairing_secrets (
                        pairing_id TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        role TEXT NOT NULL CHECK (
                            role IN ('snapshot', 'dispatch', 'management')
                        ),
                        secret_ref TEXT NOT NULL UNIQUE,
                        nonce BLOB NOT NULL CHECK (length(nonce) = 12),
                        ciphertext BLOB NOT NULL CHECK (length(ciphertext) >= 16),
                        PRIMARY KEY (pairing_id, generation, role)
                    );
                    CREATE TABLE IF NOT EXISTS pairing_private_material (
                        pairing_id TEXT NOT NULL,
                        invitation_id TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (
                            role IN ('invitation', 'locator')
                        ),
                        value_ref TEXT NOT NULL UNIQUE,
                        nonce BLOB NOT NULL CHECK (length(nonce) = 12),
                        ciphertext BLOB NOT NULL CHECK (length(ciphertext) >= 16),
                        PRIMARY KEY (pairing_id, role)
                    );
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                metadata = conn.execute(
                    """
                    SELECT schema_version, store_id,
                           key_check_nonce, key_check_ciphertext
                    FROM secret_store_metadata
                    WHERE singleton=1
                    """
                ).fetchone()
                if metadata is None:
                    key_check_nonce = os.urandom(_NONCE_BYTES)
                    key_check_ciphertext = AESGCM(self._key).encrypt(
                        key_check_nonce,
                        _KEY_CHECK_PLAINTEXT,
                        self._key_check_associated_data(),
                    )
                    conn.execute(
                        """
                        INSERT INTO secret_store_metadata(
                            singleton, schema_version, store_id,
                            key_check_nonce, key_check_ciphertext
                        ) VALUES (1, ?, ?, ?, ?)
                        """,
                        (
                            _SCHEMA_VERSION,
                            self._store_id,
                            key_check_nonce,
                            key_check_ciphertext,
                        ),
                    )
                elif (
                    metadata["schema_version"] != _SCHEMA_VERSION
                    or metadata["store_id"] != self._store_id
                ):
                    raise SecretStoreConfigurationError(
                        "secret_store_identity_mismatch"
                    )
                else:
                    try:
                        key_check = AESGCM(self._key).decrypt(
                            bytes(metadata["key_check_nonce"]),
                            bytes(metadata["key_check_ciphertext"]),
                            self._key_check_associated_data(),
                        )
                    except (InvalidTag, TypeError, ValueError):
                        raise SecretStoreIntegrityError(
                            "secret_store_integrity_failure"
                        ) from None
                    if not hmac.compare_digest(
                        key_check,
                        _KEY_CHECK_PLAINTEXT,
                    ):
                        raise SecretStoreIntegrityError(
                            "secret_store_integrity_failure"
                        )

                # Authenticate an existing store immediately so a wrong key or
                # on-disk tamper fails during restart initialization.
                existing = conn.execute(
                    """
                    SELECT pairing_id, generation
                    FROM pairing_secrets
                    ORDER BY pairing_id, generation
                    LIMIT 1
                    """
                ).fetchone()
                if existing is not None:
                    rows = self._select_bundle_rows(
                        conn,
                        existing["pairing_id"],
                        existing["generation"],
                    )
                    self._decrypt_bundle_rows(rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _create_bundle_sync(self, bundle: CredentialBundle) -> CredentialBundle:
        encrypted: list[tuple[str, int, str, str, bytes, bytes]] = []
        aesgcm = AESGCM(self._key)
        for role in _ROLES:
            credential = bundle.credential(role)
            nonce = os.urandom(_NONCE_BYTES)
            ciphertext = aesgcm.encrypt(
                nonce,
                credential.secret.encode("utf-8"),
                self._associated_data(
                    bundle.pairing_id,
                    role,
                    bundle.generation,
                    credential.secret_ref,
                ),
            )
            encrypted.append(
                (
                    bundle.pairing_id,
                    bundle.generation,
                    role,
                    credential.secret_ref,
                    nonce,
                    ciphertext,
                )
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._select_bundle_rows(
                    conn,
                    bundle.pairing_id,
                    bundle.generation,
                )
                if rows:
                    existing = self._decrypt_bundle_rows(rows)
                    if not _bundles_match(existing, bundle):
                        raise SecretStoreConflictError("secret_store_bundle_conflict")
                    conn.commit()
                    return existing
                references = tuple(
                    bundle.credential(role).secret_ref for role in _ROLES
                )
                placeholders = ",".join("?" for _ in references)
                if conn.execute(
                    f"""
                    SELECT 1
                    FROM pairing_private_material
                    WHERE value_ref IN ({placeholders})
                    LIMIT 1
                    """,
                    references,
                ).fetchone() is not None:
                    raise SecretStoreConflictError(
                        "secret_store_bundle_conflict"
                    )
                try:
                    conn.executemany(
                        """
                        INSERT INTO pairing_secrets(
                            pairing_id, generation, role, secret_ref,
                            nonce, ciphertext
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        encrypted,
                    )
                except sqlite3.IntegrityError as exc:
                    raise SecretStoreConflictError(
                        "secret_store_bundle_conflict"
                    ) from exc
                conn.commit()
                return bundle
            except BaseException:
                conn.rollback()
                raise

    def _create_pairing_material_sync(
        self,
        material: PairingMaterial,
    ) -> PairingMaterial:
        encrypted: list[tuple[str, str, str, str, bytes, bytes]] = []
        aesgcm = AESGCM(self._key)
        for role in _PAIRING_MATERIAL_ROLES:
            private_value = material.material(role)
            nonce = os.urandom(_NONCE_BYTES)
            ciphertext = aesgcm.encrypt(
                nonce,
                private_value.value.encode("utf-8"),
                self._pairing_material_associated_data(
                    material.pairing_id,
                    material.invitation_id,
                    role,
                    private_value.value_ref,
                ),
            )
            encrypted.append(
                (
                    material.pairing_id,
                    material.invitation_id,
                    role,
                    private_value.value_ref,
                    nonce,
                    ciphertext,
                )
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._select_pairing_material_rows(
                    conn,
                    material.pairing_id,
                    material.invitation_id,
                )
                if rows:
                    existing = self._decrypt_pairing_material_rows(rows)
                    if not _pairing_material_matches(existing, material):
                        raise SecretStoreConflictError(
                            "secret_store_pairing_material_conflict"
                        )
                    conn.commit()
                    return existing
                references = tuple(
                    material.material(role).value_ref
                    for role in _PAIRING_MATERIAL_ROLES
                )
                placeholders = ",".join("?" for _ in references)
                if conn.execute(
                    f"""
                    SELECT 1 FROM pairing_secrets
                    WHERE secret_ref IN ({placeholders})
                    LIMIT 1
                    """,
                    references,
                ).fetchone() is not None:
                    raise SecretStoreConflictError(
                        "secret_store_pairing_material_conflict"
                    )
                try:
                    conn.executemany(
                        """
                        INSERT INTO pairing_private_material(
                            pairing_id, invitation_id, role, value_ref,
                            nonce, ciphertext
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        encrypted,
                    )
                except sqlite3.IntegrityError as exc:
                    raise SecretStoreConflictError(
                        "secret_store_pairing_material_conflict"
                    ) from exc
                conn.commit()
                return material
            except BaseException:
                conn.rollback()
                raise

    def _load_pairing_material_sync(
        self,
        pairing_id: str,
        invitation_id: str,
    ) -> PairingMaterial | None:
        with self._connect() as conn:
            rows = self._select_pairing_material_rows(
                conn,
                pairing_id,
                invitation_id,
            )
        if not rows:
            return None
        return self._decrypt_pairing_material_rows(rows)

    def _verify_pairing_material_sync(
        self,
        pairing_id: str,
        invitation_id: str,
        role: PairingMaterialRole,
        value_ref: str,
        candidate: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT pairing_id, invitation_id, role, value_ref,
                       nonce, ciphertext
                FROM pairing_private_material
                WHERE pairing_id=? AND invitation_id=? AND role=?
                """,
                (pairing_id, invitation_id, role),
            ).fetchone()
        if row is None or row["value_ref"] != value_ref:
            raise SecretStoreIntegrityError("secret_store_integrity_failure")
        stored = self._decrypt_pairing_material_value(row)
        return hmac.compare_digest(
            stored.value.encode("utf-8"),
            candidate.encode("utf-8"),
        )

    def _load_locator_sync(self, pairing_id: str, locator_ref: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT pairing_id, invitation_id, role, value_ref,
                       nonce, ciphertext
                FROM pairing_private_material
                WHERE pairing_id=? AND role='locator'
                """,
                (pairing_id,),
            ).fetchone()
        if row is None or row["value_ref"] != locator_ref:
            raise SecretStoreIntegrityError("secret_store_integrity_failure")
        return self._decrypt_pairing_material_value(row).value

    def _delete_pairing_material_sync(
        self,
        pairing_id: str,
        invitation_id: str,
        *,
        invitation_only: bool,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if invitation_only:
                    deleted = conn.execute(
                        """
                        DELETE FROM pairing_private_material
                        WHERE pairing_id=? AND invitation_id=?
                          AND role='invitation'
                        """,
                        (pairing_id, invitation_id),
                    ).rowcount
                    if deleted not in (0, 1):
                        raise SecretStoreIntegrityError(
                            "secret_store_integrity_failure"
                        )
                else:
                    deleted = conn.execute(
                        """
                        DELETE FROM pairing_private_material
                        WHERE pairing_id=? AND invitation_id=?
                        """,
                        (pairing_id, invitation_id),
                    ).rowcount
                    if deleted < 0 or deleted > len(_PAIRING_MATERIAL_ROLES):
                        raise SecretStoreIntegrityError(
                            "secret_store_integrity_failure"
                        )
                conn.commit()
                return deleted > 0
            except BaseException:
                conn.rollback()
                raise

    def _select_pairing_material_rows(
        self,
        conn: sqlite3.Connection,
        pairing_id: str,
        invitation_id: str,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT pairing_id, invitation_id, role, value_ref,
                   nonce, ciphertext
            FROM pairing_private_material
            WHERE pairing_id=? AND invitation_id=?
            ORDER BY role
            """,
            (pairing_id, invitation_id),
        ).fetchall()

    def _decrypt_pairing_material_rows(
        self,
        rows: list[sqlite3.Row],
    ) -> PairingMaterial:
        if len(rows) != len(_PAIRING_MATERIAL_ROLES):
            raise SecretStoreIntegrityError("secret_store_integrity_failure")
        values: dict[PairingMaterialRole, PrivateValue] = {}
        pairing_id: str | None = None
        invitation_id: str | None = None
        for row in rows:
            value = self._decrypt_pairing_material_value(row)
            role = row["role"]
            if role not in _PAIRING_MATERIAL_ROLES or role in values:
                raise SecretStoreIntegrityError("secret_store_integrity_failure")
            if pairing_id is None:
                pairing_id = row["pairing_id"]
                invitation_id = row["invitation_id"]
            elif (
                pairing_id != row["pairing_id"]
                or invitation_id != row["invitation_id"]
            ):
                raise SecretStoreIntegrityError("secret_store_integrity_failure")
            values[role] = value
        if pairing_id is None or invitation_id is None or set(values) != set(
            _PAIRING_MATERIAL_ROLES
        ):
            raise SecretStoreIntegrityError("secret_store_integrity_failure")
        material = PairingMaterial(
            pairing_id=pairing_id,
            invitation_id=invitation_id,
            invitation=values["invitation"],
            locator=values["locator"],
        )
        try:
            _validate_pairing_material(material)
        except SecretStoreValidationError:
            raise SecretStoreIntegrityError(
                "secret_store_integrity_failure"
            ) from None
        return material

    def _decrypt_pairing_material_value(
        self,
        row: sqlite3.Row,
    ) -> PrivateValue:
        try:
            pairing_id = _validate_pairing_id(row["pairing_id"])
            invitation_id = _validate_pairing_id(row["invitation_id"])
            role = row["role"]
            if role not in _PAIRING_MATERIAL_ROLES:
                raise SecretStoreIntegrityError(
                    "secret_store_integrity_failure"
                )
            value_ref = _validate_secret_ref(row["value_ref"])
            nonce = bytes(row["nonce"])
            ciphertext = bytes(row["ciphertext"])
            if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
                raise SecretStoreIntegrityError(
                    "secret_store_integrity_failure"
                )
            plaintext = AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                self._pairing_material_associated_data(
                    pairing_id,
                    invitation_id,
                    role,
                    value_ref,
                ),
            ).decode("utf-8")
            return PrivateValue(
                value_ref=value_ref,
                value=_validate_private_value(plaintext),
            )
        except SecretStoreIntegrityError:
            raise
        except (
            InvalidTag,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            SecretStoreValidationError,
        ):
            raise SecretStoreIntegrityError(
                "secret_store_integrity_failure"
            ) from None

    def _load_bundle_sync(
        self,
        pairing_id: str,
        generation: int,
    ) -> CredentialBundle | None:
        with self._connect() as conn:
            rows = self._select_bundle_rows(conn, pairing_id, generation)
        if not rows:
            return None
        return self._decrypt_bundle_rows(rows)

    def _delete_bundle_sync(self, pairing_id: str, generation: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute(
                    """
                    SELECT count(*)
                    FROM pairing_secrets
                    WHERE pairing_id=? AND generation=?
                    """,
                    (pairing_id, generation),
                ).fetchone()[0]
                if count not in (0, len(_ROLES)):
                    raise SecretStoreIntegrityError(
                        "secret_store_integrity_failure"
                    )
                if count == 0:
                    conn.commit()
                    return False
                deleted = conn.execute(
                    """
                    DELETE FROM pairing_secrets
                    WHERE pairing_id=? AND generation=?
                    """,
                    (pairing_id, generation),
                ).rowcount
                if deleted != len(_ROLES):
                    raise SecretStoreIntegrityError(
                        "secret_store_integrity_failure"
                    )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def _select_bundle_rows(
        self,
        conn: sqlite3.Connection,
        pairing_id: str,
        generation: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT pairing_id, generation, role, secret_ref, nonce, ciphertext
            FROM pairing_secrets
            WHERE pairing_id=? AND generation=?
            ORDER BY role
            """,
            (pairing_id, generation),
        ).fetchall()

    def _decrypt_bundle_rows(
        self,
        rows: list[sqlite3.Row],
    ) -> CredentialBundle:
        if len(rows) != len(_ROLES):
            raise SecretStoreIntegrityError("secret_store_integrity_failure")

        credentials: dict[CredentialRole, CredentialSecret] = {}
        pairing_id: str | None = None
        generation: int | None = None
        aesgcm = AESGCM(self._key)
        try:
            for row in rows:
                row_pairing_id = _validate_pairing_id(row["pairing_id"])
                row_generation = _validate_generation(row["generation"])
                role = row["role"]
                if role not in _ROLES or role in credentials:
                    raise SecretStoreIntegrityError(
                        "secret_store_integrity_failure"
                    )
                secret_ref = _validate_secret_ref(row["secret_ref"])
                nonce = bytes(row["nonce"])
                ciphertext = bytes(row["ciphertext"])
                if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
                    raise SecretStoreIntegrityError(
                        "secret_store_integrity_failure"
                    )
                if pairing_id is None:
                    pairing_id = row_pairing_id
                    generation = row_generation
                elif (
                    pairing_id != row_pairing_id
                    or generation != row_generation
                ):
                    raise SecretStoreIntegrityError(
                        "secret_store_integrity_failure"
                    )
                plaintext = aesgcm.decrypt(
                    nonce,
                    ciphertext,
                    self._associated_data(
                        row_pairing_id,
                        role,
                        row_generation,
                        secret_ref,
                    ),
                ).decode("utf-8")
                credentials[role] = CredentialSecret(
                    secret_ref=secret_ref,
                    secret=_validate_secret(plaintext),
                )
        except SecretStoreIntegrityError:
            raise
        except (InvalidTag, UnicodeDecodeError, TypeError, ValueError):
            raise SecretStoreIntegrityError(
                "secret_store_integrity_failure"
            ) from None
        except SecretStoreValidationError:
            raise SecretStoreIntegrityError(
                "secret_store_integrity_failure"
            ) from None

        if (
            pairing_id is None
            or generation is None
            or set(credentials) != set(_ROLES)
        ):
            raise SecretStoreIntegrityError("secret_store_integrity_failure")
        bundle = CredentialBundle(
            pairing_id=pairing_id,
            generation=generation,
            snapshot=credentials["snapshot"],
            dispatch=credentials["dispatch"],
            management=credentials["management"],
        )
        try:
            _validate_bundle(bundle)
        except SecretStoreValidationError:
            raise SecretStoreIntegrityError(
                "secret_store_integrity_failure"
            ) from None
        return bundle

    def _associated_data(
        self,
        pairing_id: str,
        role: CredentialRole,
        generation: int,
        secret_ref: str,
    ) -> bytes:
        return json.dumps(
            {
                "generation": generation,
                "pairing_id": pairing_id,
                "role": role,
                "secret_ref": secret_ref,
                "store_id": self._store_id,
                "version": _SCHEMA_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _pairing_material_associated_data(
        self,
        pairing_id: str,
        invitation_id: str,
        role: PairingMaterialRole,
        value_ref: str,
    ) -> bytes:
        return json.dumps(
            {
                "generation": 0,
                "invitation_id": invitation_id,
                "pairing_id": pairing_id,
                "role": role,
                "secret_ref": value_ref,
                "store_id": self._store_id,
                "version": _SCHEMA_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _key_check_associated_data(self) -> bytes:
        return json.dumps(
            {
                "purpose": "key_check",
                "store_id": self._store_id,
                "version": _SCHEMA_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _prepare_private_path(self) -> None:
        parent = self._path.parent
        try:
            parent_status = parent.lstat()
        except FileNotFoundError:
            try:
                parent.mkdir(mode=0o700, parents=True)
                os.chmod(parent, 0o700, follow_symlinks=False)
            except (FileExistsError, NotADirectoryError, OSError) as exc:
                raise SecretStorePathError("secret_store_insecure_path") from exc
        else:
            _assert_private_directory(parent_status)

        _assert_private_directory(_safe_lstat(parent))

        try:
            database_status = self._path.lstat()
        except FileNotFoundError:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._path, flags, 0o600)
            except OSError as exc:
                raise SecretStorePathError("secret_store_insecure_path") from exc
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            database_status = _safe_lstat(self._path)
        _assert_private_file(database_status)

    def _connect(self) -> sqlite3.Connection:
        _assert_private_directory(_safe_lstat(self._path.parent))
        before = _safe_lstat(self._path)
        _assert_private_file(before)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self._path,
                timeout=10,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            after = _safe_lstat(self._path)
            _assert_private_file(after)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                conn.close()
                raise SecretStorePathError("secret_store_insecure_path")
            conn.execute("PRAGMA foreign_keys=ON")
            journal_mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()[0]
            if str(journal_mode).lower() != "delete" or int(secure_delete) != 1:
                conn.close()
                raise SecretStorePathError("secret_store_database_failure")
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except SecretStoreError:
            if conn is not None:
                conn.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise SecretStorePathError("secret_store_database_failure") from exc


def _decode_master_key(encoded: str) -> bytes:
    if not isinstance(encoded, str) or _MASTER_KEY_RE.fullmatch(encoded) is None:
        raise SecretStoreConfigurationError("secret_store_invalid_master_key")
    unpadded = encoded.rstrip("=")
    try:
        raw = base64.b64decode(
            unpadded + "=" * (-len(unpadded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise SecretStoreConfigurationError(
            "secret_store_invalid_master_key"
        ) from None
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(raw) != 32 or not hmac.compare_digest(unpadded, canonical):
        raise SecretStoreConfigurationError("secret_store_invalid_master_key")
    return raw


def _current_uid() -> int:
    return os.getuid()


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise SecretStorePathError("secret_store_insecure_path") from exc


def _assert_private_directory(status: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != _current_uid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise SecretStorePathError("secret_store_insecure_path")


def _assert_private_file(status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != _current_uid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise SecretStorePathError("secret_store_insecure_path")


def _validate_text(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SecretStoreValidationError(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SecretStoreValidationError(code) from None
    if len(encoded) > maximum:
        raise SecretStoreValidationError(code)
    return value


def _validate_pairing_id(value: object) -> str:
    return _validate_text(
        value,
        maximum=_MAX_PAIRING_ID_BYTES,
        code="secret_store_invalid_bundle",
    )


def _validate_secret_ref(value: object) -> str:
    return _validate_text(
        value,
        maximum=_MAX_SECRET_REF_BYTES,
        code="secret_store_invalid_bundle",
    )


def _validate_secret(value: object) -> str:
    return _validate_text(
        value,
        maximum=_MAX_SECRET_BYTES,
        code="secret_store_invalid_bundle",
    )


def _validate_private_value(value: object) -> str:
    return _validate_text(
        value,
        maximum=_MAX_PRIVATE_VALUE_BYTES,
        code="secret_store_invalid_pairing_material",
    )


def _validate_generation(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_GENERATION
    ):
        raise SecretStoreValidationError("secret_store_invalid_bundle")
    return value


def _validate_bundle(bundle: object) -> None:
    if not isinstance(bundle, CredentialBundle):
        raise SecretStoreValidationError("secret_store_invalid_bundle")
    _validate_pairing_id(bundle.pairing_id)
    _validate_generation(bundle.generation)
    secrets: list[bytes] = []
    references: set[str] = set()
    for role in _ROLES:
        credential = bundle.credential(role)
        if not isinstance(credential, CredentialSecret):
            raise SecretStoreValidationError("secret_store_invalid_bundle")
        reference = _validate_secret_ref(credential.secret_ref)
        secret = _validate_secret(credential.secret)
        if reference in references:
            raise SecretStoreValidationError("secret_store_invalid_bundle")
        references.add(reference)
        secrets.append(secret.encode("utf-8"))
    if any(
        hmac.compare_digest(left, right)
        for index, left in enumerate(secrets)
        for right in secrets[index + 1 :]
    ):
        raise SecretStoreValidationError("secret_store_invalid_bundle")


def _validate_pairing_material(material: object) -> None:
    if not isinstance(material, PairingMaterial):
        raise SecretStoreValidationError(
            "secret_store_invalid_pairing_material"
        )
    _validate_pairing_id(material.pairing_id)
    _validate_pairing_id(material.invitation_id)
    references: set[str] = set()
    for role in _PAIRING_MATERIAL_ROLES:
        private_value = material.material(role)
        if not isinstance(private_value, PrivateValue):
            raise SecretStoreValidationError(
                "secret_store_invalid_pairing_material"
            )
        reference = _validate_secret_ref(private_value.value_ref)
        _validate_private_value(private_value.value)
        if reference in references:
            raise SecretStoreValidationError(
                "secret_store_invalid_pairing_material"
            )
        references.add(reference)


def _bundles_match(left: CredentialBundle, right: CredentialBundle) -> bool:
    if left.pairing_id != right.pairing_id or left.generation != right.generation:
        return False
    for role in _ROLES:
        left_credential = left.credential(role)
        right_credential = right.credential(role)
        if left_credential.secret_ref != right_credential.secret_ref:
            return False
        if not hmac.compare_digest(
            left_credential.secret.encode("utf-8"),
            right_credential.secret.encode("utf-8"),
        ):
            return False
    return True


def _pairing_material_matches(
    left: PairingMaterial,
    right: PairingMaterial,
) -> bool:
    if (
        left.pairing_id != right.pairing_id
        or left.invitation_id != right.invitation_id
    ):
        return False
    for role in _PAIRING_MATERIAL_ROLES:
        left_value = left.material(role)
        right_value = right.material(role)
        if left_value.value_ref != right_value.value_ref:
            return False
        if not hmac.compare_digest(
            left_value.value.encode("utf-8"),
            right_value.value.encode("utf-8"),
        ):
            return False
    return True

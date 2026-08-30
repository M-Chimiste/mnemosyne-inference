from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
MAC_SERVICE_SRC = ROOT / "macos" / "service" / "src"
if str(MAC_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(MAC_SERVICE_SRC))

from mnemosyne_fleet.compatibility_catalog import (  # noqa: E402
    CatalogVerifier,
    TrustedCatalogKey,
    catalog_digest,
    signing_message,
)
from mnemosyne_fleet.inventory_protocol import validate_inventory  # noqa: E402
from mnemosyne_fleet.inventory_store import InventoryStore  # noqa: E402
from mnemosyne_fleet.placement import (  # noqa: E402
    PlacementCandidateInput,
    PlacementRequest,
    PlacementScorer,
    RecipeRequirements,
)
from mnemosyne_macos.config import MacConfig  # noqa: E402
from mnemosyne_macos.coordinator import CoordinatorState  # noqa: E402
from mnemosyne_macos.mac_inventory import (  # noqa: E402
    DefaultMacHardwareProbe,
    MacInventoryProducer,
)
from mnemosyne_macos.mac_inventory_store import MacInventoryIndex  # noqa: E402
from mnemosyne_macos.models import EngineName  # noqa: E402
from mnemosyne_macos.storage import StorageStatus  # noqa: E402


TEST_NOW = 1_790_000_000.0
TEST_SEED = bytes(range(1, 33))
PAIRING_ID = "11111111-1111-4111-8111-111111111111"
INSTANCE_ID = "22222222-2222-4222-8222-222222222222"


class _Filesystem:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def inspect(self, _path: str, *, name: str, **_kwargs) -> StorageStatus:
        return StorageStatus(
            name=name,
            path=str(self.root),
            exists=True,
            is_directory=True,
            writable=True,
            mount_path="/",
            volume_uuid=None,
            expected_volume_uuid=None,
            volume_matches=True,
            total_bytes=512 * 1024**3,
            free_bytes=384 * 1024**3,
            diagnostic=None,
        )


class _RuntimeUpdates:
    async def installed_status(self) -> dict[str, dict[str, object]]:
        return {
            EngineName.LLAMA_CPP.value: {
                "installed": True,
                "version": "b7000",
            }
        }


class _Installer:
    async def evidence(self, *, limit: int) -> list[dict[str, object]]:
        assert limit == 10_000
        return []


class _AsyncStatus:
    def __init__(self, value: object) -> None:
        self.value = value

    async def status(self) -> object:
        return self.value


class _CatalogIdentity:
    def __init__(self, version: str, digest: str) -> None:
        self.version = version
        self.digest = digest

    async def inventory_identity(self) -> tuple[str, str]:
        return self.version, self.digest


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _verified_catalog():
    protocol = ROOT / "compatibility_catalog" / "v1"
    envelope = json.loads(
        (protocol / "catalog.golden.json").read_text(encoding="utf-8")
    )
    recipe = envelope["catalog"]["recipes"][0]
    recipe["compatibility_tier"] = "verified"
    recipe["runtime"]["release_tier"] = "stable"
    recipe["runtime"]["known_bad_versions"] = []
    # Keep the signed hardware and runtime feature requirements intact. This
    # is the producer/scorer seam that fixture-only tests previously skipped.
    assert recipe["hardware"]["required_features"] == [
        "metal",
        "unified-memory",
    ]
    assert recipe["runtime"]["required_features"] == [
        "apple-metal",
        "flash-attention",
    ]
    envelope["catalog_digest"] = catalog_digest(envelope["catalog"])
    envelope["signatures"] = [
        {
            "key_id": "test-catalog-2026-a",
            "algorithm": "Ed25519",
            "signature": _encode(
                Ed25519PrivateKey.from_private_bytes(TEST_SEED).sign(
                    signing_message(envelope["catalog"])
                )
            ),
        }
    ]
    key_value = json.loads(
        (protocol / "test_keys.json").read_text(encoding="utf-8")
    )["keys"][0]
    key = TrustedCatalogKey.from_base64url(
        key_id=key_value["key_id"],
        public_key=key_value["public_key"],
    )
    return CatalogVerifier({key.key_id: key}).verify(
        envelope,
        now=int(TEST_NOW),
    )


def _runtime(tmp_path: Path, verified) -> SimpleNamespace:
    model_root = tmp_path / "models"
    model_root.mkdir()
    config = MacConfig.model_validate(
        {
            "schema_version": 6,
            "engines": {
                "llama_cpp": {"enabled": True},
                "omlx": {"enabled": False},
                "ds4": {"enabled": False},
                "mflux": {"enabled": False},
                "mlxcel": {"enabled": False},
                "mistral_rs": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "native-state.db")},
            "storage": {
                "default": "internal",
                "locations": [
                    {"name": "internal", "path": str(model_root)}
                ],
            },
            "models": [],
        }
    )
    coordinator = SimpleNamespace(
        state=CoordinatorState.IDLE,
        resident_alias=None,
        resident_engine=None,
        resident_model=None,
        transition_target=None,
        transition_engine=None,
    )
    participation = SimpleNamespace(state=SimpleNamespace(value="joined"))
    return SimpleNamespace(
        config=config,
        filesystem=_Filesystem(model_root),
        runtime_updates=_RuntimeUpdates(),
        _runtime_fingerprints={},
        installer=_Installer(),
        coordinator=_AsyncStatus(coordinator),
        fleet_participation=_AsyncStatus(participation),
        usage=_AsyncStatus(
            {
                "enabled": True,
                "writer_ready": True,
                "outbox_pending": 0,
                "last_flush_at": TEST_NOW,
                "last_error_code": None,
            }
        ),
        compatibility_catalog=_CatalogIdentity(
            verified.catalog_version,
            verified.catalog_digest,
        ),
        _desired_install_executor_available=True,
    )


async def test_native_producer_store_and_signed_recipe_form_eligible_placement(
    tmp_path,
    monkeypatch,
) -> None:
    verified = _verified_catalog()
    sysctls = {
        "hw.memsize": str(64 * 1024**3),
        "hw.perflevel0.physicalcpu": "8",
        "hw.perflevel1.physicalcpu": "4",
    }
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_sysctl",
        classmethod(lambda _cls, name: sysctls.get(name)),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_display_facts",
        classmethod(
            lambda cls: cls._parse_display_facts(
                json.dumps(
                    {
                        "SPDisplaysDataType": [
                            {
                                "sppci_device_type": "spdisplays_gpu",
                                "spdisplays_vendor": "sppci_vendor_Apple",
                                "sppci_bus": "spdisplays_builtin",
                                "spdisplays_metal": "spdisplays_supported",
                                "sppci_model": "Apple M2 Pro",
                                "sppci_cores": "16",
                            }
                        ]
                    }
                ).encode("utf-8")
            )
        ),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_os_version",
        staticmethod(lambda: (15, 6)),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_power",
        staticmethod(lambda: ("ac", False)),
    )

    producer = MacInventoryProducer(
        _runtime(tmp_path, verified),
        MacInventoryIndex(tmp_path / "native-inventory.db"),
        wall_clock=lambda: TEST_NOW,
        instance_id=INSTANCE_ID,
    )
    await producer.initialize()
    try:
        produced = await producer.next_document(
            pairing_id=PAIRING_ID,
            credential_generation=1,
        )
    finally:
        await producer.close()
    assert produced["hardware"]["probe_version"] == 2
    assert produced["hardware"]["gpu_cores"] == 16
    llama = next(
        row for row in produced["runtimes"] if row["engine"] == "llama.cpp"
    )
    assert llama["catalog_status"] == "available"
    assert llama["health"] == "unknown"

    store = InventoryStore(
        tmp_path / "fleet-inventory.db",
        freshness_ttl_seconds=60,
        process_instance_id="33333333-3333-4333-8333-333333333333",
        wall_clock=lambda: TEST_NOW,
        monotonic_clock=lambda: 10.0,
    )
    await store.initialize()
    accepted = await store.accept(validate_inventory(produced))
    record = accepted.record
    placement_request = PlacementRequest(
        recommendation_id="44444444-4444-4444-8444-444444444444",
        created_at=TEST_NOW,
        valid_for_seconds=60,
        logical_model_id="example-flash-vnext",
        recipe_id="example-flash-vnext-llamacpp-q4",
        required_capabilities=("chat/completions", "responses"),
        required_context_tokens=8192,
        required_concurrency=2,
    )
    requirements = RecipeRequirements.from_verified_catalog(
        verified,
        request=placement_request,
        runtime_install_mode="not_allowed",
    )
    candidate = PlacementCandidateInput(
        pairing_id=PAIRING_ID,
        pairing_display_name="Production-shaped Mac",
        service_class="primary",
        enrollment_state="active",
        active_credential_generation=1,
        freshness_state="fresh",
        inventory_received_at=record.received_at,
        basis_expires_at=record.received_at + 60,
        inventory=record.inventory,
    )
    recommendation = PlacementScorer().score(
        placement_request,
        requirements,
        (candidate,),
    ).value
    eligible = [row for row in recommendation["candidates"] if row["eligible"]]
    assert len(eligible) == 1
    selected = eligible[0]
    assert selected["runtime_state"] == "compatible_unverified"
    assert selected["evidence"]["runtime"] == "conservative"
    assert not {
        "gpu_requirement_unmet",
        "hardware_feature_evidence_missing",
        "runtime_feature_evidence_missing",
        "runtime_unhealthy",
    }.intersection(selected["hard_gate_codes"])

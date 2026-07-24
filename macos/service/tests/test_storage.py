from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_macos.config import MacConfig, StorageLocationConfig
from mnemosyne_macos.models import EngineName
from mnemosyne_macos.storage import (
    install_destination,
    inspect_location,
    inspect_path,
)


def test_nested_external_folder_keeps_exact_path_and_volume_identity(
    tmp_path, monkeypatch
) -> None:
    nested = tmp_path / "Volumes" / "Athena" / "models"
    nested.mkdir(parents=True)
    monkeypatch.setattr(
        "mnemosyne_macos.storage._volume_identity",
        lambda _path: ("/Volumes/Athena", "ATHENA-UUID"),
    )

    status = inspect_location(
        StorageLocationConfig(
            name="athena-models",
            path=str(nested),
            volume_uuid="athena-uuid",
        )
    )

    assert status.path == str(nested.resolve())
    assert status.mount_path == "/Volumes/Athena"
    assert status.volume_matches is True
    assert status.writable is True


def test_storage_status_preserves_a_selected_symlink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "Volumes" / "Athena" / "nested" / "models"
    nested.mkdir(parents=True)
    selected = tmp_path / "home" / ".lmstudio" / "models"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(nested, target_is_directory=True)
    monkeypatch.setattr(
        "mnemosyne_macos.storage._volume_identity",
        lambda path: (
            "/Volumes/Athena",
            "ATHENA-UUID",
        )
        if path == nested.resolve()
        else pytest.fail("volume inspection did not use the resolved target"),
    )

    status = inspect_path(str(selected))

    assert status.path == str(selected)
    assert status.mount_path == "/Volumes/Athena"
    assert status.volume_uuid == "ATHENA-UUID"
    assert status.is_directory is True


def test_volume_identity_mismatch_fails_closed(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "models"
    folder.mkdir()
    monkeypatch.setattr(
        "mnemosyne_macos.storage._volume_identity",
        lambda _path: ("/Volumes/Other", "OTHER-UUID"),
    )

    status = inspect_path(
        str(folder), expected_volume_uuid="ATHENA-UUID"
    )

    assert status.volume_matches is False
    assert status.diagnostic == "the folder is not on the volume originally selected"


def test_install_destination_is_engine_scoped_and_path_safe(tmp_path) -> None:
    destination = install_destination(
        tmp_path, EngineName.OMLX, "mlx-community/GLM-5.2-4bit"
    )
    assert destination == (
        tmp_path / "omlx" / "mlx-community" / "GLM-5.2-4bit"
    ).resolve()

    with pytest.raises(Exception, match="owner/model"):
        install_destination(tmp_path, EngineName.OMLX, "../escape")


def test_storage_configuration_accepts_nested_paths_and_validates_profiles() -> None:
    config = MacConfig.model_validate(
        {
            "storage": {
                "default": "athena-models",
                "locations": [
                    {
                        "name": "athena-models",
                        "path": "/Volumes/Athena/models",
                        "volume_uuid": "ATHENA-UUID",
                    }
                ],
            },
            "models": [
                {
                    "alias": "glm",
                    "engine": "omlx",
                    "model": "mlx-community/GLM",
                    "storage": "athena-models",
                }
            ],
        }
    )
    assert config.storage.locations[0].path == "/Volumes/Athena/models"

    with pytest.raises(ValueError, match="unknown storage"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "glm",
                        "engine": "omlx",
                        "model": "mlx-community/GLM",
                        "storage": "missing",
                    }
                ]
            }
        )

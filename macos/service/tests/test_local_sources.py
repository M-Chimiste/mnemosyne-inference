from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import mnemosyne_macos.app as app_module
from mnemosyne_macos.app import create_control_app
from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.local_sources import LocalModelSource
from mnemosyne_macos.local_sources import discover_local_model_sources


def test_lmstudio_settings_source_preserves_nested_symlink_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    external = tmp_path / "Volumes" / "Athena" / "models"
    external.mkdir(parents=True)
    selected = home / "Model Links" / "lmstudio"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(external, target_is_directory=True)
    settings = home / ".lmstudio" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"downloadsFolder": str(selected)}),
        encoding="utf-8",
    )

    sources = discover_local_model_sources(home=home)

    assert [source.to_dict() for source in sources] == [
        {
            "id": "lmstudio-downloads",
            "display_name": "LM Studio model folder",
            "path": str(selected),
            "source": "lmstudio-settings",
        },
        {
            "id": "lmstudio-models",
            "display_name": "LM Studio default model folder",
            "path": str(home / ".lmstudio" / "models"),
            "source": "lmstudio-conventional",
        },
    ]


def test_documented_default_is_a_hint_even_when_it_does_not_exist(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    sources = discover_local_model_sources(home=home)

    assert len(sources) == 1
    assert sources[0].id == "lmstudio-models"
    assert sources[0].path == str(home / ".lmstudio" / "models")
    assert sources[0].source == "lmstudio-conventional"


def test_invalid_settings_and_empty_conventional_folders_are_ignored(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings = home / ".lmstudio" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"downloadsFolder":"relative/models"}', encoding="utf-8")
    assert [source.path for source in discover_local_model_sources(home=home)] == [
        str(home / ".lmstudio" / "models")
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"downloadsFolder":"/Volumes/Athena/models\\u0000escape"}',
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_unsafe_or_oversize_settings_do_not_create_a_source(
    tmp_path: Path,
    payload: bytes,
) -> None:
    home = tmp_path / "home"
    settings = home / ".lmstudio" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(payload)

    assert [source.id for source in discover_local_model_sources(home=home)] == [
        "lmstudio-models"
    ]


@pytest.mark.asyncio
async def test_control_route_exposes_sources_when_lmstudio_engine_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(
        app_module,
        "discover_local_model_sources",
        lambda: [
            LocalModelSource(
                id="lmstudio-downloads",
                display_name="LM Studio model folder",
                path="/Volumes/Athena/models",
                source="lmstudio-settings",
            )
        ],
    )
    runtime = SimpleNamespace(config=MacConfig())
    assert runtime.config.engines.lmstudio.enabled is False
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.get("/manager/model-library/local-sources")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "sources": [
            {
                "id": "lmstudio-downloads",
                "display_name": "LM Studio model folder",
                "path": "/Volumes/Athena/models",
                "source": "lmstudio-settings",
            }
        ],
    }

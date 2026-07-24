"""Read-only discovery of model-library roots owned by other local tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any


_MAX_SETTINGS_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalModelSource:
    id: str
    display_name: str
    path: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lexical_path(value: str, *, home: Path) -> Path | None:
    if "\0" in value:
        return None
    if value == "~":
        expanded = str(home)
    elif value.startswith("~/"):
        expanded = os.path.join(str(home), value[2:])
    elif value.startswith("~"):
        return None
    else:
        expanded = value
    if not os.path.isabs(expanded):
        return None
    # Do not resolve here. LM Studio users commonly retain a stable path that
    # is itself a symlink to an external model volume.
    return Path(os.path.normpath(os.path.abspath(expanded)))


def _settings_downloads_folder(settings_path: Path, *, home: Path) -> Path | None:
    try:
        with settings_path.open("rb") as stream:
            raw = stream.read(_MAX_SETTINGS_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_SETTINGS_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("downloadsFolder")
    if not isinstance(value, str) or not value.strip():
        return None
    return _lexical_path(value.strip(), home=home)


def _source(
    *,
    source_id: str,
    display_name: str,
    path: Path,
    origin: str,
) -> LocalModelSource:
    return LocalModelSource(
        id=source_id,
        display_name=display_name,
        path=str(path),
        source=origin,
    )


def discover_local_model_sources(
    *,
    home: str | Path | None = None,
    lmstudio_settings_path: str | Path | None = None,
) -> list[LocalModelSource]:
    """Find LM Studio's on-disk model roots without contacting or enabling it."""

    home_path = Path(home).expanduser() if home is not None else Path.home()
    settings_path = (
        Path(lmstudio_settings_path).expanduser()
        if lmstudio_settings_path is not None
        else home_path / ".lmstudio" / "settings.json"
    )
    sources: list[LocalModelSource] = []
    seen: set[str] = set()

    configured = _settings_downloads_folder(settings_path, home=home_path)
    if configured is not None:
        normalized = os.path.normcase(str(configured))
        seen.add(normalized)
        sources.append(
            _source(
                source_id="lmstudio-downloads",
                display_name="LM Studio model folder",
                path=configured,
                origin="lmstudio-settings",
            )
        )

    # These are path hints only. Do not stat, resolve, or enumerate them in
    # the control process: either path may be a symlink to an offline external
    # volume. Finder and the bounded filesystem helper validate the user's
    # eventual selection.
    conventional = (
        (
            "lmstudio-models",
            home_path / ".lmstudio" / "models",
            "LM Studio default model folder",
        ),
    )
    for source_id, path, display_name in conventional:
        normalized = os.path.normcase(str(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        sources.append(
            _source(
                source_id=source_id,
                display_name=display_name,
                path=path,
                origin="lmstudio-conventional",
            )
        )
    return sources


__all__ = [
    "LocalModelSource",
    "discover_local_model_sources",
]

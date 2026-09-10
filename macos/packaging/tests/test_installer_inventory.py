import os
from pathlib import Path

import pytest

from macos.packaging.build_installer import inventory


def test_inventory_binds_hidden_files_permissions_and_relative_links(tmp_path: Path):
    (tmp_path / "code").write_bytes(b"signed code")
    (tmp_path / ".hidden").write_bytes(b"resource")
    (tmp_path / "Versions").mkdir()
    (tmp_path / "Versions" / "Current").symlink_to("../code")
    baseline = inventory(tmp_path)
    assert set(baseline) == {"code", ".hidden", "Versions", "Versions/Current"}
    assert baseline["Versions/Current"]["value"] == "../code"
    (tmp_path / "code").chmod(0o755)
    assert inventory(tmp_path)["code"] != baseline["code"]
    (tmp_path / ".hidden").write_bytes(b"changed")
    assert inventory(tmp_path)[".hidden"] != baseline[".hidden"]


def test_inventory_rejects_external_links_and_special_files(tmp_path: Path):
    payload = tmp_path / "app"
    payload.mkdir()
    (tmp_path / "outside").write_bytes(b"external")
    (payload / "link").symlink_to("../outside")
    with pytest.raises(ValueError, match="External payload link"):
        inventory(payload)
    (payload / "link").unlink()
    os.mkfifo(payload / "pipe")
    with pytest.raises(ValueError, match="Unsupported payload member"):
        inventory(payload)


def test_inventory_captures_extra_and_missing_files(tmp_path: Path):
    (tmp_path / "code").write_bytes(b"code")
    baseline = inventory(tmp_path)
    (tmp_path / "extra").write_bytes(b"extra")
    assert inventory(tmp_path) != baseline
    (tmp_path / "code").unlink()
    assert "code" not in inventory(tmp_path)

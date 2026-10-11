"""The isolated Preview builder never becomes runtime activation authority."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

import pytest

PREVIEW = Path(__file__).resolve().parents[2] / "tensorfold-preview"
spec = importlib.util.spec_from_file_location("tensorfold_preview_build", PREVIEW / "build_preview.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def copied_pin(tmp_path):
    target = tmp_path / "preview"
    shutil.copytree(PREVIEW, target)
    return target


def test_checked_in_patch_matches_exact_source_and_integrity_pin():
    pin = builder.load_pin()
    assert pin["revision"] == builder.REVISION
    assert pin["upstream"] == builder.UPSTREAM
    assert "tests/test_vision_flash_next_mlx.py" in pin["fixture_tests"]


def test_tampered_patch_is_rejected_before_any_build(tmp_path):
    directory = copied_pin(tmp_path)
    patch = directory / builder.load_pin()["patches"][0]["path"]
    patch.write_bytes(patch.read_bytes() + b"\nchanged\n")
    with pytest.raises(ValueError, match="integrity"):
        builder.load_pin(directory)


@pytest.mark.parametrize("field,value", [("revision", "0" * 40), ("upstream", "https://example.test/fork.git")])
def test_unapproved_source_pin_is_rejected(tmp_path, field, value):
    directory = copied_pin(tmp_path)
    pin_path = directory / "pin.json"
    pin = json.loads(pin_path.read_text())
    pin[field] = value
    pin_path.write_text(json.dumps(pin))
    with pytest.raises(ValueError, match="source pin"):
        builder.load_pin(directory)


def test_patch_parent_symlink_cannot_escape_preview_tree(tmp_path):
    directory = copied_pin(tmp_path)
    patches = directory / "patches"
    outside = tmp_path / "outside"
    patches.rename(outside)
    patches.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        builder.load_pin(directory)


def test_existing_output_is_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(builder, "run", lambda *a, **k: pytest.fail("must not launch a process"))
    with pytest.raises(ValueError, match="never replaced"):
        builder.build(tmp_path, python="unused")


def test_candidate_receipt_cannot_claim_hardware_qualification(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return builder.REVISION + "\n"
        if "-c" in argv:
            return json.dumps({"tensorfold": "0.6.6"})
        return ""

    monkeypatch.setattr(builder, "run", fake_run)
    output = tmp_path / "new"
    receipt = builder.build(output, python="selected-python")
    data = json.loads(receipt.read_text())
    assert data["fixture_tests"] == "passed"
    assert data["release_ready"] is False
    assert set(data["qualification"].values()) == {"not_run"}
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert ["git", "checkout", "--detach", builder.REVISION] in commands
    assert any(argv[:4] == [str(output / "venv/bin/python"), "-m", "pip", "install"]
               and argv[-1] == f"{output / 'source'}[vision,test]" for argv in commands)
    assert not any("-e" in argv for argv in commands)
    assert not (output / "active.json").exists()

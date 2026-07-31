from __future__ import annotations

import sys

from mnemosyne_fleet import main as main_module

from .helpers import fleet_config


def test_main_forces_single_process_scheduler(
    tmp_path,
    monkeypatch,
) -> None:
    config = fleet_config(tmp_path)
    app = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(main_module, "load_config", lambda _path: config)
    monkeypatch.setattr(main_module, "create_app", lambda _config: app)
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda passed_app, **kwargs: captured.update(
            {"app": passed_app, **kwargs}
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["mnemosyne-fleet", "--config", str(tmp_path / "config.toml")],
    )

    main_module.main()

    assert captured["app"] is app
    assert captured["workers"] == 1

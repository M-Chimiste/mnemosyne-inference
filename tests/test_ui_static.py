from pathlib import Path


def _write_ui(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id='root'>mnemosyne-ui</div>")
    (root / "assets" / "app.js").write_text("console.log('mnemosyne')")


def test_admin_root_redirects_to_ui(client, monkeypatch, tmp_path):
    ui_root = tmp_path / "dist"
    _write_ui(ui_root)
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(ui_root))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_admin_ui_serves_index_assets_and_spa_fallback(client, monkeypatch, tmp_path):
    ui_root = tmp_path / "dist"
    _write_ui(ui_root)
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(ui_root))

    index = client.get("/ui/")
    spa = client.get("/ui/catalog")
    asset = client.get("/ui/assets/app.js")

    assert index.status_code == 200
    assert "mnemosyne-ui" in index.text
    assert spa.status_code == 200
    assert "mnemosyne-ui" in spa.text
    assert asset.status_code == 200
    assert "mnemosyne" in asset.text


def test_admin_ui_traversal_returns_404(client, monkeypatch, tmp_path):
    ui_root = tmp_path / "dist"
    _write_ui(ui_root)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(ui_root))

    response = client.get("/ui/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert "secret" not in response.text


def test_admin_ui_requires_basic_auth(admin_client_no_auth, monkeypatch, tmp_path):
    ui_root = tmp_path / "dist"
    _write_ui(ui_root)
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(ui_root))

    response = admin_client_no_auth.get("/ui/")

    assert response.status_code == 401


def test_inference_plane_has_no_ui_routes(inference_client, monkeypatch, tmp_path):
    ui_root = tmp_path / "dist"
    _write_ui(ui_root)
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(ui_root))

    response = inference_client.get("/ui/")

    assert response.status_code == 404


def test_missing_ui_build_returns_404(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MNEMOSYNE_UI_DIR", str(tmp_path / "missing"))

    response = client.get("/ui/")

    assert response.status_code == 404

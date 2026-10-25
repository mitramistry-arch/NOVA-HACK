from pathlib import Path

from scripts.check_frontend_assets import check_assets


def _write_bundle(root: Path, javascript: str = "export const ready = true;") -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (root / "assets" / "index.js").write_text(javascript, encoding="utf-8")
    return root


def test_frontend_asset_check_accepts_bundle_without_secrets(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist")
    assert check_assets(root, environment={}) == []


def test_frontend_asset_check_rejects_server_api_key_header(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist", 'fetch("/api", { headers: { "X-API-Key": "x" } });')
    assert any("server API-key header" in error for error in check_assets(root, environment={}))


def test_frontend_asset_check_rejects_configured_secret_value(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist", 'const accidental = "very-secret-production-key";')
    errors = check_assets(root, environment={"NOVA_API_KEY": "very-secret-production-key"})
    assert any("secret value appears" in error for error in errors)


def test_frontend_asset_check_rejects_source_maps(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist")
    (root / "assets" / "index.js.map").write_text("{}", encoding="utf-8")
    assert any("source maps" in error for error in check_assets(root, environment={}))


def test_frontend_asset_check_fails_closed_for_missing_output(tmp_path: Path) -> None:
    assert any("does not exist" in error for error in check_assets(tmp_path / "missing"))


def test_frontend_asset_check_rejects_mixed_case_api_key_header(tmp_path: Path) -> None:
    root = _write_bundle(
        tmp_path / "dist", 'fetch("/api", { headers: { "x-Api-Key": "secret" } });'
    )
    assert any("API-key header" in error for error in check_assets(root, environment={}))


def test_frontend_asset_check_rejects_even_short_configured_secret(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist", 'const accidental = "x";')
    errors = check_assets(root, environment={"NOVA_API_KEY": "x"})
    assert any("secret value appears" in error for error in errors)


def test_frontend_asset_check_rejects_browser_storage_persistence(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "dist", 'localStorage.setItem("draft", userText);')
    assert any("browser storage" in error for error in check_assets(root, environment={}))

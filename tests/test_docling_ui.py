from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docling_ui_image_is_pinned_and_offline_buildable():
    dockerfile = (ROOT / "services" / "docling-ui" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "@sha256:" in dockerfile
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile
    assert 'DOCLING_SERVE_ENABLE_UI="true"' in dockerfile
    assert 'GRADIO_ANALYTICS_ENABLED="false"' in dockerfile
    assert '"--workers", "1"' in dockerfile


def test_docling_root_redirects_to_native_workspace():
    app_source = (ROOT / "services" / "docling-ui" / "docling_ui_app.py").read_text(
        encoding="utf-8"
    )
    assert '@app.get("/", include_in_schema=False)' in app_source
    assert 'RedirectResponse(url="/ui/", status_code=307)' in app_source

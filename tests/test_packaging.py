"""v1.0.0 packaging invariants.

There is exactly one version source — ``council.__version__`` — and hatchling
derives the distribution metadata from it. These tests pin that the installed
distribution agrees and that the web API reports the same value.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from conftest import cfg
from fastapi.testclient import TestClient

import council
from council.web.server import create_app


def test_distribution_version_matches_package_attr() -> None:
    installed = importlib.metadata.version("ai-council")
    assert installed == council.__version__ == "1.3.0"


def test_api_meta_reports_the_single_version(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, config=cfg(participants=3))
    with TestClient(app) as client:
        meta = client.get("/api/meta").json()
        assert meta["version"] == council.__version__


def test_web_static_assets_travel_with_the_package(tmp_path: Path) -> None:
    """The wheel force-includes static assets; catch regressions from source."""
    root = Path(council.__file__).resolve().parent
    assert (root / "web" / "static" / "index.html").is_file()
    assert (root / "web" / "static" / "themes" / "high-contrast.json").is_file()
    assert (root / "web" / "static" / "i18n" / "zh.json").is_file()
    # create_app would fail to mount if the directory were missing.
    app = create_app(data_dir=tmp_path, config=cfg(participants=2))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/themes/dark.json").status_code == 200

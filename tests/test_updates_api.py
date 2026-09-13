"""Update endpoints: the toggle, the refresh, and the start-up check.

Offline like the rest of the suite — the vendor HTTP layer is mocked, and the
probes are aimed at the two local providers (Ollama / vLLM) precisely because
they need no API key. That keeps these tests deterministic on a machine that
happens to have a key in its environment.
"""

from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from council.core.config import Config, parse_config
from council.registry import discovery
from council.web.server import create_app

#: `ollama` and `vllm` declare `secret_required = false`, so they probe with no
#: credentials at all — the only two providers a test can rely on.
_LOCAL_ROUTES = {
    "localhost/v1/models": {"data": [{"id": "brand-new-local"}]},
}


def _handler(routes: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        for prefix, payload in routes.items():
            if key.startswith(prefix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "unrouted"})

    return httpx.MockTransport(handle)


def _config(**updates: Any) -> Config:
    return parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "n1", "adapter": "fake", "model": "fake/n1", "role": "participant"},
                {"id": "n2", "adapter": "fake", "model": "fake/n2", "role": "participant"},
                {"id": "judge", "adapter": "fake", "model": "fake/judge", "role": "judge"},
            ],
            "judge": {"enabled": True, "node_id": "judge", "auto_select": True},
            "updates": updates,
        }
    )


def _client(tmp_path: Path, config: Config, routes: dict[str, Any] | None = None) -> TestClient:
    app = create_app(
        data_dir=tmp_path,
        config=config,
        discovery_transport=_handler(routes if routes is not None else _LOCAL_ROUTES),
    )
    return TestClient(app)


def test_status_defaults_to_disabled(tmp_path: Path) -> None:
    """Opt-in by default, so the "talks to nothing but model endpoints" promise
    holds until the user says otherwise."""
    with _client(tmp_path, _config()) as client:
        payload = client.get("/api/updates").json()
    assert payload["enabled"] is False
    assert payload["config_enabled"] is False
    assert payload["report"] is None
    assert payload["last_check_at"] == ""
    assert str(tmp_path / "registry" / "discovered") in payload["overlay_dir"]


def test_refresh_is_refused_while_disabled(tmp_path: Path) -> None:
    with _client(tmp_path, _config()) as client:
        response = client.post("/api/updates/refresh")
    assert response.status_code == 409
    assert "未开启" in response.json()["detail"]


def test_toggle_enables_and_persists(tmp_path: Path) -> None:
    with _client(tmp_path, _config()) as client:
        payload = client.post("/api/updates/enabled", json={"enabled": True}).json()
        assert payload["enabled"] is True
        # The config file still says false; the panel's choice is the override.
        assert payload["config_enabled"] is False
        state = discovery.load_state(tmp_path)
        assert state["enabled"] is True
        assert client.get("/api/updates").json()["enabled"] is True

        # A fresh process must read the same choice back.
        assert discovery.load_state(tmp_path)["enabled"] is True


def test_config_default_is_honoured_without_a_panel_choice(tmp_path: Path) -> None:
    with _client(tmp_path, _config(enabled=True)) as client:
        assert client.get("/api/updates").json()["enabled"] is True


def test_toggle_off_overrides_an_enabled_config(tmp_path: Path) -> None:
    with _client(tmp_path, _config(enabled=True)) as client:
        client.post("/api/updates/enabled", json={"enabled": False})
        assert client.get("/api/updates").json()["enabled"] is False
        assert client.post("/api/updates/refresh").status_code == 409


def test_refresh_writes_overlay_and_reports(tmp_path: Path) -> None:
    with _client(tmp_path, _config(enabled=True)) as client:
        response = client.post("/api/updates/refresh")
        assert response.status_code == 200, response.text
        payload = response.json()

    assert payload["report"]["new_total"] >= 1
    assert payload["last_error"] == ""
    assert payload["last_check_at"]

    overlay = tmp_path / "registry" / "discovered" / "ollama.toml"
    assert overlay.is_file()
    parsed = tomllib.loads(overlay.read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in parsed["models"]]
    assert "brand-new-local" in ids
    assert all(entry["discovered"] for entry in parsed["models"])


def test_missing_secret_names_the_variable_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure report must name the variable, never leak a value."""
    monkeypatch.setattr(discovery.secrets, "resolve", lambda name, **_: None)
    with _client(tmp_path, _config(enabled=True)) as client:
        payload = client.post("/api/updates/refresh").json()
    entry = next(p for p in payload["report"]["providers"] if p["provider"] == "openai_api")
    assert entry["status"] == "no_secret"
    assert "OPENAI_API_KEY" in entry["detail"]


def test_nothing_ever_echoes_a_secret_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probing a vendor must not be able to write the key anywhere it lands.

    Canary value in the environment, a failing probe, then sweep the API
    response, the state file and every file discovery produced.
    """
    canary = "sk-LEAK-CANARY-0123456789abcdef"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    with _client(tmp_path, _config(enabled=True)) as client:
        payload = client.post("/api/updates/refresh").json()

    assert canary not in json.dumps(payload)
    assert canary not in (tmp_path / "updates.json").read_text(encoding="utf-8")
    for path in (tmp_path / "registry").rglob("*"):
        if path.is_file():
            assert canary not in path.read_text(encoding="utf-8"), path


def test_provider_failure_is_recorded_not_fatal(tmp_path: Path) -> None:
    """An unroutable vendor (404 here) becomes a row in the report."""
    with _client(tmp_path, _config(enabled=True), routes={}) as client:
        response = client.post("/api/updates/refresh")
        assert response.status_code == 200
        statuses = {p["status"] for p in response.json()["report"]["providers"]}
    assert "error" in statuses or "no_secret" in statuses


def test_models_catalog_marks_curated_entries(tmp_path: Path) -> None:
    with _client(tmp_path, _config()) as client:
        catalog = client.get("/api/models").json()
    assert catalog["providers"]
    for provider in catalog["providers"]:
        for model in provider["models"]:
            assert isinstance(model["discovered"], bool)
    # Nothing is discovered unless the user turned discovery on.
    assert all(
        model["discovered"] is False
        for provider in catalog["providers"]
        for model in provider["models"]
    )


def test_startup_check_runs_only_when_enabled(tmp_path: Path) -> None:
    disabled = tmp_path / "off"
    with _client(disabled, _config(enabled=False, min_interval_h=0)) as client:
        client.get("/api/updates")
    assert discovery.load_state(disabled).get("last_check_at") in (None, "")

    enabled = tmp_path / "on"
    with _client(enabled, _config(enabled=True, check_on_start=True, min_interval_h=0)) as client:
        deadline = time.monotonic() + 5.0
        seen = ""
        while time.monotonic() < deadline and not seen:
            seen = client.get("/api/updates").json()["last_check_at"]
            if not seen:
                time.sleep(0.05)
        assert seen, "启动检查没有在后台跑起来"


def test_discovered_models_reach_the_picker(tmp_path: Path) -> None:
    """The overlay has to be written *and* read from the same directory.

    Regression guard for `council serve --data-dir X`: the registry used to come
    from the process-wide default data directory regardless of X, so an overlay
    landed in X and was then read from somewhere else — the model appeared in
    the report but could never be selected.
    """
    from council.registry import registry_root

    default_root = registry_root()
    with _client(tmp_path, _config(enabled=True)) as client:
        assert registry_root() == tmp_path / "registry"
        client.post("/api/updates/refresh")
        catalog = client.get("/api/models").json()

    openai = next(p for p in catalog["providers"] if p["id"] == "ollama")
    discovered = [m for m in openai["models"] if m["discovered"]]
    assert [m["id"] for m in discovered] == ["brand-new-local"]

    # Leaving the app must not leave the root pinned for the next caller.
    assert registry_root() == default_root


def test_startup_check_skipped_within_min_interval(tmp_path: Path) -> None:
    """A long-running server must not re-probe every vendor on every start."""
    discovery.save_state(
        {
            "last_check_at": discovery.discover(
                data=tmp_path,
                options=discovery.DiscoveryOptions(catalog_url=""),
                transport=_handler(_LOCAL_ROUTES),
            ).checked_at
        },
        tmp_path,
    )
    with _client(tmp_path, _config(enabled=True, check_on_start=True, min_interval_h=24)) as client:
        time.sleep(0.3)  # give a wrongly-scheduled check time to show up
        payload = client.get("/api/updates").json()
    assert payload["report"] is None, "近期已检查过，不应再探测"


def test_catalog_only_offers_models_that_pass_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider without a resolvable key must not be offered at all.

    Selecting such a model cannot work: the session would fail on the first
    call. Key-less local endpoints stay, and every offered model has to pass
    the offline payload scan.
    """
    monkeypatch.setattr("council.adapters.scan.secrets.resolve", lambda name, **_: None)
    with _client(tmp_path, _config()) as client:
        catalog = client.get("/api/models").json()

    by_id = {p["id"]: p for p in catalog["providers"]}
    assert by_id["ollama"]["available"] is True, "免密端点恒可用"
    assert by_id["ollama"]["models"]
    assert all(m["usable"] for m in by_id["ollama"]["models"])

    assert by_id["openai_api"]["available"] is False
    assert by_id["openai_api"]["models"]
    assert all(not m["usable"] for m in by_id["openai_api"]["models"])


def test_scan_skips_cli_models_when_the_binary_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli_session 同理：本地 CLI 没装就不进下拉框。"""
    monkeypatch.setattr("council.adapters.scan.shutil.which", lambda _: None)
    from council.adapters.scan import cli_installed, scan_model
    from council.registry.loader import Registry

    registry = Registry.load(include_discovered=False)
    issues = scan_model(registry, "cli_session", "codex:")
    assert issues, "未安装时应报告不可用"
    assert cli_installed("codex") is False


def test_provider_that_failed_probe_is_hidden_from_picker(tmp_path: Path) -> None:
    """上次联网探测失败的厂商，模型不进下拉框。

    Ollama/vLLM 没启动时照样报「失败（连接被拒）」，但下拉框却还列出它们的模型
    ——选中只会让会议中途失败。修好后再点一次「立即检查」即可恢复。
    """
    with _client(tmp_path, _config(enabled=True), routes={}) as client:
        client.post("/api/updates/refresh")  # 全部厂商都探测失败
        catalog = client.get("/api/models").json()

    by_id = {p["id"]: p for p in catalog["providers"]}
    assert by_id["ollama"]["available"] is False
    assert all(not m["usable"] for m in by_id["ollama"]["models"])
    assert by_id["vllm"]["available"] is False

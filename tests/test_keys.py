"""Keys endpoints: status, save, delete — all offline, keyring faked.

The real keyring backend is environment-specific (and unavailable on headless
CI), so the tests monkeypatch ``council.core.secrets`` — the web layer is what
we are testing, not the OS vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import cfg
from fastapi.testclient import TestClient

from council.web.server import create_app

_VALUES: dict[str, str] = {}


def _fake_status(name: str, *, env_file: Path | None = None) -> str:
    return _VALUES.get(name, "unset")


def _fake_set(name: str, value: str) -> None:
    # Writing the keyring does not change which source *wins* — an env var or
    # .env value still shadows the vault, exactly like the real precedence.
    if _VALUES.get(name) not in ("env", "dotenv"):
        _VALUES[name] = "keyring"


def _fake_delete(name: str) -> None:
    _VALUES.pop(name, None)


@pytest.fixture()
def keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    _VALUES.clear()
    monkeypatch.setattr("council.core.secrets.set_secret", _fake_set)
    monkeypatch.setattr("council.core.secrets.delete_secret", _fake_delete)
    monkeypatch.setattr("council.core.secrets.secret_status", _fake_status)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(data_dir=tmp_path, config=cfg(participants=2)))


def test_overview_lists_presets_with_sources(tmp_path: Path, keyring: None) -> None:
    with _client(tmp_path) as client:
        data = client.get("/api/keys").json()
        names = [entry["name"] for entry in data["presets"]]
        assert names == ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"]
        assert data["statuses"] == {
            "OPENAI_API_KEY": "unset",
            "ANTHROPIC_API_KEY": "unset",
            "GOOGLE_API_KEY": "unset",
        }


def test_set_then_status_reports_keyring(tmp_path: Path, keyring: None) -> None:
    with _client(tmp_path) as client:
        saved = client.post(
            "/api/keys",
            json={"name": "OPENAI_API_KEY", "value": "sk-test-123"},
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["ok"] and body["source"] == "keyring" and not body["shadowed"]

        status = client.get("/api/keys/OPENAI_API_KEY").json()
        assert status == {"name": "OPENAI_API_KEY", "source": "keyring"}


def test_shadowed_when_env_or_dotenv_present(tmp_path: Path, keyring: None) -> None:
    _VALUES["ANTHROPIC_API_KEY"] = "env"
    with _client(tmp_path) as client:
        body = client.post("/api/keys", json={"name": "ANTHROPIC_API_KEY", "value": "new"}).json()
        assert body["shadowed"] is True  # env would win at resolve time


def test_delete_removes_keyring_entry(tmp_path: Path, keyring: None) -> None:
    _VALUES["OPENAI_API_KEY"] = "keyring"
    with _client(tmp_path) as client:
        deleted = client.delete("/api/keys/OPENAI_API_KEY")
        assert deleted.status_code == 200, deleted.text
        assert _VALUES.get("OPENAI_API_KEY") is None
        assert client.get("/api/keys/OPENAI_API_KEY").status_code == 404


def test_delete_of_unset_key_is_404(tmp_path: Path, keyring: None) -> None:
    with _client(tmp_path) as client:
        assert client.delete("/api/keys/GOOGLE_API_KEY").status_code == 404


def test_invalid_name_is_rejected(tmp_path: Path, keyring: None) -> None:
    with _client(tmp_path) as client:
        bad = client.post("/api/keys", json={"name": "1BAD NAME", "value": "x"})
        assert bad.status_code == 422
        empty = client.post("/api/keys", json={"name": "OPENAI_API_KEY", "value": ""})
        assert empty.status_code == 422


def test_arbitrary_custom_name_is_allowed(tmp_path: Path, keyring: None) -> None:
    with _client(tmp_path) as client:
        saved = client.post("/api/keys", json={"name": "MOONSHOT_API_KEY", "value": "sk-x"})
        assert saved.status_code == 200, saved.text
        assert _VALUES.get("MOONSHOT_API_KEY") == "keyring"

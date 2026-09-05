"""Secrets: names everywhere, values nowhere."""

from __future__ import annotations

from council.core.secrets import (
    SecretError,
    redact,
    require,
    resolve,
    secret_fingerprint,
)


def test_env_var_wins(monkeypatch) -> None:
    monkeypatch.setenv("COUNCIL_TEST_KEY", "from-env")
    assert resolve("COUNCIL_TEST_KEY") == "from-env"


def test_env_file_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COUNCIL_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nOTHER=ignored\nCOUNCIL_TEST_KEY=from-file\n", encoding="utf-8")
    assert resolve("COUNCIL_TEST_KEY", env_file=env_file) == "from-file"


def test_env_file_supports_quotes(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COUNCIL_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('COUNCIL_TEST_KEY="quoted value"\n', encoding="utf-8")
    assert resolve("COUNCIL_TEST_KEY", env_file=env_file) == "quoted value"


def test_missing_secret_raises_without_leaking(monkeypatch) -> None:
    monkeypatch.delenv("COUNCIL_DEFINITELY_MISSING", raising=False)
    import pytest

    with pytest.raises(SecretError) as info:
        require("COUNCIL_DEFINITELY_MISSING")
    assert "COUNCIL_DEFINITELY_MISSING" in str(info.value)


def test_fingerprint_never_exposes_value(monkeypatch) -> None:
    monkeypatch.setenv("COUNCIL_TEST_KEY", "super-secret-value")
    fingerprint = secret_fingerprint("COUNCIL_TEST_KEY")
    assert fingerprint is not None
    assert "super-secret-value" not in fingerprint
    assert len(fingerprint) == 12


def test_redact_masks_known_values() -> None:
    text = "Authorization: Bearer sk-abc123 said the proxy"
    assert "sk-abc123" not in redact(text, "sk-abc123")
    assert "***" in redact(text, "sk-abc123")


def test_redact_longest_first() -> None:
    text = "abcdef"
    assert redact(text, "abc", "abcdef") == "***"

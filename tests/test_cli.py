"""CLI smoke tests: the exact path a first-time user takes.

`council run` with no config file must work offline out of the box — that is
the ten-minute acceptance test for a fresh clone.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from council.cli.main import app
from council.core.paths import sessions_db

runner = CliRunner()


def test_run_smoke_with_default_fake_adapters(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "如何把日活从 1 万做到 10 万？", "--data-dir", str(tmp_path)],
        env={"CI": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "会议完成" in result.output
    assert "P0" in result.output or "P1" in result.output
    assert (tmp_path / "sessions.sqlite3").is_file()


def test_run_records_the_session_and_resume_is_a_noop(tmp_path: Path) -> None:
    first = runner.invoke(app, ["run", "问题", "--data-dir", str(tmp_path), "--session", "smoke-1"])
    assert first.exit_code == 0, first.output

    listed = runner.invoke(app, ["sessions", "--data-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "smoke-1" in listed.output
    assert "completed" in listed.output

    resumed = runner.invoke(app, ["resume", "smoke-1", "--data-dir", str(tmp_path)])
    assert resumed.exit_code == 0, resumed.output
    assert "会议完成" in resumed.output


def test_run_honours_an_explicit_config_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
schema_version = 1
[council]
active_nodes = 2
[[nodes]]
id = "a"
adapter = "fake"
model = "fake/a"
[[nodes]]
id = "b"
adapter = "fake"
model = "fake/b"
[[nodes]]
id = "judge"
adapter = "fake"
model = "fake/judge"
role = "judge"
[judge]
node_id = "judge"
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["run", "问题", "--config", str(config), "--data-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "参与者（2 名）" in result.output


def test_config_check_reports_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config-check", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 2
    assert "不存在" in result.output


def test_config_check_accepts_a_valid_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
schema_version = 1
[council]
active_nodes = 2
[[nodes]]
id = "a"
adapter = "fake"
model = "fake/a"
[[nodes]]
id = "b"
adapter = "fake"
model = "fake/b"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config-check", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "配置合法" in result.output


def test_sessions_command_on_empty_store(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sessions", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "还没有任何会话" in result.output
    assert sessions_db(tmp_path).is_file()

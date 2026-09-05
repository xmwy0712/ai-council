"""Configuration: schema validation, semantic rules, fingerprinting."""

from __future__ import annotations

import pytest

from council.core.config import (
    Config,
    ConfigError,
    default_config,
    diff_configs,
    fingerprint,
    load_config,
    parse_config,
)


def test_default_config_is_valid() -> None:
    config = default_config()
    assert len(config.participants) == 3
    assert config.judge_node is not None
    assert config.warnings() == []


def test_judge_must_not_be_a_participant() -> None:
    with pytest.raises(ConfigError, match=r"judge\.node_id"):
        parse_config(
            {
                "council": {"active_nodes": 2},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m", "role": "participant"},
                    {"id": "n2", "adapter": "fake", "model": "m", "role": "participant"},
                    {"id": "judge", "adapter": "fake", "model": "m", "role": "judge"},
                ],
                # 把参与者 n1 指认为 Judge：独立性被破坏，必须报错
                "judge": {"node_id": "n1"},
            }
        )


def test_duplicate_node_ids_rejected() -> None:
    with pytest.raises(ConfigError, match="节点 id 重复"):
        parse_config(
            {
                "council": {"active_nodes": 2},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m"},
                    {"id": "n1", "adapter": "fake", "model": "m"},
                ],
            }
        )


def test_active_nodes_must_match_enabled_participants() -> None:
    with pytest.raises(ConfigError, match="active_nodes"):
        parse_config(
            {
                "council": {"active_nodes": 3},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m"},
                    {"id": "n2", "adapter": "fake", "model": "m"},
                ],
            }
        )


def test_min_quorum_above_participants_rejected() -> None:
    with pytest.raises(ConfigError, match="min_quorum"):
        parse_config(
            {
                "council": {"active_nodes": 2},
                "failure": {"min_quorum": 3},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m"},
                    {"id": "n2", "adapter": "fake", "model": "m"},
                ],
            }
        )


def test_unknown_key_rejected_with_readable_problem() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_config({"council": {"active_nodez": 3}})
    assert any("active_nodez" in p for p in excinfo.value.problems)


def test_bad_hex_color_rejected() -> None:
    with pytest.raises(ConfigError, match="color"):
        parse_config(
            {
                "council": {"active_nodes": 2},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m", "color": "blue"},
                    {"id": "n2", "adapter": "fake", "model": "m"},
                ],
            }
        )


def test_future_schema_version_rejected() -> None:
    with pytest.raises(ConfigError, match="高于本程序支持"):
        parse_config(
            {
                "schema_version": 99,
                "council": {"active_nodes": 2},
                "nodes": [
                    {"id": "n1", "adapter": "fake", "model": "m"},
                    {"id": "n2", "adapter": "fake", "model": "m"},
                ],
            }
        )


def test_warns_when_judge_shares_model_with_participant() -> None:
    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "n1", "adapter": "fake", "model": "shared"},
                {"id": "n2", "adapter": "fake", "model": "other"},
                {"id": "judge", "adapter": "fake", "model": "shared", "role": "judge"},
            ],
            "judge": {"node_id": "judge"},
        }
    )
    assert any("独立性" in w for w in config.warnings())


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = default_config()
    b = default_config()
    assert fingerprint(a) == fingerprint(b)
    b.council.max_rounds = 4
    assert fingerprint(a) != fingerprint(b)


def test_diff_configs_reports_paths() -> None:
    a = default_config()
    b = default_config()
    b.council.max_rounds = 4
    assert "council.max_rounds: 8 → 4" in diff_configs(a, b)


def test_load_config_roundtrip(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
schema_version = 1
[council]
active_nodes = 2
[timeout]
idle_s = 45
[[nodes]]
id = "a"
adapter = "fake"
model = "m"
[[nodes]]
id = "b"
adapter = "fake"
model = "m"
""",
        encoding="utf-8",
    )
    config: Config = load_config(path)
    assert config.timeout.idle_s == 45
    assert [n.id for n in config.participants] == ["a", "b"]


def test_load_config_missing_file(tmp_path) -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.toml")


def test_load_config_bad_toml(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("council = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法 TOML"):
        load_config(path)


def test_shipped_example_config_is_valid() -> None:
    """The example config is documentation; it must never drift out of validity."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    config = load_config(example)
    assert len(config.participants) == 3
    assert config.judge_node is not None
    assert config.judge is not None
    assert config.judge.node_id not in {n.id for n in config.participants}
    assert config.warnings() == []

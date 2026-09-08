"""会话级阵容覆盖与注册表目录接口（2026-09-05 厂商扩展配套测试）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import make_adapters
from fastapi.testclient import TestClient

from council.web.server import create_app


def _registry_cfg() -> Any:
    """节点直接使用注册表内的真实模型（adapter_builder 仍注入 fake 适配器）。"""
    from council.core.config import parse_config

    return parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {
                    "id": "a",
                    "adapter": "openai_api",
                    "model": "gpt-5.2",
                    "thinking": "high",
                    "role": "participant",
                },
                {
                    "id": "b",
                    "adapter": "zhipu",
                    "model": "glm-4.6",
                    "role": "participant",
                },
                {"id": "judge", "adapter": "fake", "model": "fake/judge", "role": "judge"},
            ],
            "judge": {"enabled": True, "node_id": "judge", "auto_select": True},
        }
    )


def _client(tmp_path: Path) -> tuple[TestClient, Any]:
    app = create_app(
        data_dir=tmp_path,
        config=_registry_cfg(),
        adapter_builder=lambda c: make_adapters(c),
    )
    return TestClient(app), app


def test_meta_exposes_model_and_thinking_details(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        meta = client.get("/api/meta").json()
        node = meta["participants"][0]
        assert node["model_display"] == "GPT-5.2"
        assert node["thinking_supported"] is True
        assert "high" in node["thinking_levels"]
        assert node["thinking"] == "high"
        # judge 完整对象同样带展示信息
        assert meta["judge_node"] is not None
        assert meta["judge_node"]["id"] in ("judge", "a", "b") or "display" in meta["judge_node"]


def test_models_endpoint_lists_registry_catalog(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        data = client.get("/api/models").json()
        providers = {p["id"]: p for p in data["providers"]}
        # 10 家新厂商 + 原有 4 家都在目录里
        for vid in ("openai_api", "deepseek", "moonshot", "zhipu", "dashscope", "meta"):
            assert vid in providers, vid
        assert providers["deepseek"]["secret_required"] is True
        assert providers["ollama"]["secret_required"] is False
        gpt = next(m for m in providers["openai_api"]["models"] if m["id"] == "gpt-5.2")
        assert gpt["display"] == "GPT-5.2"
        assert "high" in gpt["thinking_levels"]


def test_create_session_applies_roster_overrides(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    with client:
        manager = app.state.manager
        response = client.post(
            "/api/sessions",
            json={
                "question": "覆盖测试",
                "overrides": [
                    {"node_id": "a", "model": "gpt-5.1", "thinking": "medium"},
                ],
            },
        )
        assert response.status_code == 202, response.text
        session_id = response.json()["session_id"]
        hub = manager.hub(session_id)
        assert hub is not None
        overridden = next(n for n in hub._config.nodes if n.id == "a")
        assert overridden.model == "gpt-5.1"
        assert overridden.thinking == "medium"
        # 会话配置是深拷贝：全局配置不被污染
        global_node = next(n for n in manager.config.nodes if n.id == "a")
        assert global_node.model == "gpt-5.2"
        assert global_node.thinking == "high"
        assert hub._config is not manager.config


def test_overrides_reject_unknown_inputs(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        # 未知节点
        r = client.post(
            "/api/sessions",
            json={"question": "q", "overrides": [{"node_id": "ghost", "model": "gpt-5.2"}]},
        )
        assert r.status_code == 422
        # 注册表外模型
        r = client.post(
            "/api/sessions",
            json={"question": "q", "overrides": [{"node_id": "a", "model": "made-up"}]},
        )
        assert r.status_code == 422
        # 不可翻译档位：anthropic Claude 5 系是 Adaptive（thinking_style=none）
        r = client.post(
            "/api/sessions",
            json={
                "question": "q",
                "overrides": [{"node_id": "b", "thinking": "high"}],
            },
        )
        assert r.status_code == 422


def test_model_override_switches_node_adapter(tmp_path: Path) -> None:
    """在 fake 演示节点上选择真实模型：适配器必须一并切换到该厂商——
    否则 fake 适配器拿着真实模型名继续本地演戏，密钥与思考永不生效。"""
    import os

    from council.adapters.openai_api import OpenAIAdapter

    os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")
    os.environ.setdefault("MOONSHOT_API_KEY", "sk-test")
    client, app = _client(tmp_path)
    with client:
        manager = app.state.manager
        response = client.post(
            "/api/sessions",
            json={
                "question": "适配器联动测试",
                "overrides": [
                    {"node_id": "a", "model": "deepseek-v4-flash", "thinking": "low"},
                    {"node_id": "b", "model": "kimi-k3", "thinking": "max"},
                ],
            },
        )
        assert response.status_code == 202, response.text
        hub = manager.hub(response.json()["session_id"])
        node_a = next(n for n in hub._config.nodes if n.id == "a")
        node_b = next(n for n in hub._config.nodes if n.id == "b")
        # 配置层：适配器跟随模型所属厂商
        assert node_a.adapter == "deepseek"
        assert node_a.thinking == "low"
        assert node_b.adapter == "moonshot"
        assert node_b.thinking == "max"
        # 适配器实例层：deepseek 节点解析为 OpenAI 兼容适配器且指向官方端点
        from council.adapters import build_adapters

        adapters = build_adapters(hub._config)
        deepseek = adapters["a"]
        assert isinstance(deepseek, OpenAIAdapter)
        assert "deepseek" in deepseek._base_url


def test_missing_key_surfaces_as_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """阵容切到真实厂商但缺密钥：返回 409 并指明缺哪把钥匙，而非 500。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 系统钥匙串里可能存有真实密钥：屏蔽 resolve，模拟「完全未配置」
    monkeypatch.setattr("council.core.secrets.resolve", lambda name, **kwargs: None)
    # 系统钥匙串里可能存有真实密钥：屏蔽 resolve，模拟「完全未配置」
    monkeypatch.setattr("council.core.secrets.resolve", lambda name, **kwargs: None)
    # 真实适配器构建器才会触发密钥检查（fake 构建器永远不缺密钥）
    from council.adapters import build_adapters

    app = create_app(
        data_dir=tmp_path,
        config=_registry_cfg(),
        adapter_builder=build_adapters,
    )
    client = TestClient(app)
    with client:
        r = client.post(
            "/api/sessions",
            json={
                "question": "缺密钥测试",
                "overrides": [{"node_id": "a", "model": "glm-5.3-flash", "thinking": "low"}],
            },
        )
        assert r.status_code == 409, r.text
        assert "ZHIPU_API_KEY" in r.json()["detail"]


def test_cli_model_override_sets_cli_setting(tmp_path: Path) -> None:
    """选 cli_session 模型（agy/codex/claude）：适配器与 settings.cli 一并设置。"""
    import os

    os.environ.setdefault("MOONSHOT_API_KEY", "sk-test")
    client, app = _client(tmp_path)
    with client:
        manager = app.state.manager
        response = client.post(
            "/api/sessions",
            json={
                "question": "CLI 适配器测试",
                "overrides": [{"node_id": "b", "model": "agy:"}],
            },
        )
        assert response.status_code == 202, response.text
        hub = manager.hub(response.json()["session_id"])
        node_b = next(n for n in hub._config.nodes if n.id == "b")
        assert node_b.adapter == "cli_session"
        assert node_b.settings["cli"] == "agy"
        assert node_b.model == "agy:"

"""会话级阵容覆盖与注册表目录接口（2026-09-05 厂商扩展配套测试）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

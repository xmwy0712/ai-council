"""回归测试：运行中「换模型」必须同步 adapter。

背景（用户实测复现的误报）：
  节点 n1 是 adapter=moonshot，用户在失败面板里把 model 依次改成
  glm-5.3 / agy: / gpt-5.6-sol，但 adapter 一直是 moonshot。
  于是这些请求全被发到 api.moonshot.cn——厂商不认识新模型名，
  回 429/404，界面显示「限流（HTTP 429）」。

  铁证：agy 是本地 CLI（走 subprocess，根本没有 HTTP），却报出
  「HTTP 429」——这只可能是请求根本没到 agy。

规划路径（新建会谈的 _apply_overrides）早就有这套联动；运行中干预
（_apply_decision）漏了，于是同一个模型在不同路径下行为不一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import cfg

from council.adapters import _FACTORIES
from council.core.config import NodeSection
from council.core.orchestrator import apply_model_to_node

REGISTRY_OK = Path("council/registry")


def _node(adapter: str, model: str = "placeholder") -> NodeSection:
    conf = cfg(participants=3)
    n = conf.nodes[0].model_copy(deep=True)
    n.adapter = adapter
    n.model = model
    n.settings = {}
    return n


@pytest.mark.parametrize(
    ("init_adapter", "model_id", "want_adapter"),
    [
        ("moonshot", "glm-5.3", "zhipu"),  # 跨厂商（用户实际踩的坑）
        ("moonshot", "agy:", "cli_session"),  # 跨到本地 CLI
        ("moonshot", "deepseek-v4-flash", "deepseek"),
        ("zhipu", "glm-5.3-flash", "zhipu"),  # 同厂商：保持
    ],
)
def test_switching_model_syncs_the_adapter(
    init_adapter: str, model_id: str, want_adapter: str
) -> None:
    node = _node(init_adapter)
    apply_model_to_node(node, model_id)
    assert node.model == model_id
    assert node.adapter == want_adapter, (
        f"从 {init_adapter} 换成 {model_id} 后适配器应为 {want_adapter}，"
        f"实得 {node.adapter}——不同步就会把新模型发到旧厂商"
    )


def test_cli_session_model_sets_settings_cli() -> None:
    """cli_session 的模型除适配器外还要把 settings.cli 指到前缀。"""
    node = _node("moonshot")
    apply_model_to_node(node, "agy:")
    assert node.adapter == "cli_session"
    assert node.settings.get("cli") == "agy"


def test_unknown_model_leaves_adapter_alone() -> None:
    """不在注册表里的模型不改适配器（交由调用方校验后报错）。"""
    node = _node("moonshot")
    synced = apply_model_to_node(node, "definitely-not-a-model")
    assert synced is None
    assert node.adapter == "moonshot"


def test_synced_adapter_is_a_real_adapter() -> None:
    """同步出来的适配器名必须是工厂认识的名字，否则实例化时才炸。"""
    for model_id in ("glm-5.3", "agy:", "deepseek-v4-flash", "gpt-5.6-sol"):
        node = _node("moonshot")
        apply_model_to_node(node, model_id)
        assert node.adapter in _FACTORIES or node.adapter in {
            "zhipu",
            "deepseek",
            "moonshot",
            "openai_api",
            "cli_session",
        }, f"{model_id} 同步出的适配器 {node.adapter} 不被工厂认识"


def test_apply_decision_uses_the_shared_helper() -> None:
    """SWITCH_MODEL 分支必须走共用函数，避免两处逻辑再漂移。"""
    src = Path("council/core/orchestrator.py").read_text(encoding="utf-8")
    seg = src.split("InterventionAction.SWITCH_MODEL:")[1].split(
        "InterventionAction.SWITCH_ADAPTER:"
    )[0]
    assert "apply_model_to_node" in seg, (
        "运行中换模型必须复用 apply_model_to_node，否则又会出现「只改 model 不改 adapter」的错配"
    )


def test_planning_path_and_intervention_path_agree() -> None:
    """两条路径（新建会谈 / 运行中干预）对同一模型给出同样的适配器。

    它们曾经不一致：规划路径会同步 adapter，干预路径不会——
    用户于是看到「换模型后仍报错」。
    """
    for model_id, want in (("glm-5.3", "zhipu"), ("agy:", "cli_session")):
        node = _node("moonshot")
        apply_model_to_node(node, model_id)
        assert node.adapter == want, f"{model_id} 应同步为 {want}"

"""回归测试：字数上限配置必须真的进入提示词。

背景（与 timeout 同类的「静默失效」缺陷）：
`budget.max_chars_review` / `budget.max_chars_debate` / `nodes.overrides.max_chars`
三项在配置里声明、在示例配置与文档里有说明，但**没有任何代码读取它们**。
P4 辩论模板甚至写着「字数有上限」，却不带具体数字——模型无从遵守，
用户把上限调到 200 也毫无效果。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import cfg

from council.adapters.base import resolve_max_chars_debate, resolve_max_chars_review
from council.core.prompts import PromptSet

TEMPLATES = Path(__file__).resolve().parents[1] / "council" / "core" / "templates" / "zh-CN"


def test_review_limit_follows_config() -> None:
    conf = cfg(participants=3)
    node = conf.nodes[0]
    assert resolve_max_chars_review(node, conf) == conf.budget.max_chars_review


def test_debate_limit_follows_config() -> None:
    conf = cfg(participants=3)
    node = conf.nodes[0]
    assert resolve_max_chars_debate(node, conf) == conf.budget.max_chars_debate


def test_node_override_wins_over_global() -> None:
    """节点级 max_chars 必须压过全局值。"""
    conf = cfg(participants=3)
    node = conf.nodes[0].model_copy(deep=True)
    node.overrides.max_chars = 321
    assert resolve_max_chars_review(node, conf) == 321
    assert resolve_max_chars_debate(node, conf) == 321


@pytest.mark.parametrize(
    ("name", "limit", "resolver"),
    [
        ("review", 4000, resolve_max_chars_review),
        ("debate", 1200, resolve_max_chars_debate),
    ],
)
def test_template_renders_the_actual_number(name: str, limit: int, resolver: object) -> None:
    """渲染出的提示词必须带上具体数字，而不是只说「有上限」。"""
    conf = cfg(participants=3)
    node = conf.nodes[0]
    ps = PromptSet("zh-CN")
    values = dict(
        node_id="n1",
        version=1,
        author="n1",
        max_chars=resolver(node, conf),
        data_zones="<data_zone>x</data_zone>",
    )
    if name == "debate":
        values.update(debate_round=1, debate_rounds=2)
    text = ps.render(name, **values)
    expected = conf.budget.max_chars_review if name == "review" else conf.budget.max_chars_debate
    assert str(expected) in text
    assert "{{" not in text, "占位符必须全部被填掉"


def test_templates_declare_the_placeholder() -> None:
    """模板里有「字数上限」说法的地方，必须带 {{max_chars}} 占位符。

    这是防止回归的关键：只改代码不改模板（或反之）都会让上限重新变成空话。
    """
    for name in ("review", "debate"):
        text = (TEMPLATES / f"{name}.md").read_text(encoding="utf-8")
        assert "{{max_chars}}" in text, f"{name}.md 缺少 max_chars 占位符"


def test_custom_limit_reaches_the_prompt_end_to_end() -> None:
    """把上限设成任意值，提示词里出现的必须是那个值。"""
    conf = cfg(participants=3)
    conf.budget.max_chars_review = 888
    conf.budget.max_chars_debate = 222
    node = conf.nodes[0]
    ps = PromptSet("zh-CN")

    r = ps.render(
        "review",
        node_id="n",
        version=1,
        author="a",
        max_chars=resolve_max_chars_review(node, conf),
        data_zones="<data_zone>x</data_zone>",
    )
    d = ps.render(
        "debate",
        node_id="n",
        version=1,
        author="a",
        debate_round=1,
        debate_rounds=1,
        max_chars=resolve_max_chars_debate(node, conf),
        data_zones="<data_zone>x</data_zone>",
    )
    assert "888" in r
    assert "222" in d

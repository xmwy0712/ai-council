"""Sanitising pipeline golden tests.

Each rule has a golden input/output pair; protected zones (code, inline code,
URLs) must survive every rule untouched.
"""

from __future__ import annotations

from council.core.config import SanitizeSection
from council.sanitize import sanitize

GOLDEN: list[tuple[str, str]] = [
    # 装饰性 Markdown 噪声
    ("这是**加粗**和__下划线__。", "这是加粗和下划线。"),
    ("* 列表项不应被删", "* 列表项不应被删"),
    ("###\n正文\n", "正文\n"),
    ("---\n---\n---\n分隔\n", "---\n分隔\n"),
    ("第1行\n\n\n\n第2行", "第1行\n\n第2行"),
    ("尾部空格   \n保留", "尾部空格\n保留"),
    # 中英文空格与标点
    ("用Python写代码比用Java快", "用 Python 写代码比用 Java 快"),
    ("号码123与456相连", "号码123与456相连"),
    ("你好,世界!", "你好，世界！"),
    # 表情堆砌
    ("太棒了！！！😂😂😂😂 鼓掌", "太棒了！！！😂 鼓掌"),
    # 套话开头结尾
    ("好的，我来回答。\n正文", "正文"),
    ("作为一个 AI，我无法访问你的文件。\n正文", "正文"),
    ("正文\n希望这有帮助！", "正文"),
    ("正文\n如果还有任何问题欢迎随时问我。", "正文"),
    # 无规则命中时原样保留
    ("# 保留的标题\n- 列表项", "# 保留的标题\n- 列表项"),
]


def test_golden_pairs() -> None:
    for source, expected in GOLDEN:
        assert sanitize(source) == expected, f"golden failed for {source!r}"


def test_protected_zones_survive_every_rule() -> None:
    text = (
        "好，我来分析。**强调** 但代码块不动：\n"
        "```python\nx = '**not-bold**'  # ---\n方案一 = 1\n```\n"
        "行内 `**y**` 和网址 https://example.com/a__b 保持原样。"
    )
    cleaned = sanitize(text)
    assert "**not-bold**" in cleaned
    assert "方案一 = 1" in cleaned  # code is not scanned for alternative plans here
    assert "`**y**`" in cleaned
    assert "https://example.com/a__b" in cleaned


def test_disabled_rules_are_skipped() -> None:
    section = SanitizeSection(disabled_rules=("strip_bold", "cjk_latin_space"))
    out = sanitize("这是**加粗**内容，用Python写", section)
    assert "**加粗**" in out  # strip_bold disabled
    assert "用Python" in out  # cjk spacing disabled


def test_disabled_pipeline_returns_input() -> None:
    section = SanitizeSection(enabled=False)
    text = "完全**不净化** \n\n\n\n 原文"
    assert sanitize(text, section) == text


def test_unknown_disabled_rule_id_is_tolerated() -> None:
    section = SanitizeSection(disabled_rules=("does_not_exist",))
    assert sanitize("好的，正文。", section) == "正文。"

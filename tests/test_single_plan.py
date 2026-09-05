"""Single-plan enforcement: one plan or the output is rejected."""

from __future__ import annotations

from council.core.single_plan import find_multi_plan_violations, mask_protected

ONE = "我们采用单一数据管道方案，把所有特征合并后统一建模。"
TWO_TITLED = "方案一：独立缓存。\n方案二：共享缓存。\n"
TITLED_ONE = "方案一：我们只用共享缓存，理由见下。\n"
OPTION_AB = "选项 A 是把迁移放白天；选项 B 是夜间。\n"
PLANB = "主方案是灰度发布，Plan B 是直接全量回滚。\n"
OR = "先做特征工程，或者可以直接上端到端模型。\n"
BRANCH = "两条路：一种思路是买现成 SaaS，另一种思路是自研。\n"


def test_single_plan_passes() -> None:
    assert find_multi_plan_violations(ONE) == []
    assert find_multi_plan_violations(TITLED_ONE) == []


def test_two_titled_plans_violate() -> None:
    violations = find_multi_plan_violations(TWO_TITLED)
    assert violations and "并列方案标题" in violations[0]


def test_option_branches_violate() -> None:
    assert find_multi_plan_violations(OPTION_AB)
    assert find_multi_plan_violations(PLANB)
    assert find_multi_plan_violations(OR)
    assert find_multi_plan_violations(BRANCH)


def test_code_blocks_are_protected() -> None:
    text = "建议的代码写法：\n```\n选项 A = fetch('plan-a'); 备选 = 'Plan B'\n```\n其余正文单一。"
    violations = find_multi_plan_violations(text)
    assert violations == []  # quoted code must never trigger a rejection


def test_inline_code_and_urls_are_protected() -> None:
    text = "接口返回 `方案一` 或 `方案二` 字段；详见 https://x.example/plan-b 文档。"
    assert find_multi_plan_violations(text) == []


def test_mask_protected_roundtrips() -> None:
    text = "正文 `code` 和 ```fence\ncode\n``` 都要原样回来。"
    masked, tokens = mask_protected(text)
    assert "`code`" not in masked and "```" not in masked
    restored = masked
    for token, original in tokens.items():
        restored = restored.replace(token, original)
    assert restored == text


def test_empty_proposal_is_violation_free() -> None:
    assert find_multi_plan_violations("") == []

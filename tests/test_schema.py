"""Structured-output contracts.

The critical property under test: a phase transition can only ever be driven by
a validated enum field. There is no code path that reads prose to guess state.
"""

from __future__ import annotations

import json

import pytest

from council.core.errors import ContractError
from council.core.schema import (
    ProposalOut,
    ReviewOut,
    VerdictOut,
    contract_block,
    extract_json,
    parse_structured,
)

PROPOSAL = {
    "proposal": "唯一一套方案",
    "assumptions": ["假设 A"],
    "risks": ["风险 A"],
    "steps": ["第一步", "第二步"],
    "open_questions": ["待确认 A"],
}


def test_parse_plain_json() -> None:
    out = parse_structured(json.dumps(PROPOSAL), ProposalOut)
    assert out.proposal == "唯一一套方案"
    assert len(out.steps) == 2


def test_parse_fenced_json() -> None:
    text = "这是我的方案：\n```json\n" + json.dumps(PROPOSAL) + "\n```\n"
    out = parse_structured(text, ProposalOut)
    assert out.proposal == "唯一一套方案"


def test_parse_json_embedded_in_prose() -> None:
    text = "好的，我来看一下。\n" + json.dumps(PROPOSAL) + "\n希望有帮助。"
    assert parse_structured(text, ProposalOut).proposal == "唯一一套方案"


def test_extract_json_never_invents_content() -> None:
    assert extract_json("没有任何 JSON") == "没有任何 JSON"


def test_invalid_json_raises_contract_error() -> None:
    with pytest.raises(ContractError, match="不是合法 JSON"):
        parse_structured("{not json", ProposalOut)


def test_missing_required_field_raises_contract_error() -> None:
    with pytest.raises(ContractError, match="不符合契约"):
        parse_structured(json.dumps({"assumptions": []}), ProposalOut)


def test_wrong_type_raises_contract_error() -> None:
    with pytest.raises(ContractError, match="不符合契约"):
        parse_structured(json.dumps({"proposal": 42}), ProposalOut)


def test_non_object_json_raises_contract_error() -> None:
    with pytest.raises(ContractError, match="必须是 JSON 对象"):
        parse_structured("[1, 2, 3]", ProposalOut)


def test_contract_error_is_never_retryable() -> None:
    with pytest.raises(ContractError) as info:
        parse_structured("{}", ProposalOut)
    assert info.value.retryable is False
    assert info.value.kind.value == "contract"


def test_verdict_status_is_constrained() -> None:
    ok = parse_structured(json.dumps({"status": "PASS", "rationale": "可以了"}), VerdictOut)
    assert ok.status == "PASS"
    with pytest.raises(ContractError, match="不符合契约"):
        parse_structured(json.dumps({"status": "MAYBE", "rationale": "看情况"}), VerdictOut)


def test_user_decision_requires_a_question() -> None:
    with pytest.raises(ContractError, match="question_for_user"):
        parse_structured(
            json.dumps({"status": "NEED_USER_DECISION", "rationale": "需要你定"}), VerdictOut
        )


def test_review_splits_severities() -> None:
    review = parse_structured(
        json.dumps(
            {
                "issues": [
                    {"id": "i1", "severity": "must", "text": "缺验收"},
                    {"id": "i2", "severity": "should", "text": "可再精简"},
                ]
            }
        ),
        ReviewOut,
    )
    assert [i.id for i in review.must_fix] == ["i1"]
    assert [i.id for i in review.should_fix] == ["i2"]


def test_contract_block_is_valid_json() -> None:
    block = contract_block(ProposalOut)
    assert json.loads(block)["properties"]["proposal"]

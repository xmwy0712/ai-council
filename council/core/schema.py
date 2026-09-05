"""Structured output contracts.

Every phase has exactly one output model. The engine never reads prose to
decide what happens next: phase transitions are driven by enum fields that live
here.

A note on :func:`extract_json`: we only ever peel a *container* (```json
fence, or the outermost ``{...}``). We never regex the natural-language body for
status words — that is precisely the failure that made the reference prototype
loop forever.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ContractError

__all__ = [
    "DebateOut",
    "Issue",
    "JudgeSelectionOut",
    "ProposalOut",
    "RejectedOut",
    "ReviewOut",
    "RevisionOut",
    "ScoreRow",
    "Severity",
    "VerdictOut",
    "contract_block",
    "parse_structured",
]

T = TypeVar("T", bound=BaseModel)

_FENCE: Final = re.compile(r"```(?:json|JSON)?\s*(?P<body>.*?)```", re.DOTALL)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(StrEnum):
    MUST = "must"  # blocks the proposal
    SHOULD = "should"  # improves it, does not block


class Issue(_Base):
    id: str = Field(min_length=1, max_length=32)
    severity: Severity
    text: str = Field(min_length=1)
    rationale: str = ""


class ProposalOut(_Base):
    """Exactly one plan. No alternatives, no branches, no "or we could"."""

    proposal: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class RevisionOut(ProposalOut):
    change_summary: str = ""


class ScoreRow(_Base):
    node_id: str
    correctness: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    feasibility: int = Field(ge=0, le=10)
    risk: int = Field(ge=0, le=10)
    fit: int = Field(ge=0, le=10)
    weighted: float = Field(ge=0, le=10)


class RejectedOut(_Base):
    node_id: str
    fatal_flaw: str = Field(min_length=1)


class JudgeSelectionOut(_Base):
    selected_node_id: str
    scores: list[ScoreRow] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    rejected: list[RejectedOut] = Field(default_factory=list)


class ReviewOut(_Base):
    """Reviewers may only point at problems — never rewrite the whole plan."""

    issues: list[Issue] = Field(default_factory=list)
    notes: str = ""

    @property
    def must_fix(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.MUST]

    @property
    def should_fix(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.SHOULD]


class DebateOut(_Base):
    conceded: list[str] = Field(default_factory=list)
    rebuttals: list[str] = Field(default_factory=list)
    still_disputed: list[Issue] = Field(default_factory=list)


class VerdictOut(_Base):
    status: str = Field(pattern=r"^(PASS|NEED_REVISION|NEED_USER_DECISION)$")
    must_fix: list[Issue] = Field(default_factory=list)
    should_fix: list[Issue] = Field(default_factory=list)
    disputes: list[Issue] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    #: Mandatory when status is NEED_USER_DECISION — a blocked council must say
    #: what only a human can settle.
    question_for_user: str = ""

    @model_validator(mode="after")
    def _require_question(self) -> VerdictOut:
        if self.status == "NEED_USER_DECISION" and not self.question_for_user.strip():
            raise ValueError("status=NEED_USER_DECISION 时必须填写 question_for_user")
        for issue in (*self.must_fix, *self.should_fix, *self.disputes):
            if issue.severity is Severity.SHOULD and issue in self.must_fix:
                raise ValueError("must_fix 中的 issue 必须使用 severity=must")
        return self


# ------------------------------------------------------------------- parsing


def extract_json(text: str) -> str:
    """Peel the JSON *container* off a reply. Never inspects the prose."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group("body").strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return stripped


def parse_structured(text: str, model: type[T], *, node_id: str | None = None) -> T:
    raw = extract_json(text)
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ContractError(
            f"输出不是合法 JSON：{err.msg}（第 {err.lineno} 行第 {err.colno} 列）",
            node_id=node_id,
            raw=text,
        ) from err
    if not isinstance(data, dict):
        raise ContractError(
            f"输出必须是 JSON 对象，收到 {type(data).__name__}", node_id=node_id, raw=text
        )
    try:
        return model.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or '<root>'}: {item['msg']}"
            for item in err.errors()[:5]
        )
        raise ContractError(
            f"输出不符合契约：{problems}",
            node_id=node_id,
            raw=text,
            repair_hint=problems,
        ) from err


def contract_block(model: type[BaseModel]) -> str:
    """Render a human/LLM readable description of an output contract."""
    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2)

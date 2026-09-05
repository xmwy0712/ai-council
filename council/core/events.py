"""Append-only event log vocabulary.

The log is the truth. Current session state is *defined* as the fold of all
events (see :mod:`council.core.state`), which is what makes crash recovery and
``council resume`` correct by construction instead of by luck.

Every model call is bracketed by ``CallIssued`` (written *before* dispatch) and
``CallCompleted`` / ``CallFailed`` (written after). A call that has an issued
event but no terminal event is in-flight and gets re-dispatched on resume; a
completed call is replayed from the log and never re-billed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Usage
from .machine import Phase

__all__ = [
    "EVENT_PAYLOADS",
    "CallChunk",
    "CallCompleted",
    "CallFailed",
    "CallIssued",
    "CallKey",
    "ConfigChanged",
    "Convergence",
    "Event",
    "EventType",
    "NodeStateChanged",
    "PhaseCompleted",
    "PhaseStarted",
    "SessionCreated",
    "SessionStatusChanged",
    "UserDecision",
    "new_event",
    "utc_now",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class EventType(StrEnum):
    SESSION_CREATED = "SessionCreated"
    PHASE_STARTED = "PhaseStarted"
    PHASE_COMPLETED = "PhaseCompleted"
    CALL_ISSUED = "CallIssued"
    CALL_CHUNK = "CallChunk"
    CALL_COMPLETED = "CallCompleted"
    CALL_FAILED = "CallFailed"
    USER_DECISION = "UserDecision"
    CONFIG_CHANGED = "ConfigChanged"
    CONVERGENCE = "Convergence"
    NODE_STATE = "NodeStateChanged"
    SESSION_STATUS = "SessionStatusChanged"


class CallKey(BaseModel):
    """Idempotency key: ``(session_id, phase, round, node_id, attempt)``.

    ``session_id`` lives on the event envelope; the key itself is phase-scoped.
    """

    model_config = ConfigDict(frozen=True)

    phase: str
    round: int
    node_id: str
    attempt: int
    #: Disambiguates repeated calls inside one phase (e.g. debate round 2 of 2).
    #: Empty for every phase that calls a node at most once.
    tag: str = ""

    def as_key(self) -> str:
        base = f"{self.phase}:{self.round}:{self.node_id}:{self.attempt}"
        return f"{base}#{self.tag}" if self.tag else base


class SessionCreated(BaseModel):
    question: str
    config_fingerprint: str
    language: str
    participants: list[str] = Field(default_factory=list)
    judge: str | None = None
    #: File *names* only — content is re-supplied on resume, never logged.
    attachments: list[str] = Field(default_factory=list)


class PhaseStarted(BaseModel):
    phase: str
    round: int = 0
    version: int = 1


class PhaseCompleted(BaseModel):
    phase: str
    round: int = 0
    version: int = 1
    #: Already-validated structured results. Replaying these is enough to
    #: rebuild session state without re-parsing every call.
    summary: dict[str, Any] = Field(default_factory=dict)


class CallIssued(BaseModel):
    key: str
    node_id: str
    phase: str
    round: int
    attempt: int
    model: str
    adapter: str
    request_hash: str


class CallChunk(BaseModel):
    key: str
    index: int
    text: str


class CallCompleted(BaseModel):
    key: str
    node_id: str
    phase: str
    round: int
    attempt: int
    tag: str = ""
    model: str
    raw_text: str
    parsed: dict[str, Any]
    usage: Usage | None = None
    latency_ms: int = 0
    repaired: int = 0


class CallFailed(BaseModel):
    key: str
    node_id: str
    phase: str
    round: int
    attempt: int
    tag: str = ""
    kind: str
    message: str
    retryable: bool


class UserDecision(BaseModel):
    phase: str
    round: int
    decision: str
    note: str = ""


class ConfigChanged(BaseModel):
    before: str
    after: str
    diff: list[str] = Field(default_factory=list)


class Convergence(BaseModel):
    round: int
    version: int
    must_fix: int
    should_fix: int
    disputes: int
    improved: bool


class NodeStateChanged(BaseModel):
    node_id: str
    state: str  # healthy | degraded
    reason: str


class SessionStatusChanged(BaseModel):
    status: str  # running | paused | awaiting_user | completed | stalled
    reason: str


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int = 0
    ts: datetime = Field(default_factory=utc_now)
    session_id: str
    type: EventType
    payload: BaseModel


EVENT_PAYLOADS: Final[dict[EventType, type[BaseModel]]] = {
    EventType.SESSION_CREATED: SessionCreated,
    EventType.PHASE_STARTED: PhaseStarted,
    EventType.PHASE_COMPLETED: PhaseCompleted,
    EventType.CALL_ISSUED: CallIssued,
    EventType.CALL_CHUNK: CallChunk,
    EventType.CALL_COMPLETED: CallCompleted,
    EventType.CALL_FAILED: CallFailed,
    EventType.USER_DECISION: UserDecision,
    EventType.CONFIG_CHANGED: ConfigChanged,
    EventType.CONVERGENCE: Convergence,
    EventType.NODE_STATE: NodeStateChanged,
    EventType.SESSION_STATUS: SessionStatusChanged,
}


def new_event(
    session_id: str,
    type_: EventType,
    payload: BaseModel,
    *,
    ts: datetime | None = None,
) -> Event:
    expected = EVENT_PAYLOADS[type_]
    if not isinstance(payload, expected):
        raise TypeError(
            f"事件 {type_.value} 的载荷必须是 {expected.__name__}，收到 {type(payload).__name__}"
        )
    return Event(seq=0, ts=ts or utc_now(), session_id=session_id, type=type_, payload=payload)


def phase_key(phase: Phase | str) -> str:
    return phase.value if isinstance(phase, Phase) else phase

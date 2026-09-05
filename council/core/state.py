"""Projection: current state as a pure fold over the event log.

Nothing here makes decisions. The engine decides, the log records, and this
module turns the log back into a state. That is what makes ``council resume``
boring: replay the events, and you are exactly where you were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .config import Config
from .contracts import Usage
from .events import (
    CallCompleted,
    CallFailed,
    CallIssued,
    CallKey,
    Convergence,
    Event,
    EventType,
    NodeStateChanged,
    PhaseCompleted,
    PhaseStarted,
    SessionCreated,
    SessionStatusChanged,
    UserDecision,
)
from .machine import PHASE_ORDER, Phase

__all__ = [
    "CallRecord",
    "RoundState",
    "SessionState",
    "SessionStatus",
    "next_phase_after",
    "replay",
]


class SessionStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_USER = "awaiting_user"
    COMPLETED = "completed"
    STALLED = "stalled"


def next_phase_after(
    phase: str,
    summary: dict[str, Any],
    *,
    round_index: int = 0,
    max_rounds: int = 8,
) -> str:
    """Deterministic successor of a completed phase.

    Three non-linear edges exist, all driven by structured fields:

    * ``VERDICT`` + ``status == PASS`` escapes the revision loop,
    * ``REVISION`` loops back to ``REVIEW`` for another round,
    * ``REVISION`` at ``max_rounds`` gives up looping and goes to ``FINAL``.

    No prose inspection is involved anywhere.
    """
    current = Phase(phase)
    if current is Phase.VERDICT:
        status = str((summary.get("verdict") or {}).get("status", "NEED_REVISION"))
        return Phase.FINAL.value if status == "PASS" else Phase.REVISION.value
    if current is Phase.REVISION:
        if round_index >= max_rounds:
            return Phase.FINAL.value
        return Phase.REVIEW.value
    index = PHASE_ORDER.index(current)
    return PHASE_ORDER[min(index + 1, len(PHASE_ORDER) - 1)].value


@dataclass
class CallRecord:
    key: CallKey
    status: str  # issued | completed | failed
    raw_text: str = ""
    parsed: dict[str, Any] | None = None
    usage: Usage | None = None
    attempt: int = 1


@dataclass
class RoundState:
    index: int
    version: int
    author: str
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    debates: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: dict[str, Any] | None = None
    revision: dict[str, Any] | None = None


class SessionState:
    def __init__(self, session_id: str, config: Config) -> None:
        self.session_id = session_id
        self.config = config
        self.status = SessionStatus.RUNNING
        self.phase: str = Phase.CONTEXT.value
        self.round_index = 0
        self.version = 1
        self.question = ""
        self.proposals: dict[str, dict[str, Any]] = {}
        self.scores: list[dict[str, Any]] = []
        self.selected: str | None = None
        self.current_proposal: dict[str, Any] | None = None
        self.rounds: list[RoundState] = []
        self.convergence: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.calls: dict[str, CallRecord] = {}
        self.entered: set[tuple[str, int]] = set()
        self.degraded: set[str] = set()
        self.absentees: dict[int, list[str]] = {}
        self.usage = Usage()

    # ------------------------------------------------------------------ fold

    def apply(self, event: Event) -> None:
        kind = event.type
        payload = event.payload
        if kind is EventType.SESSION_CREATED:
            assert isinstance(payload, SessionCreated)
            self.question = payload.question
        elif kind is EventType.PHASE_STARTED:
            assert isinstance(payload, PhaseStarted)
            self.phase = payload.phase
            self.round_index = payload.round
            self.version = payload.version
            self.entered.add((payload.phase, payload.round))
        elif kind is EventType.PHASE_COMPLETED:
            assert isinstance(payload, PhaseCompleted)
            self._apply_summary(payload)
            self.phase = next_phase_after(
                payload.phase,
                payload.summary,
                round_index=payload.round,
                max_rounds=self.config.council.max_rounds,
            )
        elif kind is EventType.CALL_ISSUED:
            assert isinstance(payload, CallIssued)
            self.calls[payload.key] = CallRecord(
                key=CallKey(
                    phase=payload.phase,
                    round=payload.round,
                    node_id=payload.node_id,
                    attempt=payload.attempt,
                ),
                status="issued",
                attempt=payload.attempt,
            )
        elif kind is EventType.CALL_CHUNK:
            pass  # chunks exist for live streaming; the log keeps them for replay UI only
        elif kind is EventType.CALL_COMPLETED:
            assert isinstance(payload, CallCompleted)
            record = CallRecord(
                key=CallKey(
                    phase=payload.phase,
                    round=payload.round,
                    node_id=payload.node_id,
                    attempt=payload.attempt,
                ),
                status="completed",
                raw_text=payload.raw_text,
                parsed=payload.parsed,
                usage=payload.usage,
                attempt=payload.attempt,
            )
            self.calls[payload.key] = record
            if payload.usage is not None:
                self.usage = self.usage + payload.usage
        elif kind is EventType.CALL_FAILED:
            assert isinstance(payload, CallFailed)
            existing = self.calls.get(payload.key)
            if existing is not None:
                existing.status = "failed"
        elif kind is EventType.USER_DECISION:
            assert isinstance(payload, UserDecision)
            self.decisions.append(
                {
                    "phase": payload.phase,
                    "round": payload.round,
                    "decision": payload.decision,
                    "note": payload.note,
                }
            )
        elif kind is EventType.CONVERGENCE:
            assert isinstance(payload, Convergence)
            self.convergence.append(payload.model_dump())
        elif kind is EventType.NODE_STATE:
            assert isinstance(payload, NodeStateChanged)
            if payload.state == "degraded":
                self.degraded.add(payload.node_id)
            else:
                self.degraded.discard(payload.node_id)
        elif kind is EventType.SESSION_STATUS:
            assert isinstance(payload, SessionStatusChanged)
            self.status = SessionStatus(payload.status)
        elif kind is EventType.CONFIG_CHANGED:
            pass  # recorded for audit; the engine reconciles config separately

    def _apply_summary(self, payload: PhaseCompleted) -> None:
        summary = payload.summary
        phase = Phase(payload.phase)
        if phase is Phase.PROPOSAL:
            self.proposals.update(summary.get("proposals", {}))
            if self.selected is None and self.proposals:
                self.selected = summary.get("selected")
        elif phase is Phase.SELECT:
            self.selected = summary.get("selected")
            self.scores = list(summary.get("scores", []))
            if self.selected is not None and self.selected in self.proposals:
                self.current_proposal = self.proposals[self.selected]
        elif phase is Phase.REVIEW:
            self.current_round().reviews.update(summary.get("reviews", {}))
            for node_id in summary.get("absentees", []):
                self.absentees.setdefault(payload.round, [])
                if node_id not in self.absentees[payload.round]:
                    self.absentees[payload.round].append(node_id)
        elif phase is Phase.DEBATE:
            self.current_round().debates.update(summary.get("debates", {}))
        elif phase is Phase.VERDICT:
            self.current_round().verdict = summary.get("verdict")
        elif phase is Phase.REVISION:
            self.version = int(summary.get("version", self.version + 1))
            self.current_round().revision = summary.get("revision")
            if summary.get("revision"):
                self.current_proposal = summary["revision"]
        elif phase is Phase.FINAL:
            self.status = SessionStatus.COMPLETED

    # --------------------------------------------------------------- helpers

    def current_round(self) -> RoundState:
        if not self.rounds or self.rounds[-1].index != self.round_index:
            self.rounds.append(
                RoundState(
                    index=self.round_index,
                    version=self.version,
                    author=self.selected or "",
                )
            )
        return self.rounds[-1]

    def completed_call(
        self,
        phase: str,
        round_index: int,
        node_id: str,
        tag: str = "",
    ) -> CallRecord | None:
        """Most recent *completed* call for a (phase, round, node, tag) tuple.

        Used by resume: a finished call is replayed from the log and never
        re-billed, regardless of which attempt produced it.
        """
        best: CallRecord | None = None
        for record in self.calls.values():
            if (
                record.status == "completed"
                and record.key.phase == phase
                and record.key.round == round_index
                and record.key.node_id == node_id
                and record.key.tag == tag
                and (best is None or record.key.attempt > best.key.attempt)
            ):
                best = record
        return best

    def in_flight_calls(self) -> list[CallRecord]:
        return [r for r in self.calls.values() if r.status == "issued"]

    def latest_convergence(self) -> dict[str, Any] | None:
        return self.convergence[-1] if self.convergence else None

    def stalled(self) -> bool:
        """True when the last two rounds failed to improve the metrics."""
        if len(self.convergence) < 2:
            return False
        return not self.convergence[-1]["improved"] and not self.convergence[-2]["improved"]


def replay(session_id: str, config: Config, events: list[Event]) -> SessionState:
    state = SessionState(session_id, config)
    for event in events:
        state.apply(event)
    return state

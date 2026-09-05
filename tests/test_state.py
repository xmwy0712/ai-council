"""Projection: state is a pure fold over the event log."""

from __future__ import annotations

from council.core.config import default_config, parse_config
from council.core.events import (
    CallCompleted,
    CallKey,
    Convergence,
    Event,
    EventType,
    PhaseCompleted,
    PhaseStarted,
    new_event,
)
from council.core.machine import Phase
from council.core.state import SessionState, SessionStatus, next_phase_after, replay


def _event(type_: EventType, payload) -> Event:
    return new_event("s1", type_, payload)


def test_next_phase_after_is_linear_by_default() -> None:
    assert next_phase_after("P0", {}) == "P1"
    assert next_phase_after("P1", {}) == "P2"
    assert next_phase_after("P2", {}) == "P3"
    assert next_phase_after("P3", {}) == "P4"
    assert next_phase_after("P4", {}) == "P5"


def test_pass_escapes_the_loop() -> None:
    assert next_phase_after("P5", {"verdict": {"status": "PASS"}}) == "P7"


def test_need_revision_enters_revision() -> None:
    assert next_phase_after("P5", {"verdict": {"status": "NEED_REVISION"}}) == "P6"
    assert next_phase_after("P5", {"verdict": {"status": "NEED_USER_DECISION"}}) == "P6"
    assert next_phase_after("P5", {}) == "P6"


def test_revision_loops_or_terminates_at_max_rounds() -> None:
    assert next_phase_after("P6", {}, round_index=1, max_rounds=8) == "P3"
    assert next_phase_after("P6", {}, round_index=8, max_rounds=8) == "P7"


def test_replay_rebuilds_proposals_and_selection() -> None:
    config = default_config()
    events = [
        _event(EventType.PHASE_STARTED, PhaseStarted(phase="P1", round=0, version=1)),
        _event(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase="P1",
                round=0,
                version=1,
                summary={"proposals": {"n1": {"proposal": "A"}, "n2": {"proposal": "B"}}},
            ),
        ),
        _event(EventType.PHASE_STARTED, PhaseStarted(phase="P2", round=0, version=1)),
        _event(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(phase="P2", round=0, version=1, summary={"selected": "n2"}),
        ),
    ]
    state = replay("s1", config, events)
    assert set(state.proposals) == {"n1", "n2"}
    assert state.selected == "n2"
    assert state.current_proposal == {"proposal": "B"}
    assert state.phase == Phase.REVIEW.value


def test_completed_call_is_reused_across_attempts() -> None:
    """Attempt 1 hung, attempt 2 finished: replay must find the finished one."""
    config = default_config()
    events = [
        new_event("s1", EventType.CALL_ISSUED, _issued_payload("P1", 1, "n1", 1)),
        new_event("s1", EventType.CALL_ISSUED, _issued_payload("P1", 1, "n1", 2)),
        new_event(
            "s1",
            EventType.CALL_COMPLETED,
            CallCompleted(
                key="P1:1:n1:2",
                node_id="n1",
                phase="P1",
                round=1,
                attempt=2,
                model="m",
                raw_text="{}",
                parsed={"proposal": "X"},
            ),
        ),
    ]
    state = replay("s1", config, events)
    record = state.completed_call("P1", 1, "n1")
    assert record is not None
    assert record.key.attempt == 2
    assert state.completed_call("P1", 1, "n2") is None


def _issued_payload(phase: str, round_: int, node_id: str, attempt: int):
    from council.core.events import CallIssued

    return CallIssued(
        key=CallKey(phase=phase, round=round_, node_id=node_id, attempt=attempt).as_key(),
        node_id=node_id,
        phase=phase,
        round=round_,
        attempt=attempt,
        model="m",
        adapter="fake",
        request_hash="h",
    )


def test_in_flight_calls_are_detected() -> None:
    config = default_config()
    events = [new_event("s1", EventType.CALL_ISSUED, _issued_payload("P3", 1, "n1", 1))]
    state = replay("s1", config, events)
    assert [r.key.node_id for r in state.in_flight_calls()] == ["n1"]


def test_stalled_after_two_unimproved_rounds() -> None:
    config = default_config()
    events = [
        new_event(
            "s1",
            EventType.CONVERGENCE,
            Convergence(round=1, version=1, must_fix=3, should_fix=0, disputes=1, improved=True),
        ),
        new_event(
            "s1",
            EventType.CONVERGENCE,
            Convergence(round=2, version=2, must_fix=3, should_fix=0, disputes=1, improved=False),
        ),
    ]
    state = replay("s1", config, events)
    assert not state.stalled()
    state.apply(
        new_event(
            "s1",
            EventType.CONVERGENCE,
            Convergence(round=3, version=3, must_fix=3, should_fix=0, disputes=1, improved=False),
        )
    )
    assert state.stalled()


def test_degraded_nodes_survive_replay() -> None:
    from council.core.events import NodeStateChanged

    config = default_config()
    state = SessionState("s1", config)
    state.apply(
        new_event(
            "s1", EventType.NODE_STATE, NodeStateChanged(node_id="n1", state="degraded", reason="x")
        )
    )
    assert "n1" in state.degraded
    state.apply(
        new_event(
            "s1",
            EventType.NODE_STATE,
            NodeStateChanged(node_id="n1", state="healthy", reason="probe"),
        )
    )
    assert state.degraded == set()


def test_session_status_roundtrip() -> None:
    from council.core.events import SessionStatusChanged

    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "a", "adapter": "fake", "model": "m"},
                {"id": "b", "adapter": "fake", "model": "m"},
            ],
        }
    )
    state = SessionState("s1", config)
    state.apply(
        new_event("s1", EventType.SESSION_STATUS, SessionStatusChanged(status="paused", reason="x"))
    )
    assert state.status is SessionStatus.PAUSED

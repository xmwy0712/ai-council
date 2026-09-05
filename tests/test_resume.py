"""Crash recovery: resume replays finished work and re-sends only what is missing.

This is the property the reference prototype did not have at all: it rewrote the
whole session JSON on every round, so an interruption meant starting over (and
it leaked file handles while doing so).
"""

from __future__ import annotations

import pytest
from conftest import cfg, make_engine

from council.core.events import EventType
from council.core.orchestrator import EngineStopped
from council.core.state import SessionStatus


async def _run_until_p1_done(tmp_path, db: str = "sessions.sqlite3") -> tuple[list[str], str]:
    """Run a session and kill it right after P1. Returns (keys called, session id)."""
    harness = await make_engine(tmp_path, db=db)
    holder: dict[str, object] = {"engine": harness.engine}

    async def stopper(event: object) -> None:
        payload = getattr(event, "payload", None)
        if (
            getattr(event, "type", None) is EventType.PHASE_COMPLETED
            and getattr(payload, "phase", None) == "P1"
        ):
            engine = holder["engine"]
            assert hasattr(engine, "stop")
            engine.stop()

    harness.engine.sink = stopper  # type: ignore[assignment]
    with pytest.raises(EngineStopped):
        await harness.engine.run("session-1", "问题")
    await harness.store.close()
    return harness.recorded_keys, "session-1"


async def test_resume_reuses_completed_calls(tmp_path) -> None:
    first_keys, session_id = await _run_until_p1_done(tmp_path)
    assert any(key.startswith("P1:0:") for key in first_keys)

    second = await make_engine(tmp_path, db="sessions.sqlite3")
    state = await second.engine.run(session_id)

    assert state.status is SessionStatus.COMPLETED
    # Nothing from P1 was re-issued: those results came from the log.
    assert not [k for k in second.recorded_keys if k.startswith("P1:0:")]
    # Every remaining key was issued exactly once.
    assert len(second.recorded_keys) == len(set(second.recorded_keys))
    await second.store.close()


async def test_resume_finishes_the_phases_that_never_ran(tmp_path) -> None:
    _first_keys, session_id = await _run_until_p1_done(tmp_path)
    second = await make_engine(tmp_path, db="sessions.sqlite3")
    state = await second.engine.run(session_id)
    await second.store.close()

    phases = {getattr(e.payload, "phase", None) for e in second.of_type("PhaseCompleted")}
    assert phases == {"P2", "P3", "P4", "P5", "P7"}
    assert state.selected is not None


async def test_resume_of_a_completed_session_is_a_noop(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    await harness.engine.run("s1", "问题")
    await harness.store.close()

    second = await make_engine(tmp_path)
    state = await second.engine.run("s1", "问题")
    await second.store.close()

    assert state.status is SessionStatus.COMPLETED
    assert second.recorded_keys == []


async def test_config_drift_is_reported_and_old_config_wins_by_default(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    await harness.engine.run("s1", "问题")
    await harness.store.close()

    changed = await make_engine(tmp_path)
    changed.config.council.max_rounds = 3
    state = await changed.engine.run("s1", "问题")

    assert changed.engine.drift is not None
    assert changed.engine.drift.before != changed.engine.drift.after
    assert any("max_rounds" in line for line in changed.engine.drift.diff)
    # No drift handler was supplied, so the session continues under its own rules.
    assert changed.engine.config.council.max_rounds == 8
    assert state.status is SessionStatus.COMPLETED
    await changed.store.close()


async def test_drift_handler_can_adopt_the_new_config(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    await harness.engine.run("s1", "问题")
    await harness.store.close()

    async def adopt_new(drift: object) -> str:
        return "new"

    resuming = await make_engine(tmp_path)
    resuming.config.council.max_rounds = 3
    resuming.engine._drift_handler = adopt_new  # type: ignore[assignment]
    await resuming.engine.run("s1", "问题")

    meta = await resuming.store.get_session("s1")
    assert meta is not None
    assert meta.config.council.max_rounds == 3
    assert any(e.type is EventType.CONFIG_CHANGED for e in resuming.events)
    await resuming.store.close()


async def test_frozen_session_is_resumable_from_disk(tmp_path) -> None:
    """Both reviewers fail -> the engine freezes -> `council resume` finishes it."""
    from council.core.errors import CouncilError, ErrorKind

    config = cfg(participants=3)
    config.failure.policy = "continue"
    failures = {
        "n2": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n2")],
        "n3": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n3")],
    }
    frozen = await make_engine(tmp_path, failures=failures, resume_timeout_s=0.0)
    state = await frozen.engine.run("s1", "问题")
    await frozen.store.close()

    assert state.status is SessionStatus.PAUSED
    # Nothing from P3 was ever completed, so the log has no verdict yet.
    assert not any(
        e.type is EventType.PHASE_COMPLETED
        for e in frozen.of_type("PhaseCompleted")
        if e.payload.phase == "P3"
    )

    resumed = await make_engine(tmp_path)
    final = await resumed.engine.run("s1", "问题")
    await resumed.store.close()
    assert final.status is SessionStatus.COMPLETED
    assert final.current_proposal is not None

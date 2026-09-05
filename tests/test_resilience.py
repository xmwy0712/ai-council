"""M3 resilience hardening: stall detection, in-flight resume, outage recovery,
mid-flight policy switches and intervention actions.

The defining property under test everywhere: a resumed session re-sends only
work that never completed — nothing is re-billed, nothing is silently skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import RecordingHandler, cfg, make_engine

from council.adapters import register
from council.adapters.fake import FakeAdapter, default_reply
from council.core.config import parse_config
from council.core.errors import CouncilError, ErrorKind
from council.core.events import EventType
from council.core.orchestrator import (
    CouncilEngine,
    EngineStopped,
    InterventionAction,
    InterventionDecision,
)
from council.core.state import SessionStatus


def _phase(req: Any) -> str:
    return str(req.metadata.get("phase", ""))


# ---------------------------------------------------------------- stall clock


async def test_idle_stall_is_detected_and_classified(tmp_path) -> None:
    """No new output for idle_s => timeout, not an infinite hang."""
    config = cfg(participants=3)
    config.failure.policy = "continue"
    node = config.node("n1")
    node.overrides.idle_s = 0.05
    node.overrides.max_retries = 0

    class SlowAdapter(FakeAdapter):
        def __init__(self, node_id: str) -> None:
            super().__init__(node_id, model=node.model, delay_s=0.5)

    harness = await make_engine(tmp_path, config=config)
    harness.engine.adapters["n1"] = SlowAdapter("n1")

    state = await harness.engine.run("s1", "问题")

    assert "n1" not in state.proposals
    failed = [e for e in harness.of_type("CallFailed") if e.payload.node_id == "n1"]
    assert failed and failed[0].payload.kind == "timeout"
    assert failed[0].payload.retryable is True


# ------------------------------------------------------------ in-flight resume


async def test_interrupted_stream_is_retried_on_resume(tmp_path) -> None:
    """A kill mid-stream leaves CallIssued without CallCompleted; resume must
    re-send exactly those calls and none of the finished ones."""
    harness = await make_engine(tmp_path)
    seen: list[Any] = []

    async def crash_mid_stream(event: Any) -> None:
        seen.append(event)
        if event.type is EventType.CALL_CHUNK and str(event.payload.key).startswith("P1:"):
            raise EngineStopped("模拟进程被杀")

    harness.engine.sink = crash_mid_stream  # type: ignore[assignment]
    with pytest.raises(EngineStopped):
        await harness.engine.run("s1", "问题")

    issued = {
        e.payload.key for e in seen if e.type is EventType.CALL_ISSUED and e.payload.phase == "P1"
    }
    done = {
        e.payload.key
        for e in seen
        if e.type is EventType.CALL_COMPLETED and e.payload.phase == "P1"
    }
    in_flight = issued - done
    assert in_flight, "测试前提：至少一次调用被中断在流中途"
    await harness.store.close()

    second = await make_engine(tmp_path)
    state = await second.engine.run("s1", "问题")
    await second.store.close()

    assert state.status is SessionStatus.COMPLETED
    for key in in_flight:
        assert key in second.recorded_keys, f"中断的调用 {key} 必须重发"
    for key in done:
        assert key not in second.recorded_keys, f"已完成的调用 {key} 不得重发（重复计费）"


# ------------------------------------------------------------ outage recovery


async def test_outage_blocks_then_auto_recovers(tmp_path, monkeypatch) -> None:
    """All nodes fail with retryable errors -> the phase freezes, one health
    probe round re-admits them, and the phase retries in place."""
    monkeypatch.setattr(CouncilEngine, "_backoff", staticmethod(lambda attempt: 0.01))
    config = cfg(participants=3, cooldown=0.05)
    config.failure.policy = "continue"
    config.failure.max_retries = 1  # 2 attempts per call, then drop
    failures = {
        node: [CouncilError(ErrorKind.NETWORK, "断网", node_id=node) for _ in range(2)]
        for node in ("n1", "n2", "n3")
    }
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")

    assert state.status is SessionStatus.COMPLETED
    reasons = [e.payload.reason for e in harness.of_type("SessionStatusChanged")]
    assert any("所有参与者" in r for r in reasons)
    assert "recovered" in reasons
    assert set(state.proposals) == {"n1", "n2", "n3"}


async def test_persistent_outage_unwinds_to_resume(tmp_path, monkeypatch) -> None:
    """When probes still fail, the session freezes; `council resume` finishes."""
    monkeypatch.setattr(CouncilEngine, "_backoff", staticmethod(lambda attempt: 0.01))
    config = cfg(participants=3, cooldown=0.02)
    config.failure.policy = "continue"
    config.failure.max_retries = 0

    class DownAdapter(FakeAdapter):
        async def health(self):  # type: ignore[override]
            from council.core.contracts import HealthStatus

            return HealthStatus(ok=False, detail="still down", error_kind="network")

        def chat(self, req):  # type: ignore[override]
            return self._down(req)

        async def _down(self, req):
            yield _error_chunk(req)

    def _error_chunk(req: Any) -> Any:
        from council.core.contracts import ChatChunk, ChunkType

        return ChatChunk(
            type=ChunkType.ERROR,
            error=CouncilError(ErrorKind.NETWORK, "断网", node_id=self_node_id),
        )

    self_node_id = "n1"
    harness = await make_engine(tmp_path, config=config, failures={})
    for node_id in ("n1", "n2", "n3"):
        harness.adapters[node_id] = DownAdapter(node_id, model="fake")
    # Replace the engine's adapter map so rebuilds keep the downed adapters.
    harness.engine.adapters = dict(harness.adapters)

    state = await harness.engine.run("s1", "问题")
    await harness.store.close()

    assert state.status is SessionStatus.PAUSED
    resumed = await make_engine(tmp_path)
    final = await resumed.engine.run("s1", "问题")
    await resumed.store.close()
    assert final.status is SessionStatus.COMPLETED


# ------------------------------------------------------------- policy control


async def test_policy_switch_takes_effect_mid_run(tmp_path) -> None:
    """ask_user -> continue, flipped from a sink while the session runs."""
    config = cfg(participants=3)
    assert config.judge is not None
    config.failure.policy = "ask_user"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}
    handler = RecordingHandler()
    harness = await make_engine(tmp_path, config=config, failures=failures, handler=handler)

    async def switch_policy(event: Any) -> None:
        if event.type is EventType.PHASE_STARTED and event.payload.phase == "P1":
            harness.engine.set_failure_policy(parse_config_policy_continue())

    harness.engine.sink = switch_policy  # type: ignore[assignment]
    state = await harness.engine.run("s1", "问题")

    assert "n1" not in state.proposals
    assert handler.failures == []  # nobody was asked: the switch already won


def parse_config_policy_continue():
    from council.core.config import FailurePolicy

    return FailurePolicy.CONTINUE


async def test_pause_policy_really_freezes(tmp_path) -> None:
    """policy=pause must freeze (regression: the gate used to stay open)."""
    config = cfg(participants=3)
    config.failure.policy = "pause"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")
    await harness.store.close()

    assert state.status is SessionStatus.PAUSED

    resumed = await make_engine(tmp_path)
    final = await resumed.engine.run("s1", "问题")
    await resumed.store.close()
    assert final.status is SessionStatus.COMPLETED


# ------------------------------------------------------------- interventions


async def test_wait_action_freezes_then_resumes(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "ask_user"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}

    class ResumeSoonHandler(RecordingHandler):
        engine: CouncilEngine | None = None

        async def on_failure(self, req):  # type: ignore[override]
            decision = await super().on_failure(req)
            if self.engine is not None:
                loop = asyncio.get_running_loop()
                loop.call_later(0.05, self.engine.resume)
            return decision

    handler = ResumeSoonHandler(
        on_failure_answer=InterventionDecision(action=InterventionAction.WAIT)
    )
    harness = await make_engine(tmp_path, config=config, failures=failures, handler=handler)
    handler.engine = harness.engine
    state = await harness.engine.run("s1", "问题")

    assert "n1" in state.proposals
    assert state.status is SessionStatus.COMPLETED


async def test_switch_adapter_rebuilds_and_recovers(tmp_path) -> None:
    calls = {"n": 0}

    class FlakyAdapter(FakeAdapter):
        def __init__(self, node: Any) -> None:
            super().__init__(node.id, model=node.model)

        async def chat(self, req):  # type: ignore[override]
            calls["n"] += 1
            if calls["n"] == 1:
                yield _auth_error_chunk("n1")
                return
            async for chunk in super().chat(req):
                yield chunk

    def _auth_error_chunk(node_id: str) -> Any:
        from council.core.contracts import ChatChunk, ChunkType

        return ChatChunk(
            type=ChunkType.ERROR,
            error=CouncilError(ErrorKind.AUTH, "密钥无效", node_id=node_id),
        )

    register("flaky", lambda node: FlakyAdapter(node.id))

    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "failure": {"policy": "ask_user", "circuit_cooldown_s": 0.01},
            "nodes": [
                {"id": "n1", "adapter": "flaky", "model": "fake/n1"},
                {"id": "n2", "adapter": "fake", "model": "fake/n2"},
                {"id": "judge", "adapter": "fake", "model": "fake/judge", "role": "judge"},
            ],
            "judge": {"node_id": "judge"},
        }
    )
    handler = RecordingHandler(
        on_failure_answer=InterventionDecision(
            action=InterventionAction.SWITCH_ADAPTER, adapter="fake"
        )
    )
    harness = await make_engine(tmp_path, config=config, handler=handler)
    harness.engine.adapters["n1"] = FlakyAdapter(config.node("n1"))
    old_adapter = harness.engine.adapters["n1"]
    state = await harness.engine.run("s1", "问题")

    assert handler.failures and handler.failures[0].node_id == "n1"
    assert harness.engine.adapters["n1"] is not old_adapter
    assert "n1" in state.proposals
    assert state.status is SessionStatus.COMPLETED


async def test_judge_failure_falls_back_to_human_selection(tmp_path) -> None:
    """Judge outage must never produce a silent pick: a human decides."""
    config = cfg(participants=3)
    handler = RecordingHandler(on_select_answer="n2")

    def judge_down(req: Any) -> str:
        if req.metadata.get("node_id") == "judge" and _phase(req) == "P2":
            raise CouncilError(ErrorKind.AUTH, "judge 未登录", node_id="judge")
        return default_reply(req)

    harness = await make_engine(
        tmp_path, config=config, responders={"judge": judge_down}, handler=handler
    )
    state = await harness.engine.run("s1", "问题")

    assert state.selected == "n2"
    assert handler.selections
    decisions = [e.payload.decision for e in harness.of_type("UserDecision")]
    assert "n2" in decisions

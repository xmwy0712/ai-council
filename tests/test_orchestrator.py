"""Engine behaviour: convergence, failure handling, escalation.

Every test here runs offline against the fake adapter.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import RecordingHandler, cfg, make_engine

from council.adapters.fake import default_reply
from council.core.config import parse_config
from council.core.errors import CouncilError, ErrorKind
from council.core.events import EventType
from council.core.orchestrator import (
    EngineStopped,
    InterventionAction,
    InterventionDecision,
)
from council.core.state import SessionStatus


def _reply(req: Any) -> str:
    return default_reply(req)


def _phase(req: Any) -> str:
    return str(req.metadata.get("phase", ""))


def _round(req: Any) -> str:
    return str(req.metadata.get("round", ""))


# ------------------------------------------------------------------ happy path


async def test_full_run_converges_and_emits_every_phase(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    state = await harness.engine.run("s1", "测试问题")

    assert state.status is SessionStatus.COMPLETED
    assert state.selected in {"n1", "n2", "n3"}
    assert set(state.proposals) == {"n1", "n2", "n3"}

    phases = [
        e.payload.phase
        for e in harness.of_type("PhaseCompleted")  # type: ignore[attr-defined]
    ]
    assert phases == ["P0", "P1", "P2", "P3", "P4", "P5", "P7"]
    assert state.version == 1


async def test_reviews_exclude_the_author(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    state = await harness.engine.run("s1", "问题")
    reviewers = {r for r in state.rounds[0].reviews}
    assert state.selected not in reviewers
    assert len(reviewers) == 2


async def test_proposals_are_dispatched_in_parallel(tmp_path) -> None:
    """Three 0.2s proposals must not take 0.6s.

    The reference prototype ran everything after P1 serially; this is the
    regression guard for that.
    """
    started: dict[str, float] = {}

    async def sink(event: Any) -> None:
        if event.type is EventType.PHASE_STARTED and event.payload.phase == "P1":
            started["t0"] = asyncio.get_running_loop().time()
        if event.type is EventType.PHASE_COMPLETED and event.payload.phase == "P1":
            started["t1"] = asyncio.get_running_loop().time()

    harness = await make_engine(tmp_path, sink=sink, delay_s=0.2)
    await harness.engine.run("s1", "问题")
    elapsed = started["t1"] - started["t0"]
    assert elapsed < 0.45, f"P1 串行执行了（{elapsed:.2f}s）"


# -------------------------------------------------------------- revision loop


def _revision_then_pass(req: Any) -> str:
    if _phase(req) == "P5" and _round(req) == "1":
        return json.dumps(
            {
                "status": "NEED_REVISION",
                "must_fix": [{"id": "i1", "severity": "must", "text": "缺少验收指标"}],
                "should_fix": [],
                "disputes": [],
                "rationale": "必须给出可观测的验收标准",
            },
            ensure_ascii=False,
        )
    return default_reply(req)


async def test_need_revision_produces_a_new_version_and_a_new_round(tmp_path) -> None:
    responders = {"judge": _revision_then_pass}
    harness = await make_engine(tmp_path, responders=responders)
    state = await harness.engine.run("s1", "问题")

    assert state.status is SessionStatus.COMPLETED
    assert state.version == 2
    assert len(state.rounds) == 2
    assert state.rounds[0].verdict is not None
    assert state.rounds[0].verdict["status"] == "NEED_REVISION"  # type: ignore[index]
    assert state.rounds[1].verdict["status"] == "PASS"  # type: ignore[index]
    assert state.rounds[0].revision is not None


async def test_convergence_metrics_are_recorded_per_round(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    state = await harness.engine.run("s1", "问题")
    assert state.convergence
    assert state.convergence[0]["improved"] is True


# ------------------------------------------------------------- escalation


async def test_garbage_output_is_repaired(tmp_path) -> None:
    state_box = {"n1_bad_replies": 0}

    def flaky(req: Any) -> str:
        if req.metadata.get("node_id") == "n1" and _phase(req) == "P1":
            state_box["n1_bad_replies"] += 1
            if state_box["n1_bad_replies"] == 1:
                return "这不是 JSON，我只是想聊聊方案。"
        return default_reply(req)

    harness = await make_engine(tmp_path, responders={"n1": flaky})
    state = await harness.engine.run("s1", "问题")
    assert "n1" in state.proposals
    completed = [e for e in harness.of_type("CallCompleted") if e.payload.node_id == "n1"]
    assert any(e.payload.repaired == 1 for e in completed)


async def test_persistent_contract_failure_drops_the_node_without_looping(tmp_path) -> None:
    def always_bad(req: Any) -> str:
        if req.metadata.get("node_id") == "n1" and _phase(req) == "P1":
            return "我拒绝输出 JSON。"
        return default_reply(req)

    config = cfg(participants=3)
    config.failure.policy = "continue"
    harness = await make_engine(tmp_path, config=config, responders={"n1": always_bad})
    state = await harness.engine.run("s1", "问题")

    assert "n1" not in state.proposals
    assert set(state.proposals) == {"n2", "n3"}
    summaries = harness.summaries("P1")
    assert summaries[0]["absentees"] == ["n1"]


async def test_auth_failure_is_not_retried(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "continue"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "API key 无效", node_id="n1")]}
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")

    assert "n1" not in state.proposals
    p1_calls = [c for c in harness.adapters["n1"].calls if c.metadata.get("phase") == "P1"]
    assert len(p1_calls) == 1
    failed = [e for e in harness.of_type("CallFailed") if e.payload.node_id == "n1"]
    assert failed and failed[0].payload.kind == "auth"


async def test_retryable_failure_is_retried(tmp_path) -> None:
    config = cfg(participants=3)
    failures = {"n1": [CouncilError(ErrorKind.RATE_LIMIT, "429", node_id="n1")]}
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")

    assert "n1" in state.proposals
    assert len(harness.adapters["n1"].calls) == 2


async def test_degraded_node_pauses_then_recovers_on_probe(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.circuit_threshold = 1
    config.failure.min_quorum = 3
    config.failure.policy = "continue"
    # AUTH is not retryable, so the node fails fast and trips the breaker.
    failures = {"n3": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n3")]}
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")

    assert state.status is SessionStatus.COMPLETED
    statuses = [e.payload.status for e in harness.of_type("SessionStatusChanged")]
    assert "paused" in statuses
    node_states = [
        (e.payload.node_id, e.payload.state) for e in harness.of_type("NodeStateChanged")
    ]
    assert ("n3", "degraded") in node_states
    assert ("n3", "healthy") in node_states


async def test_need_user_decision_asks_the_handler(tmp_path) -> None:
    def blocking(req: Any) -> str:
        if _phase(req) == "P5":
            return json.dumps(
                {
                    "status": "NEED_USER_DECISION",
                    "must_fix": [],
                    "should_fix": [],
                    "disputes": [{"id": "d1", "severity": "must", "text": "预算上限无法确定"}],
                    "rationale": "需要用户做价值取舍",
                    "question_for_user": "预算上限是 10 万还是 50 万？",
                },
                ensure_ascii=False,
            )
        return default_reply(req)

    handler = RecordingHandler(on_decision_answer="预算上限设为 30 万")
    harness = await make_engine(tmp_path, responders={"judge": blocking}, handler=handler)
    state = await harness.engine.run("s1", "问题")

    assert handler.decisions
    assert "预算上限" in handler.decisions[0].question
    assert state.decisions
    assert state.decisions[0]["decision"] == "预算上限设为 30 万"


async def test_two_unimproved_rounds_stall_the_session(tmp_path) -> None:
    def stuck(req: Any) -> str:
        if _phase(req) == "P5":
            return json.dumps(
                {
                    "status": "NEED_REVISION",
                    "must_fix": [
                        {"id": "a", "severity": "must", "text": "问题 A"},
                        {"id": "b", "severity": "must", "text": "问题 B"},
                    ],
                    "should_fix": [],
                    "disputes": [],
                    "rationale": "仍未解决",
                },
                ensure_ascii=False,
            )
        return default_reply(req)

    handler = RecordingHandler()
    harness = await make_engine(tmp_path, responders={"judge": stuck}, handler=handler)
    state = await harness.engine.run("s1", "问题")

    statuses = [e.payload.status for e in harness.of_type("SessionStatusChanged")]
    assert "stalled" in statuses
    assert len(state.rounds) == 3
    assert handler.decisions


# ------------------------------------------------------------ interventions


async def test_human_selection_is_used_when_auto_select_is_off(tmp_path) -> None:
    config = cfg(participants=3)
    assert config.judge is not None
    config.judge.auto_select = False
    handler = RecordingHandler(on_select_answer="n3")
    harness = await make_engine(tmp_path, config=config, handler=handler)
    state = await harness.engine.run("s1", "问题")

    assert state.selected == "n3"
    assert handler.selections
    decisions = [e.payload.decision for e in harness.of_type("UserDecision")]
    assert "n3" in decisions


async def test_ask_user_policy_offers_all_actions(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "ask_user"
    # AUTH is not retryable, so the engine must escalate to the handler instead
    # of silently retrying on its own.
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}
    handler = RecordingHandler(
        on_failure_answer=InterventionDecision(action=InterventionAction.RETRY)
    )
    harness = await make_engine(tmp_path, config=config, failures=failures, handler=handler)
    state = await harness.engine.run("s1", "问题")

    assert handler.failures
    assert set(handler.failures[0].available) == {
        InterventionAction.RETRY,
        InterventionAction.WAIT,
        InterventionAction.SWITCH_MODEL,
        InterventionAction.SWITCH_ADAPTER,
        InterventionAction.DROP_NODE,
        InterventionAction.ABORT,
    }
    assert "n1" in state.proposals


async def test_abort_terminates_the_session(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "ask_user"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}
    handler = RecordingHandler(
        on_failure_answer=InterventionDecision(action=InterventionAction.ABORT)
    )
    harness = await make_engine(tmp_path, config=config, failures=failures, handler=handler)
    with pytest.raises(EngineStopped):
        await harness.engine.run("s1", "问题")


async def test_switch_model_action_changes_the_request(tmp_path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "ask_user"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="n1")]}
    handler = RecordingHandler(
        on_failure_answer=InterventionDecision(
            action=InterventionAction.SWITCH_MODEL, model="fake/replacement"
        )
    )
    harness = await make_engine(tmp_path, config=config, failures=failures, handler=handler)
    await harness.engine.run("s1", "问题")
    assert any(call.model == "fake/replacement" for call in harness.adapters["n1"].calls)


# ------------------------------------------------------------------- config


async def test_quorum_shortfall_pauses(tmp_path) -> None:
    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "failure": {
                "min_quorum": 2,
                "policy": "continue",
                "circuit_threshold": 1,
                "circuit_cooldown_s": 0.01,
            },
            "nodes": [
                {"id": "a", "adapter": "fake", "model": "m"},
                {"id": "b", "adapter": "fake", "model": "m"},
                {"id": "judge", "adapter": "fake", "model": "m", "role": "judge"},
            ],
            "judge": {"node_id": "judge"},
        }
    )
    failures = {"a": [CouncilError(ErrorKind.AUTH, "密钥无效", node_id="a")]}
    harness = await make_engine(tmp_path, config=config, failures=failures)
    state = await harness.engine.run("s1", "问题")
    statuses = [e.payload.status for e in harness.of_type("SessionStatusChanged")]
    assert "paused" in statuses
    assert state.status is SessionStatus.COMPLETED


async def test_pause_and_resume_gate(tmp_path) -> None:
    harness = await make_engine(tmp_path)
    gate: list[str] = []

    async def stop_after_p1(event: Any) -> None:
        if event.type is EventType.PHASE_COMPLETED and event.payload.phase == "P1":
            gate.append("p1")
            harness.engine.pause()

    harness.engine.sink = stop_after_p1
    task = asyncio.create_task(harness.engine.run("s1", "问题"))
    while not gate:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert not task.done()
    harness.engine.resume()
    state = await task
    assert state.status is SessionStatus.COMPLETED


async def test_debate_rounds_are_respected(tmp_path) -> None:
    config = cfg(participants=3, debate_rounds=0)
    harness = await make_engine(tmp_path, config=config)
    state = await harness.engine.run("s1", "问题")
    assert state.rounds[0].debates == {}


async def test_max_rounds_stops_the_loop(tmp_path) -> None:
    def never_pass(req: Any) -> str:
        if _phase(req) == "P5":
            return json.dumps(
                {
                    "status": "NEED_REVISION",
                    "must_fix": [{"id": f"i{_round(req)}", "severity": "must", "text": "再改"}],
                    "should_fix": [],
                    "disputes": [],
                    "rationale": "还不够",
                },
                ensure_ascii=False,
            )
        return default_reply(req)

    config = cfg(participants=3, max_rounds=2)
    harness = await make_engine(tmp_path, config=config, responders={"judge": never_pass})
    state = await harness.engine.run("s1", "问题")
    assert len(state.rounds) == 2
    assert state.status is SessionStatus.COMPLETED

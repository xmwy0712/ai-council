"""Shared test fixtures.

Every test in this suite is offline: the only adapter used is the in-process
fake. Nothing here (or anywhere in the suite) may open a socket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from council.adapters.fake import FakeAdapter
from council.core.config import Config, parse_config
from council.core.events import Event
from council.core.orchestrator import (
    CouncilEngine,
    DecisionRequest,
    InterventionDecision,
    InterventionRequest,
    SelectionRequest,
)
from council.core.store import EventStore

__all__ = ["Harness", "RecordingHandler", "cfg", "make_engine"]

_OPEN_STORES: list[EventStore] = []


@pytest.fixture(autouse=True)
async def close_event_stores():
    """Leave no SQLite connection behind — a leaked handle is exactly the kind of
    bug the reference prototype had."""
    yield
    while _OPEN_STORES:
        store = _OPEN_STORES.pop()
        await store.close()


def cfg(
    *,
    participants: int = 3,
    judge: bool = True,
    cooldown: float = 0.01,
    **council: Any,
) -> Config:
    """Build a valid config of fake nodes. Small cooldown keeps pause tests fast."""
    nodes: list[dict[str, Any]] = [
        {"id": f"n{i}", "adapter": "fake", "model": f"fake/n{i}", "role": "participant"}
        for i in range(1, participants + 1)
    ]
    raw: dict[str, Any] = {
        "council": {"active_nodes": participants, **council},
        "nodes": nodes,
        "failure": {"circuit_cooldown_s": cooldown},
    }
    if judge:
        nodes.append({"id": "judge", "adapter": "fake", "model": "fake/judge", "role": "judge"})
        raw["judge"] = {"enabled": True, "node_id": "judge", "auto_select": True}
    return parse_config(raw)


ReplyFn = Callable[[Any], str]


def make_adapters(
    config: Config,
    responders: dict[str, ReplyFn] | None = None,
    failures: dict[str, list[Any]] | None = None,
    recorder: list[str] | None = None,
    delay_s: float = 0.0,
) -> dict[str, FakeAdapter]:
    adapters: dict[str, FakeAdapter] = {}
    for node in config.nodes:
        if not node.enabled:
            continue
        responder = (responders or {}).get(node.id)

        def build(responder: ReplyFn | None = responder) -> ReplyFn:
            def reply(req: Any) -> str:
                if recorder is not None:
                    recorder.append(str(req.metadata.get("idempotency_key", "")))
                if responder is not None:
                    return responder(req)
                from council.adapters.fake import default_reply

                return default_reply(req)

            return reply

        adapters[node.id] = FakeAdapter(
            node.id,
            model=node.model,
            responder=build(),
            failures=list((failures or {}).get(node.id, [])),
            delay_s=delay_s,
        )
    return adapters


@dataclass
class RecordingHandler:
    """Intervention handler that answers deterministically and records asks."""

    on_failure_answer: Any = None
    on_select_answer: str = ""
    on_decision_answer: str = "按 A 方案执行"
    failures: list[InterventionRequest] = field(default_factory=list)
    selections: list[SelectionRequest] = field(default_factory=list)
    decisions: list[DecisionRequest] = field(default_factory=list)

    async def on_failure(self, req: InterventionRequest) -> InterventionDecision:
        self.failures.append(req)
        if self.on_failure_answer is not None:
            return self.on_failure_answer
        from council.core.orchestrator import InterventionAction

        return InterventionDecision(action=InterventionAction.DROP_NODE)

    async def on_select(self, req: SelectionRequest) -> str:
        self.selections.append(req)
        return self.on_select_answer or req.candidates[0]

    async def on_decision(self, req: DecisionRequest) -> str:
        self.decisions.append(req)
        return self.on_decision_answer


@dataclass
class Harness:
    store: EventStore
    config: Config
    adapters: dict[str, FakeAdapter]
    engine: CouncilEngine
    events: list[Event]
    handler: RecordingHandler | None
    recorded_keys: list[str]

    def of_type(self, *names: str) -> list[Event]:
        return [e for e in self.events if e.type.value in names]

    def summaries(self, phase: str) -> list[dict[str, Any]]:
        out = []
        for event in self.of_type("PhaseCompleted"):
            if event.payload.phase == phase:
                out.append(event.payload.summary)
        return out


Sink = Callable[[Event], Awaitable[None]]


async def make_engine(
    tmp_path: Path,
    *,
    config: Config | None = None,
    responders: dict[str, ReplyFn] | None = None,
    failures: dict[str, list[Any]] | None = None,
    handler: RecordingHandler | None = None,
    sink: Sink | None = None,
    db: str | None = None,
    delay_s: float = 0.0,
    resume_timeout_s: float = 5.0,
) -> Harness:
    config = config or cfg()
    store = await EventStore(tmp_path / (db or "sessions.sqlite3")).open()
    _OPEN_STORES.append(store)
    recorded: list[str] = []
    adapters = make_adapters(config, responders, failures, recorder=recorded, delay_s=delay_s)
    seen: list[Event] = []

    async def capture(event: Event) -> None:
        seen.append(event)
        if sink is not None:
            await sink(event)

    engine = CouncilEngine(
        store=store,
        config=config,
        adapters=adapters,
        handler=handler,
        sink=capture,
        adapter_builder=lambda c: make_adapters(
            c, responders, failures, recorder=recorded, delay_s=delay_s
        ),
        resume_timeout_s=resume_timeout_s,
    )
    return Harness(
        store=store,
        config=config,
        adapters=adapters,
        engine=engine,
        events=seen,
        handler=handler,
        recorded_keys=recorded,
    )

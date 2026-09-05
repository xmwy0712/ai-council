"""Event store: append-only semantics, crash-safe writes, replay fidelity."""

from __future__ import annotations

import pytest

from council.core.config import default_config
from council.core.events import (
    CallIssued,
    EventType,
    PhaseCompleted,
    PhaseStarted,
    SessionCreated,
    new_event,
)
from council.core.store import EventStore, StoreError


async def test_append_assigns_monotonic_seq(tmp_path) -> None:
    async with await EventStore(tmp_path / "s.sqlite3").open() as store:
        await store.create_session("s1", default_config(), question="q")
        events = await store.append(
            "s1",
            [
                (EventType.PHASE_STARTED, PhaseStarted(phase="P0")),
                (EventType.PHASE_COMPLETED, PhaseCompleted(phase="P0", summary={"a": 1})),
            ],
        )
        assert [e.seq for e in events] == [1, 2]
        stored = await store.events("s1")
        assert [e.type for e in stored] == [
            EventType.PHASE_STARTED,
            EventType.PHASE_COMPLETED,
        ]
        assert stored[1].payload.summary == {"a": 1}


async def test_persist_false_skips_the_log(tmp_path) -> None:
    async with await EventStore(tmp_path / "s.sqlite3").open() as store:
        await store.create_session("s1", default_config())
        await store.append(
            "s1",
            [(EventType.PHASE_COMPLETED, PhaseCompleted(phase="P0"))],
            persist=False,
        )
        assert await store.count_events("s1") == 0


async def test_roundtrip_preserves_payload_types(tmp_path) -> None:
    async with await EventStore(tmp_path / "s.sqlite3").open() as store:
        await store.create_session("s1", default_config())
        await store.append(
            "s1",
            [
                (
                    EventType.SESSION_CREATED,
                    SessionCreated(question="q", config_fingerprint="abc", language="zh-CN"),
                ),
                (
                    EventType.CALL_ISSUED,
                    CallIssued(
                        key="P1:1:n1:1",
                        node_id="n1",
                        phase="P1",
                        round=1,
                        attempt=1,
                        model="m",
                        adapter="fake",
                        request_hash="h",
                    ),
                ),
            ],
        )
        stored = await store.events("s1")
        assert isinstance(stored[0].payload, SessionCreated)
        assert isinstance(stored[1].payload, CallIssued)
        assert stored[1].payload.key == "P1:1:n1:1"


async def test_append_is_atomic_on_failure(tmp_path) -> None:
    async with await EventStore(tmp_path / "s.sqlite3").open() as store:
        await store.create_session("s1", default_config())
        with pytest.raises(StoreError):
            await store.append("s1", [(EventType.SESSION_CREATED, PhaseCompleted(phase="P0"))])
        assert await store.count_events("s1") == 0


async def test_session_metadata_and_config_swap(tmp_path) -> None:
    async with await EventStore(tmp_path / "s.sqlite3").open() as store:
        config = default_config()
        await store.create_session("s1", config, question="问题")
        meta = await store.get_session("s1")
        assert meta is not None
        assert meta.question == "问题"

        changed = default_config()
        changed.council.max_rounds = 3
        await store.update_session_config("s1", changed)
        meta = await store.get_session("s1")
        assert meta is not None
        assert meta.config.council.max_rounds == 3

        rows = await store.list_sessions()
        assert [r.session_id for r in rows] == ["s1"]


async def test_unopened_store_raises(tmp_path) -> None:
    store = EventStore(tmp_path / "s.sqlite3")
    with pytest.raises(StoreError, match="尚未打开"):
        await store.append("s1", [(EventType.PHASE_COMPLETED, PhaseCompleted(phase="P0"))])


async def test_new_event_rejects_mismatched_payload() -> None:
    with pytest.raises(TypeError):
        new_event(
            "s1",
            EventType.PHASE_COMPLETED,
            SessionCreated(question="", config_fingerprint="", language="zh-CN"),
        )

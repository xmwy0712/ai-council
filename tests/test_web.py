"""M5 web-server tests. Everything runs offline with fake adapters.

The server is exercised exactly the way a browser would be: create a session
over HTTP, stream it over a WebSocket, drive pause/resume/stop and
interventions through actions, and download the export.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from conftest import cfg, make_adapters
from fastapi.testclient import TestClient

from council.core.errors import CouncilError, ErrorKind
from council.web import sessionhub as _sessionhub_module
from council.web.server import create_app


@pytest.fixture(autouse=True)
def _fast_select_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake Judge 转人工选主案：测试里 1 秒兜底自动取首个候选，避免挂等。"""
    monkeypatch.setattr(_sessionhub_module, "_SELECT_TIMEOUT_S", 1.0)


def _client(tmp_path: Path, config: Any = None, **kwargs: Any) -> TestClient:
    config = config if config is not None else cfg(participants=3)
    app = create_app(
        data_dir=tmp_path,
        config=config,
        adapter_builder=lambda c: make_adapters(c, **kwargs),
    )
    return TestClient(app)


def _start(client: TestClient, question: str = "如何把日活做到十万？") -> str:
    response = client.post("/api/sessions", json={"question": question})
    assert response.status_code == 202, response.text
    return str(response.json()["session_id"])


def _wait(
    client: TestClient, session_id: str, want: str, timeout_s: float = 15.0
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("status") == want:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {want} 超时，最后状态：{last}")


def test_happy_path_create_stream_export(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        meta = client.get("/api/meta").json()
        assert meta["participants"]
        assert meta["judge"]

        session_id = _start(client, "一个值得讨论的问题")
        detail = _wait(client, session_id, "completed")
        assert detail["proposal"] is not None
        assert detail["calls_done"] > 0

        events = client.get(f"/api/sessions/{session_id}/events").json()["events"]
        kinds = {event["type"] for event in events}
        assert "SessionCreated" in kinds and "PhaseCompleted" in kinds

        listing = client.get("/api/sessions").json()["sessions"]
        assert any(row["session_id"] == session_id for row in listing)
        row = next(row for row in listing if row["session_id"] == session_id)
        assert row["status"] == "completed"

        exported = client.get(f"/api/sessions/{session_id}/export").content.decode()
        assert "# AI Council 会谈纪要" in exported
        assert "一个值得讨论的问题" in exported
        raw = client.get(f"/api/sessions/{session_id}/export", params={"raw": True})
        assert raw.status_code == 200

        assert client.get("/").status_code == 200  # static index


def test_pause_then_resume_via_actions(tmp_path: Path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "pause"
    failures = {"n3": [CouncilError(ErrorKind.AUTH, "密钥失效一次", node_id="n3")]}
    with _client(tmp_path, config=config, failures=failures) as client:
        session_id = _start(client)
        _wait(client, session_id, "paused")

        action = client.post(f"/api/sessions/{session_id}/actions", json={"action": "resume"})
        assert action.status_code == 200, action.text
        detail = _wait(client, session_id, "completed")
        assert detail["proposal"] is not None


def test_stop_from_paused_state_over_websocket(tmp_path: Path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "pause"
    failures = {"n3": [CouncilError(ErrorKind.AUTH, "密钥失效一次", node_id="n3")]}
    with _client(tmp_path, config=config, failures=failures) as client:
        session_id = _start(client)
        _wait(client, session_id, "paused")

        with client.websocket_connect(f"/api/sessions/{session_id}/ws") as ws:
            ws.send_json({"since": 0})
            ws.send_json({"kind": "action", "action": "stop"})
            outcome = _drain_until(ws, "session_done")
            assert outcome["status"] == "stopped"

        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["status"] == "stopped"


def test_resume_streams_live_events_over_websocket(tmp_path: Path) -> None:
    config = cfg(participants=3)
    config.failure.policy = "pause"
    failures = {"n3": [CouncilError(ErrorKind.AUTH, "密钥失效一次", node_id="n3")]}
    with _client(tmp_path, config=config, failures=failures) as client:
        session_id = _start(client)
        _wait(client, session_id, "paused")

        with client.websocket_connect(f"/api/sessions/{session_id}/ws") as ws:
            ws.send_json({"since": 0})
            ws.send_json({"kind": "action", "action": "resume"})
            kinds: set[str] = set()
            outcome: dict[str, Any] | None = None
            for _ in range(2000):
                frame = ws.receive_json()
                kind = frame.get("kind")
                if kind == "event":
                    kinds.add(frame["event"]["type"])
                elif kind == "replay":
                    for event in frame["events"]:
                        kinds.add(event["type"])
                elif kind == "session_done":
                    outcome = frame
                    break
        assert outcome is not None and outcome["status"] == "completed", outcome
        assert "PhaseCompleted" in kinds
        assert "SessionCreated" in kinds

        detail = _wait(client, session_id, "completed")
        assert detail["status"] == "completed"


def test_ask_user_auto_defaults_when_unattended(tmp_path: Path) -> None:
    """Nobody listening on ask_user → conservative drop, session still finishes."""
    config = cfg(participants=3)
    config.failure.policy = "ask_user"
    failures = {"n1": [CouncilError(ErrorKind.AUTH, "密钥失效", node_id="n1")]}
    with _client(tmp_path, config=config, failures=failures) as client:
        session_id = _start(client)
        detail = _wait(client, session_id, "completed")
        assert detail["proposal"] is not None


def test_attachment_validation_is_fail_fast(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        started = client.post(
            "/api/sessions",
            json={
                "question": "结合背景材料作答",
                "files": [{"name": "背景.txt", "content": "这是一份背景材料。\n企业现状良好。"}],
            },
        )
        assert started.status_code == 202, started.text
        session_id = started.json()["session_id"]
        detail = _wait(client, session_id, "completed")
        assert detail["proposal"] is not None
        events = client.get(f"/api/sessions/{session_id}/events").json()["events"]
        created = next(e for e in events if e["type"] == "SessionCreated")
        assert created["payload"]["attachments"] == ["背景.txt"]

    with _client(tmp_path) as client:
        before = len(client.get("/api/sessions").json()["sessions"])
        bad = client.post(
            "/api/sessions",
            json={
                "question": "坏附件",
                "files": [{"name": "伪装.json", "content": "这不是 JSON{{{ 内容"}],
            },
        )
        assert bad.status_code == 400, bad.text
        assert "伪装" in bad.json()["detail"]
        after = client.get("/api/sessions").json()["sessions"]
        assert len(after) == before  # no ghost session for the rejected upload


def test_resume_existing_finished_session_is_idempotent(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        session_id = _start(client)
        _wait(client, session_id, "completed")

        resumed = client.post(f"/api/sessions/{session_id}/resume", json={"question": ""})
        assert resumed.status_code == 202, resumed.text
        _wait(client, session_id, "completed")
        listing = client.get("/api/sessions").json()["sessions"]
        assert len(listing) == 1


def test_unknown_session_and_bad_action_are_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/sessions/does-not-exist").status_code == 404
        assert client.get("/api/sessions/does-not-exist/export").status_code == 404

        session_id = _start(client)
        _wait(client, session_id, "completed")
        # The session is reported completed, but the engine may still be
        # tearing down for a moment; stop only settles on 409 once nothing is
        # live. Poll briefly instead of asserting on the first response.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            stopped = client.post(f"/api/sessions/{session_id}/actions", json={"action": "stop"})
            if stopped.status_code == 409:
                break
            time.sleep(0.05)
        assert stopped.status_code == 409  # nothing live to stop


def _drain_until(ws: Any, want_kind: str) -> dict[str, Any]:
    for _ in range(2000):
        frame = ws.receive_json()
        if frame.get("kind") == want_kind:
            return frame
    raise AssertionError(f"没有等到 {want_kind} 消息")

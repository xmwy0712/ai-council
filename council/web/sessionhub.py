"""In-process session hub for the web UI.

One :class:`SessionHub` owns exactly one running :class:`CouncilEngine`. It
broadcasts engine events to live WebSocket subscribers and turns the engine's
intervention asks (``ask_user`` policy, selection, human decisions) into
answerable tickets: the browser answers over the socket, the engine coroutine
waits on the ticket's future and resumes.

Design rules:

- The store is the source of truth. Live events are only a fast lane; a new
  subscriber replays stored events with ``since=`` and deduplicates by seq.
- If nobody is listening when an ask arrives (or the last listener leaves while
  one is pending) the hub answers with a conservative default instead of
  hanging the session forever: drop the failing node, pick the first
  candidate, or record an unattended continuation note.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters import build_adapters
from ..core.attachments import AttachmentRejected, load_attachments
from ..core.config import Config
from ..core.contracts import Adapter
from ..core.events import ConfigChanged, Event
from ..core.orchestrator import (
    CouncilEngine,
    DecisionRequest,
    EngineStopped,
    InterventionAction,
    InterventionDecision,
    InterventionRequest,
    SelectionRequest,
)
from ..core.state import SessionState
from ..core.store import EventStore, SessionMeta

__all__ = [
    "HubError",
    "SessionHub",
    "SessionManager",
    "UploadRejected",
    "event_json",
    "state_digest",
]

AdapterBuilder = Callable[[Config], dict[str, Adapter]]

# How long a frozen session waits in-process before unwinding to a persisted
# breakpoint. 12h is generous for a live server; the browser can resume at any
# time before that.
_RESUME_TIMEOUT_S = 43200.0


class HubError(RuntimeError):
    """Session is missing, already finished, or cannot accept that action."""


class UploadRejected(ValueError):
    """An uploaded file failed the attachment gate (suffix/content sniffing)."""


def event_json(event: Event) -> dict[str, Any]:
    """Wire form of an event: one dict a browser can render directly."""
    payload = event.payload.model_dump(mode="json")
    return {
        "seq": event.seq,
        "ts": event.ts.isoformat(),
        "type": event.type.value,
        "payload": payload,
    }


def _proposal_snapshot(state: SessionState) -> dict[str, Any] | None:
    proposal = state.current_proposal
    if proposal is None:
        return None
    return {key: value for key, value in proposal.items()}


def state_digest(state: SessionState, meta: SessionMeta | None) -> dict[str, Any]:
    """Small JSON snapshot of a live or replayed session state."""
    usage = state.usage
    done = sum(1 for call in state.calls.values() if getattr(call, "status", "") == "completed")
    digest: dict[str, Any] = {
        "session_id": state.session_id,
        "question": state.question,
        "status": state.status.value,
        "phase": state.phase,
        "round": state.round_index + 1,
        "version": state.version,
        "selected": state.selected,
        "proposal": _proposal_snapshot(state),
        "scores": list(state.scores or []),
        "participants": [node.id for node in state.config.participants],
        "judge": state.config.judge_node.id if state.config.judge_node else None,
        "degraded": sorted(state.degraded),
        "calls_done": done,
        "rounds": len(state.rounds),
        "usage": {
            "input": getattr(usage, "input_tokens", 0),
            "output": getattr(usage, "output_tokens", 0),
        },
        "converged": state.latest_convergence() is not None,
    }
    if meta is not None:
        digest["created_at"] = meta.created_at
        digest["updated_at"] = meta.updated_at
    return digest


@dataclass
class _AskTicket:
    kind: str
    future: asyncio.Future[dict[str, Any]]
    default: dict[str, Any]


def _default_answer(kind: str, ask: dict[str, Any]) -> dict[str, Any]:
    if kind == "failure":
        available = [InterventionAction(value) for value in ask.get("available", [])]
        if InterventionAction.DROP_NODE in available:
            return {"action": InterventionAction.DROP_NODE.value}
        if InterventionAction.RETRY in available:
            return {"action": InterventionAction.RETRY.value}
        return {"action": InterventionAction.ABORT.value}
    if kind == "select":
        candidates = list(ask.get("candidates", []))
        return {"node_id": candidates[0]} if candidates else {"node_id": ""}
    if kind == "decision":
        return {"text": "（无人值守，自动继续）"}
    return {}


class _HubHandler:
    """Routes engine interventions into the hub's ticket system."""

    def __init__(self, hub: SessionHub) -> None:
        self._hub = hub

    async def on_failure(self, req: InterventionRequest) -> InterventionDecision:
        answer = await self._hub.ask(
            "failure",
            {
                "session_id": req.session_id,
                "node_id": req.node_id,
                "phase": req.phase,
                "round": req.round,
                "kind": req.kind,
                "message": req.message,
                "retryable": req.retryable,
                "available": [action.value for action in req.available],
            },
        )
        action = InterventionAction(answer.get("action") or InterventionAction.ABORT.value)
        return InterventionDecision(
            action=action,
            model=answer.get("model"),
            adapter=answer.get("adapter"),
        )

    async def on_select(self, req: SelectionRequest) -> str:
        answer = await self._hub.ask(
            "select",
            {
                "session_id": req.session_id,
                "candidates": list(req.candidates),
                "reason": req.reason,
            },
        )
        return str(answer.get("node_id") or "")

    async def on_decision(self, req: DecisionRequest) -> str:
        answer = await self._hub.ask(
            "decision",
            {
                "session_id": req.session_id,
                "phase": req.phase,
                "round": req.round,
                "question": req.question,
            },
        )
        return str(answer.get("text") or "")


class SessionHub:
    """One running session: engine, subscribers and pending asks."""

    def __init__(
        self,
        *,
        session_id: str,
        store: EventStore,
        config: Config,
        adapter_builder: AdapterBuilder,
        resume_timeout_s: float = _RESUME_TIMEOUT_S,
    ) -> None:
        self.session_id = session_id
        self._store = store
        self._config = config
        self._adapter_builder = adapter_builder
        self._resume_timeout_s = resume_timeout_s

        self.engine: CouncilEngine | None = None
        self.state: SessionState | None = None
        self.task: asyncio.Task[None] | None = None
        self.error: str | None = None
        self._staged_files: list[str] = []

        self._adapters: dict[str, Adapter] = {}
        self._subscribers: list[asyncio.Queue[dict[str, Any] | None]] = []
        self._pending: dict[str, _AskTicket] = {}

    # ------------------------------------------------------------- streaming

    def subscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        if queue not in self._subscribers:
            self._subscribers.append(queue)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)
        if not self._subscribers:
            self._auto_answer_all()

    @property
    def listener_count(self) -> int:
        return len(self._subscribers)

    async def _publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(message)

    async def _on_event(self, event: Event) -> None:
        await self._publish({"kind": "event", "event": event_json(event)})

    # ------------------------------------------------------------ asks / asks

    async def ask(self, kind: str, ask: dict[str, Any]) -> dict[str, Any]:
        """Publish an intervention ask and wait for a browser answer."""
        ask_id = secrets.token_hex(6)
        ticket = _AskTicket(
            kind=kind,
            future=asyncio.get_running_loop().create_future(),
            default=_default_answer(kind, ask),
        )
        self._pending[ask_id] = ticket
        await self._publish({"kind": "ask", "ask_id": ask_id, "type": kind, "ask": ask})
        if not self._subscribers:
            self._auto_answer(ask_id)
        try:
            return await asyncio.shield(ticket.future)
        finally:
            self._pending.pop(ask_id, None)

    def answer(self, ask_id: str, answer: dict[str, Any]) -> bool:
        ticket = self._pending.get(ask_id)
        if ticket is None or ticket.future.done():
            return False
        if ticket.kind == "failure" and not answer.get("action"):
            return False
        if ticket.kind == "select" and not answer.get("node_id"):
            return False
        ticket.future.set_result(answer)
        return True

    def _auto_answer(self, ask_id: str) -> None:
        ticket = self._pending.get(ask_id)
        if ticket is None or ticket.future.done():
            return
        ticket.future.set_result(ticket.default)

    def _auto_answer_all(self) -> None:
        for ask_id in list(self._pending):
            self._auto_answer(ask_id)

    # --------------------------------------------------------------- running

    async def start(
        self, question: str, files: Sequence[str] | None = None
    ) -> asyncio.Task[None]:
        if self.task is not None and not self.task.done():
            raise HubError(f"会话 {self.session_id} 正在运行中")
        self._staged_files = list(files or [])
        self._adapters = self._adapter_builder(self._config)
        self.engine = CouncilEngine(
            store=self._store,
            config=self._config,
            adapters=self._adapters,
            handler=_HubHandler(self),
            sink=self._on_event,
            adapter_builder=self._adapter_builder,
            drift_handler=self._on_drift,
            resume_timeout_s=self._resume_timeout_s,
        )
        self.task = asyncio.create_task(self._run(question, files))
        return self.task

    async def _on_drift(self, drift: ConfigChanged) -> str:
        await self._publish(
            {
                "kind": "drift",
                "diff": list(drift.diff or []),
                "answer": "old",
            }
        )
        return "old"

    async def _run(
        self, question: str, files: Sequence[str] | None
    ) -> None:
        engine = self.engine
        assert engine is not None
        message: dict[str, Any] = {
            "kind": "session_done",
            "status": "finished",
            "session_id": self.session_id,
        }
        try:
            self.state = await engine.run(self.session_id, question, files=files)
            message["status"] = self.state.status.value
        except EngineStopped as err:
            message["status"] = "stopped"
            message["reason"] = str(err)
        except asyncio.CancelledError:
            message["status"] = "cancelled"
            await self._publish(message)
            raise
        except Exception as err:  # noqa: BLE001 - surface to the UI, keep serving
            self.error = f"{type(err).__name__}: {err}"
            message["status"] = "error"
            message["reason"] = self.error
        # run() has returned: the store already carries the terminal status and
        # no engine work remains. Drop the reference NOW so has_live_engine()
        # turns false immediately -- the awaits below (publish, file cleanup,
        # adapter close) must not keep a finished session looking live, or a
        # racing resume/stop on a completed session gets 409/200 wrongly.
        self.engine = None
        # Keep the cheap status column honest even for abnormal endings.
        try:
            await self._store.set_status(self.session_id, message["status"])
        except Exception:  # noqa: BLE001 - best-effort bookkeeping
            pass
        await self._publish(message)
        await self._drop_staged_files()
        self._auto_answer_all()
        for adapter in self._adapters.values():
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    async def _drop_staged_files(self) -> None:
        for path in self._staged_files:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        self._staged_files = []

    def pause(self) -> None:
        if self.engine is None or self._is_done():
            raise HubError(f"会话 {self.session_id} 没有可暂停的运行")
        self.engine.pause()

    def resume(self) -> None:
        if self.engine is None or self._is_done():
            raise HubError(f"会话 {self.session_id} 没有可恢复的运行")
        self.engine.resume()

    def stop(self) -> None:
        if self.engine is None or self._is_done():
            raise HubError(f"会话 {self.session_id} 没有可终止的运行")
        self.engine.stop()

    def set_policy(self, policy: str) -> None:
        if self.engine is None:
            raise HubError(f"会话 {self.session_id} 未在运行")
        from ..core.config import FailurePolicy

        self.engine.set_failure_policy(FailurePolicy(policy))

    def _is_done(self) -> bool:
        return self.task is not None and self.task.done()

    def has_live_engine(self) -> bool:
        return self.engine is not None and not self._is_done()


class SessionManager:
    """Holds every in-flight session hub for one server instance."""

    def __init__(
        self,
        *,
        store: EventStore,
        config: Config,
        data_dir: Path,
        adapter_builder: AdapterBuilder = build_adapters,
        resume_timeout_s: float = _RESUME_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._config = config
        self._data_dir = data_dir
        self._adapter_builder = adapter_builder
        self._resume_timeout_s = resume_timeout_s
        self._hubs: dict[str, SessionHub] = {}
        self._upload_dir = data_dir / "uploads"
        self._upload_dir.mkdir(parents=True, exist_ok=True)

    @property
    def config(self) -> Config:
        return self._config

    @property
    def store(self) -> EventStore:
        return self._store

    def hub(self, session_id: str) -> SessionHub | None:
        return self._hubs.get(session_id)

    async def start_new(
        self,
        session_id: str,
        question: str,
        uploads: Sequence[tuple[str, str]] = (),
    ) -> SessionHub:
        if self._hubs.get(session_id) is not None:
            raise HubError(f"会话 {session_id} 已在运行")
        hub = SessionHub(
            session_id=session_id,
            store=self._store,
            config=self._config,
            adapter_builder=self._adapter_builder,
            resume_timeout_s=self._resume_timeout_s,
        )
        paths = await self._stage_uploads(session_id, uploads)
        self._hubs[session_id] = hub
        try:
            self._reject_invalid_uploads(paths)
            await hub.start(question, files=paths)
        except Exception:
            self._hubs.pop(session_id, None)
            await self._drop_paths(paths)
            raise
        return hub

    async def resume_existing(
        self,
        session_id: str,
        uploads: Sequence[tuple[str, str]] = (),
    ) -> SessionHub:
        meta = await self._store.get_session(session_id)
        if meta is None:
            raise HubError(f"找不到会话 {session_id}")
        if self._hubs.get(session_id) is not None:
            hub = self._hubs[session_id]
            if hub.has_live_engine():
                raise HubError(f"会话 {session_id} 正在运行中")
            self._hubs.pop(session_id, None)
        hub = SessionHub(
            session_id=session_id,
            store=self._store,
            config=meta.config,
            adapter_builder=self._adapter_builder,
            resume_timeout_s=self._resume_timeout_s,
        )
        paths = await self._stage_uploads(session_id, uploads)
        self._hubs[session_id] = hub
        try:
            self._reject_invalid_uploads(paths)
            await hub.start("", files=paths)
        except Exception:
            self._hubs.pop(session_id, None)
            await self._drop_paths(paths)
            raise
        return hub

    def _reject_invalid_uploads(self, paths: Sequence[str]) -> None:
        """Fail fast: run the engine's own attachment gate before starting.

        Without this a bad upload would surface minutes later as a dead session
        with no event-log row to show for it.
        """
        if not paths:
            return
        try:
            load_attachments(list(paths), self._config.attachments)
        except AttachmentRejected as err:
            raise UploadRejected(str(err)) from err

    async def _drop_paths(self, paths: Sequence[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    async def drop(self, session_id: str) -> None:
        hub = self._hubs.pop(session_id, None)
        if hub is None:
            return
        if hub.has_live_engine():
            hub.stop()
        if hub.task is not None and not hub.task.done():
            hub.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hub.task

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._store.list_sessions(limit=limit)
        out: list[dict[str, Any]] = []
        for row in rows:
            live = self._hubs.get(row.session_id)
            status = "running" if live is not None and live.has_live_engine() else row.status
            out.append(
                {
                    "session_id": row.session_id,
                    "question": row.question,
                    "status": status,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
            )
        return out

    async def digest(self, session_id: str) -> dict[str, Any]:
        meta = await self._store.get_session(session_id)
        hub = self._hubs.get(session_id)
        if meta is None:
            # Race window: the engine task has not yet persisted the session
            # row. If the hub exists, report it as starting rather than 404.
            if hub is not None:
                digest = {
                    "session_id": session_id,
                    "question": hub.engine.state.question if hub.engine is not None else "",
                    "status": "starting",
                    "phase": "",
                    "round": 0,
                    "version": 1,
                    "selected": None,
                    "proposal": None,
                    "scores": [],
                    "participants": [node.id for node in hub._config.participants],
                    "judge": (
                        hub._config.judge_node.id if hub._config.judge_node else None
                    ),
                    "degraded": [],
                    "calls_done": 0,
                    "rounds": 0,
                    "usage": {"input": 0, "output": 0},
                    "converged": False,
                    "created_at": "",
                    "updated_at": "",
                }
                if hub.engine is not None:
                    digest["phase"] = hub.engine.state.phase
                return digest
            raise HubError(f"找不到会话 {session_id}")
        if hub is not None and hub.engine is not None and hub.state is not None:
            digest = state_digest(hub.state, meta)
        else:
            from ..core.state import replay

            state = replay(session_id, meta.config, await self._store.events(session_id))
            digest = state_digest(state, meta)
        # Abnormal endings have no live SessionState; surface them honestly.
        if hub is not None and hub.error:
            digest["status"] = "error"
            digest["error"] = hub.error
        elif hub is not None and hub.state is None and meta.status in (
            "stopped",
            "error",
            "cancelled",
        ):
            digest["status"] = meta.status
        return digest

    async def events(self, session_id: str, *, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        stored = await self._store.events(session_id)
        selected = [event_json(event) for event in stored if event.seq > since]
        if limit > 0:
            selected = selected[-limit:]
        return selected

    # ----------------------------------------------------------------- uploads

    async def _stage_uploads(
        self, session_id: str, uploads: Sequence[tuple[str, str]]
    ) -> list[str]:
        """Write upload bytes under the app data dir so the engine can read them.

        One sub-directory per session keeps the original file name intact (the
        engine records attachment *names* in the log) and avoids collisions
        between concurrent sessions. Validation failures surface as HTTP 400 on
        the create call.
        """
        if not uploads:
            return []
        folder = self._upload_dir / session_id
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for index, (name, content) in enumerate(uploads):
            safe = Path(name).name
            # One sub-directory per upload keeps the *original* file name as the
            # path's basename (the engine logs attachment names) while making
            # same-name uploads impossible to collide.
            target = folder / str(index) / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            paths.append(str(target))
        return paths

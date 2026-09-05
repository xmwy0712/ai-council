"""SQLite (WAL) append-only event store.

Two properties give us crash safety: events are only ever appended inside a
single ``BEGIN IMMEDIATE`` transaction, and the database runs in WAL mode with
``synchronous=FULL``. There is no read-modify-write of a session blob, so a kill
at any instant leaves a log that replays cleanly.

Concurrency model: the engine dispatches nodes in parallel, so several
coroutines append at once. SQLite allows one writer, and the driver runs each
statement in a worker thread, so every write goes through a single ``asyncio``
lock — the log stays totally ordered and no connection is ever shared across
threads without that lock held. A plain ``threading.Lock`` guards the raw
connection as well, so ``close()`` can never race an in-flight statement from a
cancelled task (closing a live connection mid-``execute`` is a hard crash).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from .config import Config, fingerprint
from .events import EVENT_PAYLOADS, Event, EventType

__all__ = ["EventStore", "SessionMeta", "StoreError"]

_R = TypeVar("_R")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'running',
    question           TEXT NOT NULL DEFAULT '',
    config             TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
"""


class StoreError(RuntimeError):
    """Raised when the event log cannot be read or written."""


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    created_at: str
    updated_at: str
    status: str
    question: str
    config: Config
    config_fingerprint: str


def _meta_from_row(row: sqlite3.Row) -> SessionMeta:
    return SessionMeta(
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        question=row["question"],
        config=Config.model_validate_json(row["config"]),
        config_fingerprint=row["config_fingerprint"],
    )


class EventStore:
    """Async facade over a single SQLite connection."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()
        # Guards the raw connection across worker threads. `close()` takes it
        # too, so no statement can be mid-flight while the connection dies.
        self._conn_lock = threading.Lock()

    # ------------------------------------------------------- thread barrier

    def _call(self, fn: Callable[..., _R], *args: Any) -> _R:
        """Run a sync DB routine on a worker thread, serialised by conn lock."""
        with self._conn_lock:
            return fn(*args)

    async def _sync(self, fn: Callable[..., _R], *args: Any) -> _R:
        return await asyncio.to_thread(self._call, fn, *args)

    # ------------------------------------------------------------- lifecycle

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            # isolation_level=None: autocommit, so our explicit BEGIN IMMEDIATE
            # is the only transaction boundary.
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    async def open(self) -> EventStore:
        self._conn = await self._sync(self._connect)
        return self

    async def close(self) -> None:
        def shutdown() -> None:
            with self._conn_lock:
                conn = self._conn
                self._conn = None
                if conn is not None:
                    conn.close()

        await asyncio.to_thread(shutdown)

    async def __aenter__(self) -> EventStore:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("事件存储尚未打开，请先 await store.open()")
        return self._conn

    # -------------------------------------------------------------- sessions

    def _create_session(self, session_id: str, config: Config, question: str, now: str) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, created_at, updated_at, status, question, config, config_fingerprint)"
            " VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (session_id, now, now, question, config.model_dump_json(), fingerprint(config)),
        )

    async def create_session(
        self,
        session_id: str,
        config: Config,
        *,
        question: str = "",
        created_at: datetime | None = None,
    ) -> None:
        now = (created_at or datetime.now()).isoformat()
        await self._sync(self._create_session, session_id, config, question, now)

    async def get_session(self, session_id: str) -> SessionMeta | None:
        def query() -> SessionMeta | None:
            row = (
                self._db()
                .execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
                .fetchone()
            )
            return _meta_from_row(row) if row is not None else None

        return await self._sync(query)

    async def list_sessions(self, limit: int = 50) -> list[SessionMeta]:
        def query() -> list[SessionMeta]:
            rows = (
                self._db()
                .execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,))
                .fetchall()
            )
            return [_meta_from_row(row) for row in rows]

        return await self._sync(query)

    async def set_status(self, session_id: str, status: str) -> None:
        def update() -> None:
            self._db().execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, datetime.now().isoformat(), session_id),
            )

        await self._sync(update)

    async def update_session_config(self, session_id: str, config: Config) -> None:
        """Replace the stored config (used when the user edits it mid-session)."""

        def update() -> None:
            self._db().execute(
                "UPDATE sessions SET config = ?, config_fingerprint = ?, updated_at = ?"
                " WHERE session_id = ?",
                (
                    config.model_dump_json(),
                    fingerprint(config),
                    datetime.now().isoformat(),
                    session_id,
                ),
            )

        await self._sync(update)

    # ---------------------------------------------------------------- events

    def _append_sync(self, session_id: str, events: Sequence[Event]) -> list[int]:
        conn = self._db()
        seqs: list[int] = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                cur = conn.execute(
                    "INSERT INTO events (session_id, ts, type, payload) VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        event.ts.isoformat(),
                        event.type.value,
                        event.payload.model_dump_json(),
                    ),
                )
                seqs.append(int(cur.lastrowid or 0))
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (datetime.now().isoformat(), session_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return seqs

    async def append(
        self,
        session_id: str,
        items: Sequence[tuple[EventType, Any]],
        *,
        persist: bool = True,
    ) -> list[Event]:
        """Append events in one transaction and return them with ``seq`` filled.

        ``persist=False`` is used for high-volume stream chunks: they still flow
        to live subscribers but do not bloat the log.
        """
        if not items:
            return []
        events: list[Event] = []
        for type_, payload in items:
            expected = EVENT_PAYLOADS[type_]
            if not isinstance(payload, expected):
                raise StoreError(
                    f"事件 {type_.value} 的载荷必须是 {expected.__name__}，"
                    f"收到 {type(payload).__name__}"
                )
            events.append(Event(seq=0, session_id=session_id, type=type_, payload=payload))
        if not persist:
            return events

        async with self._write_lock:
            try:
                seqs = await self._sync(self._append_sync, session_id, events)
            except sqlite3.Error as err:
                raise StoreError(f"写入事件失败：{err}") from err
        return [
            event.model_copy(update={"seq": seq}) for event, seq in zip(events, seqs, strict=True)
        ]

    async def events(self, session_id: str) -> list[Event]:
        def query() -> list[Event]:
            rows = (
                self._db()
                .execute(
                    "SELECT seq, ts, type, payload FROM events WHERE session_id = ? ORDER BY seq",
                    (session_id,),
                )
                .fetchall()
            )
            return [_row_to_event(session_id, row) for row in rows]

        return await self._sync(query)

    async def count_events(self, session_id: str) -> int:
        def query() -> int:
            row = (
                self._db()
                .execute("SELECT COUNT(*) AS n FROM events WHERE session_id = ?", (session_id,))
                .fetchone()
            )
            return int(row["n"]) if row is not None else 0

        return await self._sync(query)


def _row_to_event(session_id: str, row: sqlite3.Row) -> Event:
    type_ = EventType(row["type"])
    payload = EVENT_PAYLOADS[type_].model_validate_json(row["payload"])
    return Event(
        seq=int(row["seq"]),
        ts=datetime.fromisoformat(row["ts"]),
        session_id=session_id,
        type=type_,
        payload=payload,
    )

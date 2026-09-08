"""The council engine.

What it guarantees, and how each guarantee maps to a defect in the prototype
this replaces:

* Phase transitions come from structured enum fields only — never from a regex
  over prose, and never with a silent fallback to ``NEED_REVISION``.
* Judge is a separate node that only selects and rules; it never proposes,
  reviews, debates or rewrites.
* Everything parallel is parallel: proposals and reviews fan out; only debate is
  round-robin, because round-robin is by definition sequential.
* Every call is bracketed by ``CallIssued`` / ``CallCompleted`` with an
  idempotency key, so ``council resume`` replays finished work and re-sends only
  in-flight or failed calls.
* A node that keeps failing is degraded and paused; below ``min_quorum`` the
  session freezes instead of letting one model talk to itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..adapters.base import (
    resolve_max_output_tokens,
    resolve_max_retries,
    resolve_temperature,
    resolve_thinking,
    resolve_timeout,
)
from .attachments import Attachment, load_attachments, render_zones
from .breaker import CircuitBreaker
from .config import (
    Config,
    ConfigError,
    FailurePolicy,
    NodeSection,
    diff_configs,
    fingerprint,
)
from .contracts import (
    Adapter,
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChunkType,
    ResponseFormat,
    Role,
    Usage,
)
from .errors import ContractError, CouncilError, ErrorKind
from .events import (
    CallChunk,
    CallCompleted,
    CallFailed,
    CallIssued,
    CallKey,
    ConfigChanged,
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
from .machine import Phase
from .prompts import PromptSet, wrap_data
from .schema import (
    DebateOut,
    JudgeSelectionOut,
    ProposalOut,
    ReviewOut,
    RevisionOut,
    VerdictOut,
    contract_block,
    parse_structured,
)
from .single_plan import SINGLE_PLAN_REPAIR_HINT, find_multi_plan_violations
from .state import SessionState, SessionStatus, replay
from .store import EventStore, SessionMeta

__all__ = [
    "CouncilEngine",
    "DecisionRequest",
    "EngineStopped",
    "InterventionAction",
    "InterventionDecision",
    "InterventionHandler",
    "InterventionRequest",
    "NodeUnavailable",
    "PhaseBlocked",
    "SelectionRequest",
    "SessionPaused",
]

T = TypeVar("T", bound=BaseModel)

Sink = Callable[[Event], Awaitable[None]]
DriftHandler = Callable[[ConfigChanged], Awaitable[str]]
MessageBuilder = Callable[[str], list[ChatMessage]]


class EngineStopped(RuntimeError):
    """The user terminated the session. The breakpoint stays resumable."""


class PhaseBlocked(RuntimeError):
    """A phase cannot proceed (no quorum, no reviewer, awaiting a human).

    ``retryable`` marks blocks caused by transport-shaped failures: those may
    heal after a probe, so the engine retries the phase instead of unwinding.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SessionPaused(RuntimeError):
    """The session froze itself. The breakpoint is on disk; `council resume`
    picks it up later instead of the process hanging forever."""


class NodeUnavailable(RuntimeError):
    """A node was dropped after its failure could not be resolved."""

    def __init__(self, node_id: str, reason: str, *, retryable: bool = False) -> None:
        super().__init__(f"{node_id}: {reason}")
        self.node_id = node_id
        self.reason = reason
        # True when the underlying error was transport-shaped (network /
        # rate limit / timeout): the block may heal on its own, so the phase
        # may probe and retry instead of unwinding.
        self.retryable = retryable


class InterventionAction(StrEnum):
    RETRY = "retry"
    WAIT = "wait"
    SWITCH_MODEL = "switch_model"
    SWITCH_ADAPTER = "switch_adapter"
    DROP_NODE = "drop_node"
    ABORT = "abort"


@dataclass(frozen=True)
class InterventionDecision:
    action: InterventionAction
    model: str | None = None
    adapter: str | None = None


@dataclass(frozen=True)
class InterventionRequest:
    session_id: str
    node_id: str
    phase: str
    round: int
    kind: str
    message: str
    retryable: bool
    available: tuple[InterventionAction, ...]


@dataclass(frozen=True)
class SelectionRequest:
    session_id: str
    candidates: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DecisionRequest:
    session_id: str
    phase: str
    round: int
    question: str


class InterventionHandler(Protocol):
    """How the engine asks a human for help, whatever the surface is.

    The CLI answers on stdin, the web UI answers over a WebSocket round-trip.
    The engine cannot tell them apart, which is what keeps ``core`` free of any
    UI dependency.
    """

    async def on_failure(self, req: InterventionRequest) -> InterventionDecision: ...

    async def on_select(self, req: SelectionRequest) -> str: ...

    async def on_decision(self, req: DecisionRequest) -> str: ...


class CouncilEngine:
    def __init__(
        self,
        *,
        store: EventStore,
        config: Config,
        adapters: Mapping[str, Adapter],
        handler: InterventionHandler | None = None,
        sink: Sink | None = None,
        prompts: PromptSet | None = None,
        adapter_builder: Callable[[Config], Mapping[str, Adapter]] | None = None,
        drift_handler: DriftHandler | None = None,
        resume_timeout_s: float = 0.0,
    ) -> None:
        self.store = store
        self.config = config
        self.adapters: dict[str, Adapter] = dict(adapters)
        self.handler = handler
        self.sink = sink
        self.prompts = prompts or PromptSet(config.council.language)
        self._adapter_builder = adapter_builder
        self._drift_handler = drift_handler
        # How long a frozen session waits for an in-process resume() before it
        # unwinds. 0 = head-less CLI: freeze, persist, exit. A server passes a
        # generous value and resumes through its API.
        self._resume_timeout_s = resume_timeout_s

        self.session_id = ""
        self.state = SessionState("", config)
        self.drift: ConfigChanged | None = None

        self._pause = asyncio.Event()
        self._pause.set()
        self._stopped = False
        self._breaker = CircuitBreaker(config.failure.circuit_threshold)
        self._dropped: set[str] = set()
        self._dropped_retryable: dict[str, bool] = {}
        self._failure_policy = config.failure.policy
        self._system_cache: dict[str, ChatMessage] = {}
        self._started = time.monotonic()

    # -------------------------------------------------------------- lifecycle

    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def stop(self) -> None:
        self._stopped = True
        self._pause.set()

    def set_failure_policy(self, policy: FailurePolicy) -> None:
        """Switch failure policy mid-flight. Takes effect on the next failure."""
        self._failure_policy = policy

    # -------------------------------------------------------------------- run

    async def run(
        self,
        session_id: str,
        question: str = "",
        files: Sequence[str] | None = None,
    ) -> SessionState:
        self.session_id = session_id
        self._started = time.monotonic()

        meta = await self.store.get_session(session_id)

        self._attachments: list[Attachment] = []
        self._attachment_zone, self._attachments_truncated = "", False
        if files:
            self._attachments = load_attachments(files, self.config.attachments)
            self._attachment_zone, self._attachments_truncated = render_zones(
                self._attachments, self.config.budget
            )

        if meta is None:
            await self.store.create_session(session_id, self.config, question=question)
            self.state = SessionState(session_id, self.config)
            self.state.question = question
            await self._emit(
                EventType.SESSION_CREATED,
                SessionCreated(
                    question=question,
                    config_fingerprint=fingerprint(self.config),
                    language=self.config.council.language,
                    participants=[n.id for n in self.config.participants],
                    judge=self.config.judge.node_id if self.config.judge else None,
                    attachments=[a.name for a in self._attachments],
                ),
            )
        else:
            created = next(
                (
                    e
                    for e in await self.store.events(session_id)
                    if e.type is EventType.SESSION_CREATED
                ),
                None,
            )
            recorded = getattr(created.payload, "attachments", []) if created else []
            if recorded and not self._attachments:
                raise ConfigError(
                    f"会话 {session_id} 带有附件 {('、'.join(recorded))}，"
                    "续跑时必须用 --file 重新提供这些文件"
                )
            self.state = replay(session_id, self.config, await self.store.events(session_id))
            self.state.question = question or meta.question
            await self._reconcile_config(meta)

        while True:
            await self._gate()
            if self.state.status is SessionStatus.COMPLETED:
                break
            phase = self._current_phase()
            if phase is Phase.FINAL:
                await self._phase_final()
                break
            try:
                await self._step(phase)
            except SessionPaused:
                break
        return self.state

    async def _reconcile_config(self, meta: SessionMeta) -> None:
        """Warn when a suspended session is resumed under different rules."""
        current = fingerprint(self.config)
        if meta.config_fingerprint == current:
            return
        self.drift = ConfigChanged(
            before=meta.config_fingerprint,
            after=current,
            diff=diff_configs(meta.config, self.config),
        )
        choice = "old"
        if self._drift_handler is not None:
            choice = await self._drift_handler(self.drift)
        if choice == "new":
            await self.store.update_session_config(self.session_id, self.config)
            await self._emit(EventType.CONFIG_CHANGED, self.drift)
        else:
            self.config = meta.config
            self.state.config = meta.config
            if self._adapter_builder is not None:
                self.adapters = dict(self._adapter_builder(meta.config))

    def _current_phase(self) -> Phase:
        if self.state.status is SessionStatus.STALLED:
            return Phase.FINAL
        return Phase(self.state.phase)

    async def _step(self, phase: Phase) -> None:
        round_ = self._round_for(phase)
        if (phase.value, round_) not in self.state.entered:
            await self._emit(
                EventType.PHASE_STARTED,
                PhaseStarted(phase=phase.value, round=round_, version=self.state.version),
            )
        handlers: dict[Phase, Callable[[int], Awaitable[None]]] = {
            Phase.CONTEXT: self._phase_context,
            Phase.PROPOSAL: self._phase_proposal,
            Phase.SELECT: self._phase_select,
            Phase.REVIEW: self._phase_review,
            Phase.DEBATE: self._phase_debate,
            Phase.VERDICT: self._phase_verdict,
            Phase.REVISION: self._phase_revision,
        }
        try:
            await handlers[phase](round_)
        except PhaseBlocked as blocked:
            if blocked.retryable:
                # Transport-shaped block: probe once after a cooldown and retry
                # the phase in place. A persistent outage still unwinds to
                # `council resume` — the breakpoint is already on disk.
                await self._await_recovery(str(blocked))
            else:
                await self._pause_and_wait(str(blocked))

    def _round_for(self, phase: Phase) -> int:
        if phase in (Phase.REVIEW, Phase.DEBATE, Phase.VERDICT, Phase.REVISION):
            return self._review_round()
        return self.state.round_index

    def _review_round(self) -> int:
        """Review rounds are derived, never stored: a new round starts only once
        the previous one produced a revision."""
        current = self.state.round_index
        if current <= 0:
            return 1
        existing = next((r for r in self.state.rounds if r.index == current), None)
        if existing is not None and existing.revision is not None:
            return current + 1
        return current

    # ----------------------------------------------------------------- phases

    async def _phase_context(self, round_: int) -> None:
        budget = self.config.budget
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.CONTEXT.value,
                round=round_,
                version=1,
                summary={
                    "question": self.state.question,
                    "context_tokens": budget.context_tokens,
                    "reserve_output_tokens": budget.reserve_output_tokens,
                    "attachments": [a.name for a in self._attachments],
                    "truncated": self._attachments_truncated,
                },
            ),
        )

    async def _phase_proposal(self, round_: int) -> None:
        await self._ensure_quorum()
        nodes = self._available_participants()
        results, absent, any_retryable = await self._gather(
            Phase.PROPOSAL, round_, nodes, self._proposal_messages, ProposalOut
        )
        if not results:
            raise PhaseBlocked("所有参与者都未能产出提案", retryable=any_retryable)
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.PROPOSAL.value,
                round=round_,
                version=1,
                summary={
                    "proposals": {k: v.model_dump() for k, v in results.items()},
                    "absentees": absent,
                },
            ),
        )

    async def _phase_select(self, round_: int) -> None:
        await self._ensure_quorum()
        judge = self.config.judge_node
        auto = self.config.judge is not None and self.config.judge.auto_select
        # fake 是零密钥演示适配器：让本地模板假评审替用户定主案没有意义，
        # 视同「未配置 Judge」——按文档承诺转人工审核。
        # fake 是零密钥演示适配器：让本地模板假评审替用户定主案没有意义。
        # 有干预通道（Web / 交互式 CLI）→ 视同未配置，转人工审核；
        # 无人值守（无 handler）→ 保持保守兜底：取首个候选并在事件中注明。
        judge_usable = judge is not None and judge.adapter != "fake"
        if judge is None or not auto:
            selected = await self._human_selection(round_, reason="judge.auto_select = false")
            mode = "human"
        elif not judge_usable and self.handler is None:
            selected = next(iter(self.state.proposals))
            mode = "auto-fallback"
        elif not judge_usable:
            selected = await self._human_selection(
                round_,
                reason="Judge 为 fake 演示适配器（无真实模型），视同未配置，转人工审核",
            )
            mode = "human"
        else:
            try:
                selected = await self._judge_selection(round_, judge)
                mode = "judge"
            except NodeUnavailable as err:
                selected = await self._human_selection(round_, reason=str(err))
                mode = "human"
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.SELECT.value,
                round=round_,
                version=1,
                summary={"selected": selected, "scores": self.state.scores, "mode": mode},
            ),
        )

    async def _judge_selection(self, round_: int, judge: NodeSection) -> str:
        hint = ""
        for _ in range(3):  # 1 attempt + 2 corrections, then escalate to a human
            out = await self._structured_call(
                phase=Phase.SELECT,
                round_=round_,
                node=judge,
                messages=self._select_messages(judge, hint),
                model=JudgeSelectionOut,
            )
            if out.selected_node_id in self.state.proposals:
                self.state.scores = [s.model_dump() for s in out.scores]
                return out.selected_node_id
            hint = (
                "补充要求：selected_node_id 必须是候选节点 id 之一"
                f"（{'、'.join(self.state.proposals)}）；"
                f"你上一次给出的是 {out.selected_node_id!r}。"
            )
        raise NodeUnavailable(judge.id, "Judge 连续给出无效的 selected_node_id")

    async def _human_selection(self, round_: int, *, reason: str) -> str:
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.AWAITING_USER.value, reason="select"),
        )
        if self.handler is None:
            raise PhaseBlocked(f"需要人工选择主案（{reason}），但当前没有干预处理器")
        choice = await self.handler.on_select(
            SelectionRequest(
                session_id=self.session_id,
                candidates=tuple(self.state.proposals),
                reason=reason,
            )
        )
        if choice not in self.state.proposals:
            raise PhaseBlocked(f"人工选择 {choice!r} 不在候选方案中")
        await self._emit(
            EventType.USER_DECISION,
            UserDecision(phase=Phase.SELECT.value, round=round_, decision=choice, note=reason),
        )
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.RUNNING.value, reason="selected"),
        )
        return choice

    async def _phase_review(self, round_: int) -> None:
        author = self.state.selected or ""
        nodes = [n for n in self._available_participants() if n.id != author]
        if not nodes:
            # 单参与者会话（用户在 roster 只选了一个模型）：除作者外没有第二人
            # 可评审。记一笔空评审直接进入辩论/裁定，而不是抛 PhaseBlocked 把
            # 会话冻在「等待恢复」——否则单模型讨论永远走不到终点。
            await self._emit(
                EventType.PHASE_COMPLETED,
                PhaseCompleted(
                    phase=Phase.REVIEW.value,
                    round=round_,
                    version=self.state.version,
                    summary={"reviews": {}, "absentees": [], "skipped": "solo-author"},
                ),
            )
            return
        results, absent, any_retryable = await self._gather(
            Phase.REVIEW, round_, nodes, self._review_messages, ReviewOut
        )
        if not results:
            raise PhaseBlocked("本轮所有评审者均失败", retryable=any_retryable)
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.REVIEW.value,
                round=round_,
                version=self.state.version,
                summary={
                    "reviews": {k: v.model_dump() for k, v in results.items()},
                    "absentees": absent,
                },
            ),
        )

    async def _phase_debate(self, round_: int) -> None:
        rounds = max(self.config.council.debate_rounds, 0)
        author = self.state.selected or ""
        reviewers = [n for n in self._available_participants() if n.id != author]
        collected: dict[str, dict[str, Any]] = {}
        absent: list[str] = []
        transcript: list[str] = []

        if rounds and reviewers:
            for index in range(1, rounds + 1):
                for node in reviewers:
                    try:
                        out = await self._structured_call(
                            phase=Phase.DEBATE,
                            round_=round_,
                            node=node,
                            messages=self._debate_messages(node, index, transcript),
                            model=DebateOut,
                            tag=f"d{index}",
                        )
                    except NodeUnavailable:
                        if node.id not in absent:
                            absent.append(node.id)
                        continue
                    collected[f"{node.id}#{index}"] = out.model_dump()
                    transcript.append(_debate_line(node.id, index, out))

        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.DEBATE.value,
                round=round_,
                version=self.state.version,
                summary={"debates": collected, "absentees": absent},
            ),
        )

    async def _phase_verdict(self, round_: int) -> None:
        judge = self.config.judge_node
        if judge is None:
            raise PhaseBlocked("未配置 Judge，无法做出裁定")
        verdict = await self._structured_call(
            phase=Phase.VERDICT,
            round_=round_,
            node=judge,
            messages=self._verdict_messages(judge),
            model=VerdictOut,
        )
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.VERDICT.value,
                round=round_,
                version=self.state.version,
                summary={"verdict": verdict.model_dump()},
            ),
        )
        await self._record_convergence(round_, verdict)

    async def _record_convergence(self, round_: int, verdict: VerdictOut) -> None:
        must = len(verdict.must_fix)
        should = len(verdict.should_fix)
        disputes = len(verdict.disputes)
        previous = self.state.latest_convergence()
        previous_score = int(previous["must_fix"]) + int(previous["disputes"]) if previous else None
        improved = previous_score is None or (must + disputes) < previous_score
        await self._emit(
            EventType.CONVERGENCE,
            Convergence(
                round=round_,
                version=self.state.version,
                must_fix=must,
                should_fix=should,
                disputes=disputes,
                improved=improved,
            ),
        )
        if self.state.stalled():
            await self._emit(
                EventType.SESSION_STATUS,
                SessionStatusChanged(
                    status=SessionStatus.STALLED.value, reason="连续两轮收敛指标无改善"
                ),
            )
            if self.handler is not None:
                note = await self.handler.on_decision(
                    DecisionRequest(
                        session_id=self.session_id,
                        phase=Phase.VERDICT.value,
                        round=round_,
                        question="连续两轮未改善：必须修改项与分歧项数量都没有下降。请给出你的处置意见。",
                    )
                )
                await self._emit(
                    EventType.USER_DECISION,
                    UserDecision(
                        phase=Phase.VERDICT.value, round=round_, decision=note, note="stalled"
                    ),
                )

    async def _phase_revision(self, round_: int) -> None:
        author_id = self.state.selected
        if author_id is None:
            raise PhaseBlocked("尚未选定主案作者")
        try:
            author = self.config.node(author_id)
        except ConfigError as err:
            raise PhaseBlocked(str(err)) from err

        decision = ""
        if (self.state.current_round().verdict or {}).get("status") == "NEED_USER_DECISION":
            decision = await self._user_verdict_decision(round_)

        revision = await self._structured_call(
            phase=Phase.REVISION,
            round_=round_,
            node=author,
            messages=self._revision_messages(author, decision),
            model=RevisionOut,
        )
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.REVISION.value,
                round=round_,
                version=self.state.version,
                summary={"revision": revision.model_dump(), "version": self.state.version + 1},
            ),
        )

    async def _user_verdict_decision(self, round_: int) -> str:
        verdict = self.state.current_round().verdict or {}
        question = str(verdict.get("question_for_user") or "Judge 认为需要你做决定")
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.AWAITING_USER.value, reason="verdict"),
        )
        if self.handler is None:
            raise PhaseBlocked(f"需要人工裁决（{question}），但当前没有干预处理器")
        note = await self.handler.on_decision(
            DecisionRequest(
                session_id=self.session_id,
                phase=Phase.REVISION.value,
                round=round_,
                question=question,
            )
        )
        await self._emit(
            EventType.USER_DECISION,
            UserDecision(
                phase=Phase.REVISION.value, round=round_, decision=note, note="user_verdict"
            ),
        )
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.RUNNING.value, reason="decided"),
        )
        return note

    async def _phase_final(self) -> None:
        round_ = self.state.round_index or 1
        verdict = self.state.current_round().verdict or {}
        completed = sum(1 for c in self.state.calls.values() if c.status == "completed")
        failed = sum(1 for c in self.state.calls.values() if c.status == "failed")
        stats = {
            "elapsed_s": round(time.monotonic() - self._started, 3),
            "rounds": len(self.state.rounds),
            "version": self.state.version,
            "calls_completed": completed,
            "calls_failed": failed,
            "input_tokens": self.state.usage.input_tokens,
            "output_tokens": self.state.usage.output_tokens,
            "cost_usd": self.state.usage.cost_usd,
        }
        minutes = {
            "question": self.state.question,
            "selected": self.state.selected,
            "scores": self.state.scores,
            "rounds": [
                {
                    "round": r.index,
                    "version": r.version,
                    "reviewers": sorted(r.reviews),
                    "verdict_status": (r.verdict or {}).get("status"),
                }
                for r in self.state.rounds
            ],
            "absentees": self.state.absentees,
            "decisions": self.state.decisions,
            "convergence": self.state.convergence,
        }
        await self._emit(
            EventType.PHASE_COMPLETED,
            PhaseCompleted(
                phase=Phase.FINAL.value,
                round=round_,
                version=self.state.version,
                summary={
                    "final": self.state.current_proposal or {},
                    "minutes": minutes,
                    "disputes": verdict.get("disputes", []),
                    "stats": stats,
                },
            ),
        )
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.COMPLETED.value, reason="final"),
        )

    # ---------------------------------------------------------------- dispatch

    async def _gather(
        self,
        phase: Phase,
        round_: int,
        nodes: Sequence[NodeSection],
        messages: Callable[[NodeSection], MessageBuilder],
        model: type[T],
    ) -> tuple[dict[str, T], list[str], bool]:
        async def one(node: NodeSection) -> T | None:
            try:
                return await self._structured_call(
                    phase=phase, round_=round_, node=node, messages=messages(node), model=model
                )
            except NodeUnavailable as unavailable:
                self._dropped_retryable[node.id] = unavailable.retryable
                return None

        outcomes = await asyncio.gather(*(one(n) for n in nodes), return_exceptions=True)
        results: dict[str, T] = {}
        absent: list[str] = []
        any_retryable = False
        for node, outcome in zip(nodes, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is None:
                absent.append(node.id)
                any_retryable = any_retryable or self._dropped_retryable.get(node.id, False)
            else:
                results[node.id] = outcome
        return results, absent, any_retryable

    async def _structured_call(
        self,
        *,
        phase: Phase,
        round_: int,
        node: NodeSection,
        messages: MessageBuilder,
        model: type[T],
        tag: str = "",
    ) -> T:
        """Call one node: reuse finished work, resolve failures, never guess."""
        cached = self.state.completed_call(phase.value, round_, node.id, tag)
        if cached is not None and cached.parsed is not None:
            return model.model_validate(cached.parsed)

        plan_check = model in (ProposalOut, RevisionOut)
        resolutions = 0
        while True:
            try:
                parsed = await self._attempt_structured(
                    phase, round_, node, messages, model, tag, plan_check=plan_check
                )
            except CouncilError as err:
                await self._record_failure(node, err)
                resolutions += 1
                if resolutions > 2 or not await self._resolve_failure(node, phase, round_, err):
                    raise NodeUnavailable(node.id, err.message, retryable=err.retryable) from err
                continue
            self._breaker.success(node.id)
            return parsed

    async def _attempt_structured(
        self,
        phase: Phase,
        round_: int,
        node: NodeSection,
        messages: MessageBuilder,
        model: type[T],
        tag: str,
        *,
        plan_check: bool = False,
    ) -> T:
        max_attempts = 1 + max(resolve_max_retries(node, self.config), 0)
        last: CouncilError | None = None

        for attempt in range(1, max_attempts + 1):
            await self._gate()
            key = CallKey(
                phase=phase.value, round=round_, node_id=node.id, attempt=attempt, tag=tag
            )
            request = self._build_request(node, messages(""), key)
            await self._emit(
                EventType.CALL_ISSUED,
                CallIssued(
                    key=key.as_key(),
                    node_id=node.id,
                    phase=phase.value,
                    round=round_,
                    attempt=attempt,
                    model=request.model,
                    adapter=node.adapter,
                    request_hash=self._request_hash(request),
                ),
            )
            try:
                text, usage, latency = await self._stream(node, request, key)
            except CouncilError as err:
                await self._emit(
                    EventType.CALL_FAILED,
                    CallFailed(
                        key=key.as_key(),
                        node_id=node.id,
                        phase=phase.value,
                        round=round_,
                        attempt=attempt,
                        tag=tag,
                        kind=err.kind.value,
                        message=err.message,
                        retryable=err.retryable,
                    ),
                )
                last = err
                if err.retryable and attempt < max_attempts:
                    await self._sleep(self._backoff(attempt))
                    continue
                raise

            text, parsed, repaired = await self._parse_with_repair(
                node, key, messages, model, text, plan_check=plan_check
            )
            await self._emit(
                EventType.CALL_COMPLETED,
                CallCompleted(
                    key=key.as_key(),
                    node_id=node.id,
                    phase=phase.value,
                    round=round_,
                    attempt=attempt,
                    tag=tag,
                    model=request.model,
                    raw_text=text,
                    parsed=parsed.model_dump(),
                    usage=usage,
                    latency_ms=latency,
                    repaired=repaired,
                ),
            )
            return parsed

        raise last or CouncilError(ErrorKind.UNKNOWN, "调用失败且没有可用错误信息")

    async def _parse_with_repair(
        self,
        node: NodeSection,
        key: CallKey,
        messages: MessageBuilder,
        model: type[T],
        text: str,
        *,
        plan_check: bool = False,
    ) -> tuple[str, T, int]:
        """Up to two strict repair attempts. The third failure escalates.

        When ``plan_check`` is set (proposals and revisions) a structurally
        valid output can still fail the single-plan gate, and the repair loop
        demands one merged plan instead of silently accepting branches.
        """
        current = text
        for repaired in range(3):
            try:
                parsed = parse_structured(current, model, node_id=node.id)
            except CouncilError as parse_error:
                if repaired >= 2:
                    raise
                current = await self._repair(node, key, messages, model, parse_error)
                continue
            if plan_check:
                violations = find_multi_plan_violations(str(getattr(parsed, "proposal", "")))
                if violations:
                    if repaired >= 2:
                        raise ContractError("；".join(violations), node_id=node.id, raw=current)
                    violation_error = ContractError(
                        SINGLE_PLAN_REPAIR_HINT + "；" + "；".join(violations),
                        node_id=node.id,
                        raw=current,
                    )
                    current = await self._repair(node, key, messages, model, violation_error)
                    continue
            return current, parsed, repaired
        raise CouncilError(ErrorKind.CONTRACT, "输出修复失败", node_id=node.id)

    async def _repair(
        self,
        node: NodeSection,
        key: CallKey,
        messages: MessageBuilder,
        model: type[T],
        error: CouncilError,
    ) -> str:
        prompt = self.prompts.render(
            "repair",
            error=error.message,
            previous_output=wrap_data("previous_output", str(getattr(error, "raw", ""))),
            output_contract=contract_block(model),
        )
        request = self._build_request(
            node, [*messages(""), ChatMessage(role=Role.USER, content=prompt)], key
        )
        text, _, _ = await self._stream(node, request, key)
        return text

    async def _stream(
        self, node: NodeSection, request: ChatRequest, key: CallKey
    ) -> tuple[str, Usage, int]:
        adapter = self._adapter_for(node)
        spec = request.timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + spec.total_s
        started = time.monotonic()
        parts: list[str] = []
        usage = Usage()
        index = 0

        iterator = adapter.chat(request)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CouncilError(
                        ErrorKind.TIMEOUT, f"总耗时超过 {spec.total_s:g}s", node_id=node.id
                    )
                try:
                    chunk: ChatChunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=min(spec.idle_s, remaining)
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError as err:
                    raise CouncilError(
                        ErrorKind.TIMEOUT,
                        f"{spec.idle_s:g}s 内没有任何新输出，判定为卡死",
                        node_id=node.id,
                    ) from err
                if chunk.type is ChunkType.ERROR:
                    raise chunk.error or CouncilError(
                        ErrorKind.UNKNOWN, "适配器报告了未分类的错误", node_id=node.id
                    )
                if chunk.text:
                    parts.append(chunk.text)
                    index += 1
                    await self._emit(
                        EventType.CALL_CHUNK,
                        CallChunk(key=key.as_key(), index=index, text=chunk.text),
                        persist=self.config.budget.persist_chunks,
                    )
                if chunk.usage is not None:
                    usage = chunk.usage
        finally:
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                await aclose()
        return "".join(parts), usage, int((time.monotonic() - started) * 1000)

    # --------------------------------------------------------------- failures

    async def _record_failure(self, node: NodeSection, err: CouncilError) -> None:
        self._breaker.failure(node.id)
        if self._breaker.is_open(node.id) and node.id not in self.state.degraded:
            await self._emit(
                EventType.NODE_STATE,
                NodeStateChanged(
                    node_id=node.id,
                    state="degraded",
                    reason=f"连续 {self.config.failure.circuit_threshold} 次失败：{err.message}",
                ),
            )

    async def _resolve_failure(
        self, node: NodeSection, phase: Phase, round_: int, err: CouncilError
    ) -> bool:
        """Return True to retry the node, False to drop it for this round."""
        policy = self._failure_policy
        if policy is FailurePolicy.CONTINUE:
            return False
        if policy is FailurePolicy.PAUSE:
            await self._pause_and_wait(f"{node.id} 在 {phase.value} 失败：{err.message}")
            return True
        if self.handler is None:
            await self._pause_and_wait(f"{node.id} 在 {phase.value} 失败且无人可问：{err.message}")
            return True
        decision = await self.handler.on_failure(
            InterventionRequest(
                session_id=self.session_id,
                node_id=node.id,
                phase=phase.value,
                round=round_,
                kind=err.kind.value,
                message=err.message,
                retryable=err.retryable,
                available=(
                    InterventionAction.RETRY,
                    InterventionAction.WAIT,
                    InterventionAction.SWITCH_MODEL,
                    InterventionAction.SWITCH_ADAPTER,
                    InterventionAction.DROP_NODE,
                    InterventionAction.ABORT,
                ),
            )
        )
        return await self._apply_decision(node, decision)

    async def _apply_decision(self, node: NodeSection, decision: InterventionDecision) -> bool:
        if decision.action is InterventionAction.RETRY:
            return True
        if decision.action is InterventionAction.WAIT:
            await self._pause_and_wait(f"用户选择等待节点 {node.id}")
            return True
        if decision.action is InterventionAction.SWITCH_MODEL:
            if not decision.model:
                return False
            node.model = decision.model
            await self._emit_config_changed(f"节点 {node.id} 的模型切换为 {decision.model}")
            return True
        if decision.action is InterventionAction.SWITCH_ADAPTER:
            if not decision.adapter:
                return False
            node.adapter = decision.adapter
            if self._adapter_builder is not None:
                self.adapters = dict(self._adapter_builder(self.config))
            await self._emit_config_changed(f"节点 {node.id} 的适配器切换为 {decision.adapter}")
            return True
        if decision.action is InterventionAction.DROP_NODE:
            self._dropped.add(node.id)
            return False
        raise EngineStopped(f"用户终止会话（节点 {node.id} 故障）")

    async def _emit_config_changed(self, reason: str) -> None:
        await self.store.update_session_config(self.session_id, self.config)
        await self._emit(
            EventType.CONFIG_CHANGED,
            ConfigChanged(
                before=self.drift.after if self.drift else "",
                after=fingerprint(self.config),
                diff=[reason],
            ),
        )

    async def _pause_and_wait(self, reason: str) -> None:
        """Freeze the session.

        In a server the UI keeps the coroutine alive and calls ``resume()``; in a
        head-less CLI nobody can, so we persist the breakpoint and raise
        :class:`SessionPaused` — the operator continues with ``council resume``.
        """
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.PAUSED.value, reason=reason),
        )
        # Freeze for real: without clearing the gate, an already-set event lets
        # the wait return instantly and `policy = pause` would never pause.
        self._pause.clear()
        try:
            await asyncio.wait_for(self._pause.wait(), timeout=self._resume_timeout_s)
        except TimeoutError:
            raise SessionPaused(reason) from None
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.RUNNING.value, reason="resumed"),
        )

    async def _await_recovery(self, reason: str) -> None:
        """Outage-shaped block: probe once after a cooldown and retry in place.

        Recovery means enough nodes answer a health probe again; dropped nodes
        are re-admitted (dropping is a per-round decision, not a sentence). If
        nothing recovered, the session unwinds to `council resume`.
        """
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.PAUSED.value, reason=reason),
        )
        await self._sleep(self.config.failure.circuit_cooldown_s)
        await self._probe_nodes()
        for node_id in list(self._dropped):
            if await self._node_healthy(node_id):
                self._dropped.discard(node_id)
        if len(self._available_participants()) < self.config.failure.min_quorum:
            raise SessionPaused(reason)
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.RUNNING.value, reason="recovered"),
        )

    async def _node_healthy(self, node_id: str) -> bool:
        adapter = self.adapters.get(node_id)
        if adapter is None:
            return False
        try:
            status = await asyncio.wait_for(adapter.health(), timeout=15)
        except (TimeoutError, CouncilError, OSError):
            return False
        return status.ok

    async def _probe_nodes(self) -> None:
        for node_id in sorted(self.state.degraded):
            adapter = self.adapters.get(node_id)
            if adapter is None:
                continue
            try:
                status = await asyncio.wait_for(adapter.health(), timeout=15)
            except (TimeoutError, CouncilError, OSError):
                continue
            if status.ok:
                self._breaker.reset(node_id)
                await self._emit(
                    EventType.NODE_STATE,
                    NodeStateChanged(node_id=node_id, state="healthy", reason="health probe ok"),
                )

    async def _ensure_quorum(self) -> None:
        """Freeze below ``min_quorum``: a lone model must never ship a verdict."""
        quorum = self.config.failure.min_quorum
        if len(self._available_participants()) >= quorum:
            return
        reason = f"可用节点 {len(self._available_participants())} 低于 min_quorum={quorum}"
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.PAUSED.value, reason=reason),
        )
        loop = asyncio.get_running_loop()
        # Always allow at least one health probe before giving up.
        deadline = loop.time() + max(self._resume_timeout_s, self.config.failure.circuit_cooldown_s)
        while len(self._available_participants()) < quorum:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionPaused(reason)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._pause.wait(),
                    timeout=min(self.config.failure.circuit_cooldown_s, remaining),
                )
            await self._probe_nodes()
        await self._gate()
        await self._emit(
            EventType.SESSION_STATUS,
            SessionStatusChanged(status=SessionStatus.RUNNING.value, reason="quorum restored"),
        )

    def _available_participants(self) -> list[NodeSection]:
        return [
            n
            for n in self.config.participants
            if n.id not in self.state.degraded
            and n.id not in self._dropped
            and not self._breaker.is_open(n.id)
        ]

    # --------------------------------------------------------------- plumbing

    def _adapter_for(self, node: NodeSection) -> Adapter:
        adapter = self.adapters.get(node.id)
        if adapter is None:
            raise CouncilError(
                ErrorKind.UNKNOWN, f"节点 {node.id} 没有可用适配器（adapter={node.adapter}）"
            )
        return adapter

    def _build_request(
        self, node: NodeSection, messages: list[ChatMessage], key: CallKey
    ) -> ChatRequest:
        metadata = {
            "session_id": self.session_id,
            "phase": key.phase,
            "round": str(key.round),
            "node_id": node.id,
            "attempt": str(key.attempt),
            "idempotency_key": key.as_key(),
        }
        if key.phase == Phase.SELECT.value:
            metadata["candidates"] = ",".join(self.state.proposals)
        return ChatRequest(
            messages=messages,
            model=node.model,
            temperature=resolve_temperature(node, self.config),
            max_output_tokens=resolve_max_output_tokens(node, self.config),
            thinking=resolve_thinking(node, self.config),
            response_format=ResponseFormat.JSON,
            timeout=resolve_timeout(node, self.config),
            metadata=metadata,
        )

    @staticmethod
    def _request_hash(request: ChatRequest) -> str:
        blob = json.dumps(
            {
                "model": request.model,
                "messages": [m.model_dump(mode="json") for m in request.messages],
                "temperature": request.temperature,
                "thinking": request.thinking,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    async def _emit(self, type_: EventType, payload: BaseModel, *, persist: bool = True) -> Event:
        events = await self.store.append(self.session_id, [(type_, payload)], persist=persist)
        event = events[0]
        self.state.apply(event)
        if type_ is EventType.SESSION_STATUS and isinstance(payload, SessionStatusChanged):
            # Keep the cheap status column in sync so `council sessions` and the
            # session list API need no replay.
            await self.store.set_status(self.session_id, payload.status)
        if self.sink is not None:
            await self.sink(event)
        return event

    async def _gate(self) -> None:
        await self._pause.wait()
        if self._stopped:
            raise EngineStopped("用户终止了本次会话")

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter — never a deterministic thundering herd."""
        return min(30.0, 2.0 ** (attempt - 1)) * (0.5 + random.random() / 2)

    # -------------------------------------------------------- prompt builders

    def _system(self, model: type[BaseModel]) -> ChatMessage:
        cached = self._system_cache.get(model.__name__)
        if cached is None:
            cached = ChatMessage(
                role=Role.SYSTEM, content=self.prompts.system(contract_block(model))
            )
            self._system_cache[model.__name__] = cached
        return cached

    def _zones(self, **zones: str) -> str:
        """All external content flows through here, so attachments are always
        inside a data zone regardless of which phase builds the prompt."""
        if self._attachment_zone:
            zones = {"attachments": self._attachment_zone, **zones}
        return "\n\n".join(wrap_data(name, content) for name, content in zones.items())

    @staticmethod
    def _dump(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _with_hint(system: ChatMessage, body: str, hint: str) -> list[ChatMessage]:
        if hint:
            body = f"{body}\n\n补充要求：{hint}"
        return [system, ChatMessage(role=Role.USER, content=body)]

    def _proposal_messages(self, node: NodeSection) -> MessageBuilder:
        def build(hint: str = "") -> list[ChatMessage]:
            body = self.prompts.render(
                "proposal",
                node_id=node.id,
                data_zones=self._zones(question=self.state.question),
            )
            return self._with_hint(self._system(ProposalOut), body, hint)

        return build

    def _select_messages(self, node: NodeSection, hint: str) -> MessageBuilder:
        def build(extra: str = "") -> list[ChatMessage]:
            candidates = "\n\n".join(
                f"### candidate_id={node_id}\n{self._dump(proposal)}"
                for node_id, proposal in self.state.proposals.items()
            )
            body = self.prompts.render(
                "select",
                node_id=node.id,
                hints=extra or hint,
                data_zones=self._zones(question=self.state.question, candidates=candidates),
            )
            return self._with_hint(self._system(JudgeSelectionOut), body, "")

        return build

    def _review_messages(self, node: NodeSection) -> MessageBuilder:
        def build(hint: str = "") -> list[ChatMessage]:
            body = self.prompts.render(
                "review",
                node_id=node.id,
                version=self.state.version,
                author=self.state.selected or "",
                data_zones=self._zones(
                    question=self.state.question,
                    proposal=self._dump(self.state.current_proposal or {}),
                ),
            )
            return self._with_hint(self._system(ReviewOut), body, hint)

        return build

    def _debate_messages(
        self, node: NodeSection, index: int, transcript: list[str]
    ) -> MessageBuilder:
        round_state = self.state.current_round()

        def build(hint: str = "") -> list[ChatMessage]:
            reviews = "\n\n".join(
                f"### reviewer={node_id}\n{self._dump(review)}"
                for node_id, review in round_state.reviews.items()
            )
            body = self.prompts.render(
                "debate",
                node_id=node.id,
                version=self.state.version,
                author=self.state.selected or "",
                debate_round=index,
                debate_rounds=self.config.council.debate_rounds,
                data_zones=self._zones(
                    question=self.state.question,
                    proposal=self._dump(self.state.current_proposal or {}),
                    reviews=reviews,
                    transcript="\n".join(transcript) or "（本轮还没有发言）",
                ),
            )
            return self._with_hint(self._system(DebateOut), body, hint)

        return build

    def _verdict_messages(self, node: NodeSection) -> MessageBuilder:
        round_state = self.state.current_round()

        def build(hint: str = "") -> list[ChatMessage]:
            reviews = "\n\n".join(
                f"### reviewer={node_id}\n{self._dump(review)}"
                for node_id, review in round_state.reviews.items()
            )
            debates = "\n".join(
                f"- {node_id}: {self._dump(debate)}"
                for node_id, debate in round_state.debates.items()
            )
            body = self.prompts.render(
                "verdict",
                node_id=node.id,
                version=self.state.version,
                author=self.state.selected or "",
                data_zones=self._zones(
                    question=self.state.question,
                    proposal=self._dump(self.state.current_proposal or {}),
                    reviews=reviews,
                    debates=debates or "（无辩论记录）",
                ),
            )
            return self._with_hint(self._system(VerdictOut), body, hint)

        return build

    def _revision_messages(self, node: NodeSection, decision: str) -> MessageBuilder:
        round_state = self.state.current_round()

        def build(hint: str = "") -> list[ChatMessage]:
            verdict = round_state.verdict or {}
            body = self.prompts.render(
                "revision",
                node_id=node.id,
                version=self.state.version,
                next_version=self.state.version + 1,
                data_zones=self._zones(
                    question=self.state.question,
                    current_proposal=self._dump(self.state.current_proposal or {}),
                    must_fix=self._dump(verdict.get("must_fix", [])),
                    should_fix=self._dump(verdict.get("should_fix", [])),
                    disputes=self._dump(verdict.get("disputes", [])),
                    user_decision=decision or "（本轮没有用户裁决）",
                ),
            )
            return self._with_hint(self._system(RevisionOut), body, hint)

        return build


def _debate_line(node_id: str, index: int, out: DebateOut) -> str:
    conceded = "；".join(out.conceded) or "无"
    rebuttals = "；".join(out.rebuttals) or "无"
    disputed = "；".join(i.text for i in out.still_disputed) or "无"
    return f"- 第 {index} 轮 {node_id}：接受[{conceded}] 反驳[{rebuttals}] 仍分歧[{disputed}]"

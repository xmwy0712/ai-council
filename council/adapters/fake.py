"""Deterministic in-process adapter.

The fake adapter is what makes the whole engine testable without network access
and what lets a stranger run their first council in ten seconds: the shipped
default config points at it, so ``council run "..."`` works with zero
credentials.

It speaks the same protocol as every real adapter, including streaming, error
chunks and usage reporting.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from ..core.contracts import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChunkType,
    HealthStatus,
    Usage,
)
from ..core.errors import CouncilError, ErrorKind

__all__ = ["FakeAdapter", "default_reply"]

ReplyFn = Callable[[ChatRequest], str]


def _proposal_body(node: str) -> dict[str, Any]:
    return {
        "proposal": (
            f"由 {node} 提出的完整方案：先界定问题边界，再按步骤执行，最后用可观测指标验收。"
        ),
        "assumptions": [f"{node} 假设输入数据可得"],
        "risks": ["若数据缺失，步骤二需要回退到人工补齐"],
        "steps": ["确认目标与验收标准", "收集并核对输入", "执行主流程", "验收与复盘"],
        "open_questions": ["验收指标由谁确认尚未明确"],
    }


def default_reply(req: ChatRequest) -> str:
    """Phase-aware canned reply. Deliberately converges on the first round."""
    phase = req.metadata.get("phase", "")
    node = req.metadata.get("node_id", "node")

    if phase == "P1":
        return json.dumps(_proposal_body(node), ensure_ascii=False)

    if phase == "P2":
        candidates = [c for c in req.metadata.get("candidates", "").split(",") if c]
        selected = candidates[0] if candidates else node
        scores = [
            {
                "node_id": c,
                "correctness": 8,
                "completeness": 8,
                "feasibility": 8,
                "risk": 7,
                "fit": 8,
                "weighted": 7.8,
            }
            for c in candidates
        ]
        return json.dumps(
            {
                "selected_node_id": selected,
                "scores": scores,
                "rationale": "候选方案整体质量接近，选中的这份在可执行性与贴合原问题上略优。",
                "rejected": [
                    {"node_id": c, "fatal_flaw": "步骤粒度偏粗，缺少验收信号"}
                    for c in candidates
                    if c != selected
                ],
            },
            ensure_ascii=False,
        )

    if phase == "P3":
        return json.dumps({"issues": [], "notes": f"{node} 未发现阻塞性问题"}, ensure_ascii=False)

    if phase == "P4":
        return json.dumps(
            {"conceded": [], "rebuttals": [], "still_disputed": []}, ensure_ascii=False
        )

    if phase == "P5":
        return json.dumps(
            {
                "status": "PASS",
                "must_fix": [],
                "should_fix": [],
                "disputes": [],
                "rationale": "没有发现影响方案成立的问题，继续修改不会带来实质收益。",
            },
            ensure_ascii=False,
        )

    if phase == "P6":
        body = _proposal_body(node)
        body["change_summary"] = "吸收评审意见后补充了验收信号与回退路径。"
        return json.dumps(body, ensure_ascii=False)

    return json.dumps({"ok": True}, ensure_ascii=False)


class FakeAdapter:
    """Scripted adapter: no network, no filesystem, no subprocess."""

    def __init__(
        self,
        node_id: str,
        *,
        model: str = "fake/model",
        responder: ReplyFn | None = None,
        failures: Sequence[CouncilError] = (),
        chunk_size: int = 0,
        input_tokens: int = 100,
        delay_s: float = 0.0,
    ) -> None:
        self.id = f"fake:{node_id}"
        self.model = model
        self.capabilities = Capabilities(
            streaming=True,
            thinking_levels=("low", "medium", "high"),
            max_context=128_000,
            structured_output=True,
            max_output_tokens=8_192,
        )
        self.calls: list[ChatRequest] = []
        self.closed = False
        self._responder = responder or default_reply
        self._failures = list(failures)
        self._chunk_size = chunk_size
        self._input_tokens = input_tokens
        self._delay_s = delay_s

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=1, detail="fake adapter is always healthy")

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.calls.append(req)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._failures:
            error = self._failures.pop(0)
            yield ChatChunk(type=ChunkType.ERROR, error=error)
            return

        text = self._responder(req)
        pieces = [text]
        if self._chunk_size > 0:
            pieces = [text[i : i + self._chunk_size] for i in range(0, len(text), self._chunk_size)]
        for piece in pieces:
            yield ChatChunk(type=ChunkType.DELTA, text=piece)
        yield ChatChunk(
            type=ChunkType.DONE,
            finish_reason="stop",
            usage=Usage(input_tokens=self._input_tokens, output_tokens=len(text) // 4),
        )

    async def close(self) -> None:
        self.closed = True


def failing_adapter(node_id: str, kind: ErrorKind, *, times: int = 1) -> FakeAdapter:
    """Convenience for tests: fail the first ``times`` calls, then behave."""
    errors = [CouncilError(kind, f"injected {kind.value}", node_id=node_id) for _ in range(times)]
    return FakeAdapter(node_id, failures=errors)

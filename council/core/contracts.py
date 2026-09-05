"""The only contract between the engine and any model backend.

An adapter is anything that satisfies :class:`Adapter`. API backends, local CLI
sessions and user-defined HTTP templates are all first-class citizens: the
engine cannot tell them apart, and adding a vendor must not touch the engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .errors import CouncilError

__all__ = [
    "Adapter",
    "Capabilities",
    "ChatChunk",
    "ChatMessage",
    "ChatRequest",
    "ChunkType",
    "HealthStatus",
    "ResponseFormat",
    "Role",
    "TimeoutSpec",
    "Usage",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChunkType(StrEnum):
    DELTA = "delta"
    DONE = "done"
    ERROR = "error"


class ResponseFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str


class Usage(BaseModel):
    """Reported token usage for a single call. ``cost_usd`` stays ``None`` until
    the registry knows a price for the model (see M2)."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=(
                None
                if self.cost_usd is None or other.cost_usd is None
                else self.cost_usd + other.cost_usd
            ),
        )


class TimeoutSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    connect_s: float = 30.0
    idle_s: float = 90.0
    total_s: float = 900.0


class ChatRequest(BaseModel):
    """One idempotent model call.

    ``metadata`` always carries ``session_id`` / ``phase`` / ``round`` /
    ``node_id`` / ``attempt`` / ``idempotency_key`` so adapters can log
    structure without understanding the state machine.
    """

    messages: list[ChatMessage]
    model: str
    temperature: float = 0.2
    max_output_tokens: int | None = None
    thinking: str | None = None
    response_format: ResponseFormat = ResponseFormat.JSON
    json_schema: dict[str, Any] | None = None
    timeout: TimeoutSpec = Field(default_factory=TimeoutSpec)
    metadata: dict[str, str] = Field(default_factory=dict)


class ChatChunk(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: ChunkType
    text: str = ""
    usage: Usage | None = None
    finish_reason: str | None = None
    error: CouncilError | None = None


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    streaming: bool = False
    thinking_levels: tuple[str, ...] = ()
    max_context: int = 32_000
    structured_output: bool = True
    max_output_tokens: int | None = None


class HealthStatus(BaseModel):
    ok: bool
    latency_ms: int = 0
    detail: str = ""
    error_kind: str | None = None


@runtime_checkable
class Adapter(Protocol):
    """Uniform backend interface. Structural typing, no base class needed."""

    id: str
    capabilities: Capabilities

    async def health(self) -> HealthStatus: ...

    # Deliberately not `async def`: an async generator *function* returns an
    # AsyncIterator, and declaring it as a coroutine would force every adapter
    # into an await-then-iterate shape for no reason.
    def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def close(self) -> None: ...

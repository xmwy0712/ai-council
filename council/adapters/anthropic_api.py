"""Anthropic Messages API adapter.

Verified against docs/PROVIDERS.md (2026-09-02). Thinking summaries
(``thinking_delta``) are deliberately dropped: the engine's contract wants the
final answer only, and summarized thinking is not part of it.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.config import NodeSection
from ..core.contracts import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChunkType,
    HealthStatus,
    Role,
    Usage,
)
from ..core.errors import CouncilError, ErrorKind
from ..core.secrets import require
from ..registry.loader import get_registry
from ._http import classify_http_error, ensure_success, iter_sse_data

__all__ = ["AnthropicAdapter"]

_API_VERSION = "2023-06-01"


class AnthropicAdapter:
    def __init__(
        self,
        node: NodeSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = node.id
        self._node = node
        registry = get_registry()
        # 节点声明的厂商优先（未来可挂 Anthropic 兼容端点），回退到官方默认值
        provider = registry.provider(node.adapter) or registry.provider("anthropic_api")
        default_base = provider.base_url if provider else "https://api.anthropic.com"
        self._base_url = str(node.settings.get("base_url") or default_base).rstrip("/")
        secret_env = str(
            node.settings.get("secret_env")
            or (provider.secret_env if provider else "ANTHROPIC_API_KEY")
        )
        self._key = require(secret_env)
        self._model = registry.model(node.model)
        self._client = client
        self._owns_client = client is None
        self._thinking_translation = (
            registry.translate_thinking(node.adapter, node.model, node.thinking or "")
            if node.thinking
            else None
        )
        self.capabilities = _capabilities(self._model)

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            client = self._get_client()
            response = await client.get(f"{self._base_url}/v1/models", headers=self._headers())
        except httpx.HTTPError as err:
            classified = classify_http_error(err, node_id=self.id)
            return HealthStatus(
                ok=False, detail=classified.message, error_kind=classified.kind.value
            )
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code in (404, 405):
            return HealthStatus(
                ok=True, latency_ms=latency, detail="探测端点不可用，按首次调用为准"
            )
        if response.status_code >= 400:
            return HealthStatus(
                ok=False,
                latency_ms=latency,
                detail=f"HTTP {response.status_code}",
                error_kind="auth" if response.status_code in (401, 403) else "unknown",
            )
        return HealthStatus(ok=True, latency_ms=latency)

    def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        return self._chat(req)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def _payload(self, req: ChatRequest) -> dict[str, Any]:
        system_parts = [m.content for m in req.messages if m.role is Role.SYSTEM]
        messages = [
            {"role": m.role.value, "content": m.content}
            for m in req.messages
            if m.role is not Role.SYSTEM
        ]
        max_tokens = req.max_output_tokens or 8192
        payload: dict[str, Any] = {
            "model": req.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": req.temperature,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if self._thinking_translation is not None and req.thinking:
            budget = int(self._thinking_translation.value)
            # Official constraint: budget_tokens >= 1024 and < max_tokens.
            budget = max(1024, min(budget, max_tokens - 1))
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return payload

    async def _chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        usage = Usage()
        finish_reason: str | None = None
        try:
            client = self._get_client()
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/messages",
                json=self._payload(req),
                headers=self._headers(),
                timeout=req.timeout.connect_s,
            ) as response:
                await ensure_success(response, node_id=self.id)
                async for raw in iter_sse_data(response):
                    data = json.loads(raw)
                    kind = data.get("type")
                    if kind == "message_start":
                        message = data.get("message") or {}
                        message_usage = message.get("usage") or {}
                        usage = usage.model_copy(
                            update={"input_tokens": int(message_usage.get("input_tokens") or 0)}
                        )
                    elif kind == "content_block_delta":
                        delta = data.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                yield ChatChunk(type=ChunkType.DELTA, text=str(text))
                    elif kind == "message_delta":
                        delta = data.get("delta") or {}
                        reason = delta.get("stop_reason")
                        if reason:
                            finish_reason = str(reason)
                        delta_usage = data.get("usage") or {}
                        if "output_tokens" in delta_usage:
                            output = int(delta_usage["output_tokens"])
                            usage = usage.model_copy(
                                update={
                                    "output_tokens": output,
                                    "total_tokens": usage.input_tokens + output,
                                }
                            )
        except CouncilError:
            raise
        except httpx.HTTPError as err:
            raise classify_http_error(err, node_id=self.id) from err
        if finish_reason == "refusal":
            yield ChatChunk(
                type=ChunkType.ERROR,
                error=CouncilError(ErrorKind.CONTENT_REFUSAL, "模型拒答", node_id=self.id),
            )
            return
        yield ChatChunk(
            type=ChunkType.DONE,
            usage=_costed(usage, self._model),
            finish_reason=finish_reason,
        )


def _capabilities(model: Any) -> Capabilities:
    if model is None:
        return Capabilities(streaming=True, structured_output=True, max_context=200_000)
    registry = get_registry()
    spec = registry.thinking_spec(model.provider, model.id)
    return Capabilities(
        streaming=model.streaming,
        thinking_levels=tuple(sorted(spec.levels)) if model.thinking else (),
        max_context=model.max_context_tokens or 200_000,
        structured_output=model.structured_output,
        max_output_tokens=model.max_output_tokens,
    )


def _costed(usage: Usage, model: Any) -> Usage:
    if model is None or model.price_input_usd is None or model.price_output_usd is None:
        return usage
    cost = (
        usage.input_tokens * model.price_input_usd + usage.output_tokens * model.price_output_usd
    ) / 1_000_000
    return usage.model_copy(update={"cost_usd": round(cost, 6)})

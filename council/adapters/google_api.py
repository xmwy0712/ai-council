"""Google Gemini (generativelanguage) adapter.

Verified against docs/PROVIDERS.md (2026-09-02). The thinking knob differs by
generation — ``thinkingLevel`` (Gemini 3+) vs ``thinkingBudget`` (2.5) — and
the *choice* is data from the registry, not an if-statement in business logic.
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
    ResponseFormat,
    Role,
    Usage,
)
from ..core.errors import CouncilError, ErrorKind
from ..core.secrets import require
from ..registry.loader import get_registry
from ._http import classify_http_error, ensure_success, iter_sse_data

__all__ = ["GoogleAdapter"]


class GoogleAdapter:
    def __init__(
        self,
        node: NodeSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = node.id
        self._node = node
        registry = get_registry()
        provider = registry.provider("google_api")
        default_base = (
            provider.base_url if provider else "https://generativelanguage.googleapis.com"
        )
        self._base_url = str(node.settings.get("base_url") or default_base).rstrip("/")
        secret_env = str(
            node.settings.get("secret_env")
            or (provider.secret_env if provider else "GOOGLE_API_KEY")
        )
        self._key = require(secret_env)
        self._model = registry.model(node.model)
        self._client = client
        self._owns_client = client is None
        self._thinking_translation = (
            registry.translate_thinking("google_api", node.model, node.thinking or "")
            if node.thinking
            else None
        )
        self.capabilities = _capabilities(self._model)

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            client = self._get_client()
            response = await client.get(f"{self._base_url}/v1beta/models", headers=self._headers())
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
        return {"x-goog-api-key": self._key, "content-type": "application/json"}

    def _payload(self, req: ChatRequest) -> dict[str, Any]:
        system_parts = [m.content for m in req.messages if m.role is Role.SYSTEM]
        contents = [
            {
                "role": "model" if m.role is Role.ASSISTANT else "user",
                "parts": [{"text": m.content}],
            }
            for m in req.messages
            if m.role is not Role.SYSTEM
        ]
        generation: dict[str, Any] = {"temperature": req.temperature}
        if req.max_output_tokens is not None:
            generation["maxOutputTokens"] = req.max_output_tokens
        if req.response_format is ResponseFormat.JSON:
            generation["responseMimeType"] = "application/json"
        if self._thinking_translation is not None and req.thinking:
            translation = self._thinking_translation
            if translation.style == "thinking_budget":
                generation["thinkingConfig"] = {"thinkingBudget": int(translation.value)}
            else:  # thinking_level
                generation["thinkingConfig"] = {"thinkingLevel": str(translation.value)}
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    async def _chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        usage = Usage()
        finish_reason: str | None = None
        try:
            client = self._get_client()
            async with client.stream(
                "POST",
                f"{self._base_url}/v1beta/models/{req.model}:streamGenerateContent?alt=sse",
                json=self._payload(req),
                headers=self._headers(),
                timeout=req.timeout.connect_s,
            ) as response:
                await ensure_success(response, node_id=self.id)
                async for raw in iter_sse_data(response):
                    data = json.loads(raw)
                    feedback = data.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        yield ChatChunk(
                            type=ChunkType.ERROR,
                            error=CouncilError(
                                ErrorKind.CONTENT_REFUSAL,
                                str(feedback.get("blockReason")),
                                node_id=self.id,
                            ),
                        )
                        return
                    metadata = data.get("usageMetadata")
                    if isinstance(metadata, dict):
                        usage = Usage(
                            input_tokens=int(metadata.get("promptTokenCount") or 0),
                            output_tokens=int(metadata.get("candidatesTokenCount") or 0),
                            total_tokens=int(metadata.get("totalTokenCount") or 0),
                        )
                    for candidate in data.get("candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        reason = candidate.get("finishReason")
                        if reason:
                            finish_reason = str(reason)
                        content = candidate.get("content") or {}
                        for part in content.get("parts") or []:
                            if not isinstance(part, dict) or part.get("thought"):
                                continue  # thought parts are the thinking trace
                            text = part.get("text")
                            if text:
                                yield ChatChunk(type=ChunkType.DELTA, text=str(text))
        except CouncilError:
            raise
        except httpx.HTTPError as err:
            raise classify_http_error(err, node_id=self.id) from err
        yield ChatChunk(
            type=ChunkType.DONE,
            usage=_costed(usage, self._model),
            finish_reason=finish_reason,
        )


def _capabilities(model: Any) -> Capabilities:
    if model is None:
        return Capabilities(streaming=True, structured_output=True, max_context=1_048_576)
    registry = get_registry()
    spec = registry.thinking_spec(model.provider, model.id)
    return Capabilities(
        streaming=model.streaming,
        thinking_levels=tuple(sorted(spec.levels)) if model.thinking else (),
        max_context=model.max_context_tokens or 1_048_576,
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

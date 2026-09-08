"""OpenAI and any OpenAI-compatible endpoint (custom ``base_url`` welcome).

Payload shapes follow docs/PROVIDERS.md (verified 2026-09-02). The adapter is
dumb on purpose: thinking translation comes from the registry TOML, error
classes from ``_http``, and JSON enforcement is the engine's problem.
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
    Usage,
)
from ..core.errors import CouncilError, ErrorKind
from ..core.secrets import require, resolve
from ..registry.loader import get_registry
from ._http import classify_http_error, ensure_success, iter_sse_data

__all__ = ["OpenAIAdapter"]

# 未声明 adapter 的节点（自定义 base_url）沿用 OpenAI 的默认值。
_DEFAULT_PROVIDER = "openai_api"


class OpenAIAdapter:
    def __init__(
        self,
        node: NodeSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = node.id
        self._node = node
        registry = get_registry()
        # 节点声明的厂商优先（DeepSeek / 智谱 / 通义 / Ollama …），
        # 找不到时回退到 OpenAI 默认端点。
        provider = registry.provider(node.adapter) or registry.provider(_DEFAULT_PROVIDER)
        default_base = provider.base_url if provider else "https://api.openai.com/v1"
        self._base_url = str(node.settings.get("base_url") or default_base).rstrip("/")
        secret_env = str(
            node.settings.get("secret_env")
            or (provider.secret_env if provider else "OPENAI_API_KEY")
        )
        # 本地/自建端点（Ollama、vLLM）没有密钥也能跑，缺密钥时不发 Authorization
        if provider is not None and not provider.secret_required:
            self._key = resolve(secret_env) or ""
        else:
            self._key = require(secret_env)
        self._model = registry.model(node.model)
        self._client = client
        self._owns_client = client is None
        self._omit_temperature = (self._model.omit_temperature if self._model else False) or (
            bool(provider.omit_temperature) if provider else False
        )
        self._thinking_translation = (
            registry.translate_thinking(node.adapter, node.model, node.thinking or "")
            if node.thinking
            else None
        )
        self.capabilities = _capabilities(self._model)

    # ------------------------------------------------------------- interface

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            client = self._get_client()
            response = await client.get(f"{self._base_url}/models", headers=self._headers())
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

    # ------------------------------------------------------------ internals

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._key:  # 免密钥端点（Ollama / vLLM）不发送 Authorization
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def _payload(self, req: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": message.role.value, "content": message.content} for message in req.messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not self._omit_temperature:
            payload["temperature"] = req.temperature
        if req.max_output_tokens is not None:
            payload["max_completion_tokens"] = req.max_output_tokens
        if req.response_format is ResponseFormat.JSON:
            payload["response_format"] = {"type": "json_object"}
            # DeepSeek 要求 prompt 包含 "json" 才允许 json_object 模式
            if self._node.adapter == "deepseek":
                last_user = next(
                    (m for m in reversed(payload["messages"]) if m["role"] == "user"), None
                )
                if last_user and "json" not in last_user["content"].lower():
                    last_user["content"] += "\n（请以 JSON 格式输出）"
        if self._thinking_translation is not None and req.thinking:
            payload[self._thinking_translation.param] = self._thinking_translation.value
            # 伴生字段（如 DashScope 的 enable_thinking）一并发出
            payload.update(dict(self._thinking_translation.extra))
        return payload

    async def _chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        usage = Usage()
        finish_reason: str | None = None
        try:
            client = self._get_client()
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=self._payload(req),
                headers=self._headers(),
                timeout=req.timeout.connect_s,
            ) as response:
                await ensure_success(response, node_id=self.id)
                async for raw in iter_sse_data(response):
                    data = json.loads(raw)
                    chunk_usage = data.get("usage")
                    if isinstance(chunk_usage, dict):
                        usage = Usage(
                            input_tokens=int(chunk_usage.get("prompt_tokens") or 0),
                            output_tokens=int(chunk_usage.get("completion_tokens") or 0),
                            total_tokens=int(chunk_usage.get("total_tokens") or 0),
                        )
                    for choice in data.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        reason = choice.get("finish_reason")
                        if reason:
                            finish_reason = str(reason)
                        message = choice.get("message") or choice.get("delta") or {}
                        refusal = message.get("refusal")
                        if refusal:
                            yield ChatChunk(
                                type=ChunkType.ERROR,
                                error=CouncilError(
                                    ErrorKind.CONTENT_REFUSAL,
                                    str(refusal)[:200],
                                    node_id=self.id,
                                ),
                            )
                            return
                        text = message.get("content")
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
        return Capabilities(streaming=True, structured_output=True, max_context=32_000)
    registry = get_registry()
    spec = registry.thinking_spec(model.provider, model.id)
    return Capabilities(
        streaming=model.streaming,
        thinking_levels=tuple(sorted(spec.levels)) if model.thinking else (),
        max_context=model.max_context_tokens or 32_000,
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

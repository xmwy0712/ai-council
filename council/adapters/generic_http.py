"""User-defined HTTP template adapter, for vendors with no built-in adapter.

Everything about the request is data in the node settings: URL, method,
headers, body template and the response JSONPath. Templates support
``{model}``, ``{prompt}``, ``{prompt_json}`` (JSON-escaped), ``{system}`` and
``${ENV_VAR}`` (resolved through the secret store, never written anywhere).
Streaming is intentionally out of scope for v1 — a plain JSON response is
required.
"""

from __future__ import annotations

import json
import re
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
from ..core.secrets import resolve
from ._http import classify_http_error, ensure_success, parse_json_path

__all__ = ["GenericHttpAdapter"]

_ENV_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class GenericHttpAdapter:
    def __init__(
        self,
        node: NodeSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = node.id
        self._node = node
        settings = node.settings
        self._url = str(settings.get("url") or "")
        if not self._url:
            raise CouncilError(
                ErrorKind.CONTRACT, "generic_http 需要 nodes.settings.url", node_id=node.id
            )
        self._method = str(settings.get("method") or "POST").upper()
        headers = settings.get("headers") or {}
        if not isinstance(headers, dict):
            raise CouncilError(
                ErrorKind.CONTRACT, "generic_http 的 headers 必须是表", node_id=node.id
            )
        self._headers = {str(k): self._expand(str(v)) for k, v in headers.items()}
        body = settings.get("body_template")
        if body is None:
            raise CouncilError(
                ErrorKind.CONTRACT,
                "generic_http 需要 nodes.settings.body_template",
                node_id=node.id,
            )
        self._body_template = str(body)
        self._response_path = str(settings.get("response_path") or "")
        if not self._response_path:
            raise CouncilError(
                ErrorKind.CONTRACT,
                "generic_http 需要 nodes.settings.response_path",
                node_id=node.id,
            )
        self._usage_input_path = str(settings.get("usage_input_path") or "")
        self._usage_output_path = str(settings.get("usage_output_path") or "")
        self._client = client
        self._owns_client = client is None
        self.capabilities = Capabilities(streaming=False, structured_output=True, max_context=0)

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="generic_http 无健康端点，按首次调用为准")

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

    def _expand(self, template: str) -> str:
        """Expand ``${ENV}`` in headers. Values never leave this process."""

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = resolve(name)
            if value is None:
                raise CouncilError(ErrorKind.AUTH, f"模板引用的密钥 {name} 未配置", node_id=self.id)
            return value

        return _ENV_TOKEN.sub(replace, template)

    def _render_body(self, req: ChatRequest) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in req.messages if m.role is Role.SYSTEM)
        prompt = "\n\n".join(m.content for m in req.messages if m.role is not Role.SYSTEM)
        rendered = (
            self._body_template.replace("{prompt_json}", json.dumps(prompt, ensure_ascii=False))
            .replace("{system_json}", json.dumps(system, ensure_ascii=False))
            .replace("{prompt}", prompt)
            .replace("{system}", system)
            .replace("{model}", req.model)
        )
        rendered = self._expand(rendered)
        try:
            body = json.loads(rendered)
        except json.JSONDecodeError as err:
            raise CouncilError(
                ErrorKind.CONTRACT,
                f"body_template 渲染后不是合法 JSON：{err.msg}",
                node_id=self.id,
            ) from err
        if not isinstance(body, dict):
            raise CouncilError(
                ErrorKind.CONTRACT, "body_template 渲染后必须是 JSON 对象", node_id=self.id
            )
        return body

    async def _chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        try:
            client = self._get_client()
            response = await client.request(
                self._method,
                self._url,
                json=self._render_body(req),
                headers=self._headers,
                timeout=req.timeout.total_s,
            )
            await ensure_success(response, node_id=self.id)
            data = response.json()
        except CouncilError:
            raise
        except json.JSONDecodeError as err:
            raise CouncilError(
                ErrorKind.CONTRACT, f"响应不是合法 JSON：{err.msg}", node_id=self.id
            ) from err
        except httpx.HTTPError as err:
            raise classify_http_error(err, node_id=self.id) from err
        try:
            text = parse_json_path(data, self._response_path)
        except KeyError as err:
            raise CouncilError(ErrorKind.CONTRACT, str(err), node_id=self.id) from err
        usage = Usage(
            input_tokens=_int_path(data, self._usage_input_path),
            output_tokens=_int_path(data, self._usage_output_path),
        )
        usage = usage.model_copy(update={"total_tokens": usage.input_tokens + usage.output_tokens})
        yield ChatChunk(type=ChunkType.DELTA, text=str(text))
        yield ChatChunk(type=ChunkType.DONE, usage=usage)


def _int_path(data: Any, path: str) -> int:
    if not path:
        return 0
    try:
        value = parse_json_path(data, path)
    except KeyError:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

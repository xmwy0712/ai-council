"""Shared HTTP plumbing for the API adapters.

One place for: error classification (see docs/PROVIDERS.md), SSE line handling
and redaction of anything that might contain a key. Adapters add their own
payload shape; the *rules* never diverge.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..core.errors import CouncilError, ErrorKind
from ..core.secrets import redact

__all__ = ["classify_http_error", "ensure_success", "iter_sse_data", "parse_json_path"]


def classify_http_error(exc: BaseException, *, node_id: str | None = None) -> CouncilError:
    """Turn an httpx failure into our six-class taxonomy."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return CouncilError(ErrorKind.AUTH, f"认证失败（HTTP {status}）", node_id=node_id)
        if status == 429:
            return CouncilError(ErrorKind.RATE_LIMIT, "限流（HTTP 429）", node_id=node_id)
        if status == 529:
            return CouncilError(ErrorKind.RATE_LIMIT, "服务端过载（HTTP 529）", node_id=node_id)
        if status == 408:
            return CouncilError(ErrorKind.TIMEOUT, "服务端超时（HTTP 408）", node_id=node_id)
        if status in (400, 404):
            body = _safe_body(exc.response)
            return CouncilError(
                ErrorKind.CONTRACT,
                f"请求被厂商拒绝（HTTP {status}）：{body}",
                node_id=node_id,
            )
        if status >= 500:
            return CouncilError(ErrorKind.NETWORK, f"服务端错误（HTTP {status}）", node_id=node_id)
        return CouncilError(ErrorKind.UNKNOWN, f"HTTP {status}", node_id=node_id)
    if isinstance(exc, httpx.TimeoutException):
        return CouncilError(ErrorKind.TIMEOUT, "连接或读取超时", node_id=node_id)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return CouncilError(ErrorKind.NETWORK, f"网络中断：{type(exc).__name__}", node_id=node_id)
    return CouncilError(
        ErrorKind.UNKNOWN, f"未分类的传输错误：{type(exc).__name__}", node_id=node_id
    )


def _safe_body(response: httpx.Response, *, limit: int = 200) -> str:
    try:
        text = response.text
    except Exception:
        return "(无法读取响应体)"
    # Never trust the body to be key-free; many proxies echo the Authorization.
    text = redact(text, response.request.headers.get("Authorization", ""))
    return text[:limit]


async def ensure_success(response: httpx.Response, *, node_id: str | None = None) -> None:
    if response.status_code < 400:
        return
    raise classify_http_error(
        httpx.HTTPStatusError(
            message=f"HTTP {response.status_code}", request=response.request, response=response
        ),
        node_id=node_id,
    )


async def iter_sse_data(response: httpx.Response) -> Any:
    """Yield decoded JSON objects from a `data:`-only SSE body.

    A normal `for line in aiter_lines()` loop with per-line JSON parsing is
    friendlier to read but hides malformed payloads behind a blanket except; we
    would rather crash loudly than silently drop model output. Callers pass the
    async generator straight through.
    """
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                yield payload


def parse_json_path(data: Any, path: str) -> Any:
    """Minimal dotted JSONPath for generic_http templates.

    Supports `a.b[0].c` only — deliberately not arbitrary JSONPath, which would
    drag in a dependency and invite injection-shaped paths from configs.
    """
    current: Any = data
    for segment in path.split("."):
        if not segment:
            continue
        key, _, index_part = segment.partition("[")
        if key:
            if not isinstance(current, dict) or key not in current:
                raise KeyError(f"响应中不存在字段 {key!r}（路径 {path!r}）")
            current = current[key]
        while index_part:
            index_str, _, index_part = index_part.partition("[")
            index_str = index_str.rstrip("]")
            try:
                index = int(index_str)
            except ValueError as err:
                raise KeyError(f"路径段 {segment!r} 的索引不是整数") from err
            if not isinstance(current, list) or index >= len(current):
                raise KeyError(f"响应中不存在索引 [{index}]（路径 {path!r}）")
            current = current[index]
    return current

"""Shared HTTP plumbing for the API adapters.

One place for: error classification (see docs/PROVIDERS.md), SSE line handling
and redaction of anything that might contain a key. Adapters add their own
payload shape; the *rules* never diverge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ..core.errors import CouncilError, ErrorKind
from ..core.secrets import redact

if TYPE_CHECKING:
    from ..core.contracts import TimeoutSpec

__all__ = [
    "classify_http_error",
    "ensure_success",
    "iter_sse_data",
    "parse_json_path",
    "stream_timeout",
]


def stream_timeout(spec: TimeoutSpec) -> httpx.Timeout:
    """构造流式请求的超时，四类分开设置。

    起因是一个真实缺陷：适配器此前写 ``timeout=spec.connect_s``，而 httpx 在收到
    **单个数值**时会把它同时应用到 connect / read / write / pool 四类。于是
    「连接超时」（默认 30s）被当成了「读取超时」——推理模型在 max 档位下首次
    出字往往超过 30 秒，长问题必然撞上「连接或读取超时」，而配置里的 ``idle_s``
    （90s）与 ``total_s``（900s）根本没参与 HTTP 读取，调大也没用。

    语义对应：
      - ``connect``：建连，取 ``connect_s``
      - ``read``：**两次数据之间**的等待上限，取 ``idle_s``。httpx 的 read 超时
        是「单次 socket 读操作的等待」，流式响应里每个 chunk 到达都会重置它，
        所以它天然表达「多久没有新输出即判定卡死」。
      - ``write`` / ``pool``：沿用小值，避免半开连接长时间占用

    整体墙钟上限（``total_s``）不在这里体现：它是「单次调用总时长」，应当由
    引擎层对整轮调用计时，塞进单次 socket 超时只会让语义变混。
    """
    return httpx.Timeout(
        connect=spec.connect_s,
        read=spec.idle_s,
        write=spec.connect_s,
        pool=spec.connect_s,
    )


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
        # 区分建连超时与读取超时：前者多为网络/代理问题，后者常是模型首字慢
        detail = "连接超时" if isinstance(exc, httpx.ConnectTimeout) else "读取超时（等待模型输出）"
        return CouncilError(ErrorKind.TIMEOUT, detail, node_id=node_id)
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

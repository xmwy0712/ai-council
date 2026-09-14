"""回归测试：流式请求的超时必须按语义分类，读取超时不能被 connect_s 顶替。

背景（真实缺陷）：适配器此前写 ``timeout=req.timeout.connect_s``，
而 httpx 收到单个数值时会把 connect/read/write/pool 全设成该值。
于是默认 30s 的「连接超时」被当成了「读取超时」——推理模型在 max 档位下
首次出字常超过 30 秒，长问题必然报「连接或读取超时」，而配置里的
``idle_s``（卡死判定，90s）根本没参与 HTTP 读取，调大也无效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council.adapters._http import stream_timeout
from council.core.contracts import TimeoutSpec

ADAPTERS = ("openai_api.py", "anthropic_api.py", "google_api.py", "generic_http.py")


def test_read_timeout_follows_idle_not_connect() -> None:
    """读取超时应取 idle_s，而不是 connect_s。"""
    spec = TimeoutSpec(connect_s=30.0, idle_s=120.0, total_s=900.0)
    t = stream_timeout(spec)
    assert t.connect == 30.0, "连接超时应用 connect_s"
    assert t.read == 120.0, "读取超时必须用 idle_s —— 这是本次修复的核心"
    assert t.read != t.connect, "读取与连接必须是两个独立的量"


def test_long_first_token_is_not_cut_off_by_connect_timeout() -> None:
    """首字延迟超过 connect_s 但未超过 idle_s 时，不应被判超时。

    这是用户遇到的场景：5.6-sol 开 max 档，首次出字用了 45 秒。
    """
    spec = TimeoutSpec(connect_s=30.0, idle_s=90.0, total_s=900.0)
    t = stream_timeout(spec)
    first_token_delay = 45.0
    assert first_token_delay > t.connect, "前提：该延迟确实超过了连接超时"
    assert first_token_delay < t.read, "45s 的首字延迟必须落在读取超时之内"


def test_write_and_pool_stay_small() -> None:
    """write/pool 沿用连接超时的量级，避免半开连接长期占用。"""
    spec = TimeoutSpec(connect_s=25.0, idle_s=300.0, total_s=900.0)
    t = stream_timeout(spec)
    assert t.write == 25.0
    assert t.pool == 25.0


def test_total_s_is_not_used_as_a_socket_timeout() -> None:
    """整体墙钟上限不该塞进单次 socket 超时（语义不同）。"""
    spec = TimeoutSpec(connect_s=30.0, idle_s=90.0, total_s=1800.0)
    t = stream_timeout(spec)
    assert t.read != 1800.0, "total_s 不应成为读取超时"
    assert t.connect != 1800.0


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_streaming_adapter_uses_the_shared_helper(name: str) -> None:
    """四个流式适配器都必须走统一构造器，不得再传裸数值。"""
    src = (Path(__file__).resolve().parents[1] / "council" / "adapters" / name).read_text(
        encoding="utf-8"
    )
    assert "stream_timeout(req.timeout)" in src, f"{name} 未使用 stream_timeout"
    for bad in ("timeout=req.timeout.connect_s", "timeout=req.timeout.total_s"):
        assert bad not in src, f"{name} 仍有裸数值超时：{bad}"


def test_timeout_error_message_distinguishes_connect_and_read() -> None:
    """报错要能区分「连接超时」与「读取超时」，便于定位。"""
    import httpx

    from council.adapters._http import classify_http_error

    read_err = classify_http_error(httpx.ReadTimeout("slow first token"))
    connect_err = classify_http_error(httpx.ConnectTimeout("no route"))
    assert "读取" in read_err.message
    assert "连接" in connect_err.message
    assert read_err.message != connect_err.message

"""API adapters against httpx.MockTransport. No sockets, ever."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from council.adapters.anthropic_api import AnthropicAdapter
from council.adapters.google_api import GoogleAdapter
from council.adapters.openai_api import OpenAIAdapter
from council.core.config import NodeSection
from council.core.contracts import (
    ChatMessage,
    ChatRequest,
    ChunkType,
    ResponseFormat,
    Role,
    TimeoutSpec,
)
from council.core.errors import CouncilError, ErrorKind


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _request(**kwargs: Any) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role=Role.SYSTEM, content="system prompt"),
            ChatMessage(role=Role.USER, content="user question"),
        ],
        model=kwargs.pop("model", "gpt-5.2"),
        thinking=kwargs.pop("thinking", "high"),
        response_format=ResponseFormat.JSON,
        max_output_tokens=kwargs.pop("max_output_tokens", 8000),
        timeout=TimeoutSpec(connect_s=5, idle_s=5, total_s=10),
        **kwargs,
    )


def _capture(
    handler_body: str | list[dict[str, Any]], status: int = 200
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        if isinstance(handler_body, list):
            return httpx.Response(status, text=_sse(handler_body))
        return httpx.Response(status, text=handler_body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), bodies


# ---------------------------------------------------------------- openai


async def test_openai_payload_and_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    events = [
        {"choices": [{"delta": {"content": '{"a":'}, "index": 0}]},
        {"choices": [{"delta": {"content": "1}"}, "index": 0, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    client, bodies = _capture(events)
    node = NodeSection(id="n1", adapter="openai_api", model="gpt-5.2", thinking="high")
    adapter = OpenAIAdapter(node, client=client)

    chunks = [c async for c in adapter.chat(_request())]

    body = bodies[0]
    assert body["model"] == "gpt-5.2"
    assert body["messages"][0] == {"role": "system", "content": "system prompt"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "high"
    assert body["max_completion_tokens"] == 8000
    assert body["stream"] is True

    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    assert text == '{"a":1}'
    done = chunks[-1]
    assert done.usage is not None
    assert (done.usage.input_tokens, done.usage.output_tokens) == (10, 5)


async def test_openai_respects_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, text=_sse([{"choices": [{"delta": {"content": "ok"}}]}]))

    node = NodeSection(
        id="n1",
        adapter="openai_api",
        model="kimi-k2",
        settings={"base_url": "https://api.moonshot.cn/v1"},
    )
    adapter = OpenAIAdapter(node, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _ = [c async for c in adapter.chat(_request(model="kimi-k2", thinking=None))]
    assert urls[0].startswith("https://api.moonshot.cn/v1/chat/completions")


async def test_openai_401_is_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
    client, _ = _capture('{"error": {"message": "Invalid API key"}}', status=401)
    adapter = OpenAIAdapter(
        NodeSection(id="n1", adapter="openai_api", model="gpt-5.2"), client=client
    )
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(_request())]
    assert info.value.kind is ErrorKind.AUTH
    assert info.value.retryable is False
    assert "bad-key" not in str(info.value)


async def test_openai_429_is_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client, _ = _capture("{}", status=429)
    adapter = OpenAIAdapter(
        NodeSection(id="n1", adapter="openai_api", model="gpt-5.2"), client=client
    )
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(_request())]
    assert info.value.kind is ErrorKind.RATE_LIMIT
    assert info.value.retryable is True


async def test_openai_refusal_is_content_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    events = [{"choices": [{"delta": {"refusal": "不能协助该请求"}, "index": 0}]}]
    client, _ = _capture(events)
    adapter = OpenAIAdapter(
        NodeSection(id="n1", adapter="openai_api", model="gpt-5.2"), client=client
    )
    chunks = [c async for c in adapter.chat(_request())]
    errors = [c for c in chunks if c.type is ChunkType.ERROR]
    assert errors and errors[0].error is not None
    assert errors[0].error.kind is ErrorKind.CONTENT_REFUSAL


async def test_openai_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"data": []}')

    adapter = OpenAIAdapter(
        NodeSection(id="n1", adapter="openai_api", model="gpt-5.2"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status = await adapter.health()
    assert status.ok


# ------------------------------------------------------------- anthropic


def _anthropic_events() -> list[dict[str, Any]]:
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "让我想想"},
        },
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": '{"a":'},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "1}"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]


async def test_anthropic_payload_and_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client, bodies = _capture(_anthropic_events())
    node = NodeSection(
        id="n2", adapter="anthropic_api", model="claude-sonnet-4-6", thinking="medium"
    )
    adapter = AnthropicAdapter(node, client=client)

    chunks = [
        c async for c in adapter.chat(_request(model="claude-sonnet-4-6", max_output_tokens=16000))
    ]

    body = bodies[0]
    assert body["system"] == "system prompt"
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])
    assert body["max_tokens"] == 16000
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    assert text == '{"a":1}'
    assert "让我想想" not in text  # thinking deltas are dropped
    done = chunks[-1]
    assert done.usage is not None
    assert (done.usage.input_tokens, done.usage.output_tokens, done.usage.total_tokens) == (
        12,
        7,
        19,
    )


async def test_anthropic_budget_is_clamped_below_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client, bodies = _capture(_anthropic_events())
    node = NodeSection(id="n2", adapter="anthropic_api", model="claude-sonnet-4-6", thinking="high")
    adapter = AnthropicAdapter(node, client=client)
    req = _request(model="claude-sonnet-4-6", max_output_tokens=2000)
    _ = [c async for c in adapter.chat(req)]
    budget = bodies[0]["thinking"]["budget_tokens"]
    assert 1024 <= budget < 2000


async def test_anthropic_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    events = [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "refusal"},
            "usage": {"output_tokens": 1},
        }
    ]
    client, _ = _capture(events)
    adapter = AnthropicAdapter(
        NodeSection(id="n2", adapter="anthropic_api", model="claude-sonnet-4-6"), client=client
    )
    chunks = [c async for c in adapter.chat(_request(model="claude-sonnet-4-6"))]
    errors = [c for c in chunks if c.type is ChunkType.ERROR]
    assert errors and errors[0].error is not None
    assert errors[0].error.kind is ErrorKind.CONTENT_REFUSAL


# ----------------------------------------------------------------- google


def _google_events() -> list[dict[str, Any]]:
    return [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": '{"a":'}]}}]},
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "嗯……", "thought": True}]}}
            ]
        },
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "1}"}]}, "finishReason": "STOP"}
            ]
        },
        {
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 6,
                "totalTokenCount": 17,
            }
        },
    ]


async def test_google_thinking_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-test")
    client, bodies = _capture(_google_events())
    node = NodeSection(id="n3", adapter="google_api", model="gemini-3.5-flash", thinking="high")
    adapter = GoogleAdapter(node, client=client)
    chunks = [c async for c in adapter.chat(_request(model="gemini-3.5-flash"))]

    generation = bodies[0]["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingLevel": "HIGH"}
    assert generation["responseMimeType"] == "application/json"
    assert bodies[0]["systemInstruction"] == {"parts": [{"text": "system prompt"}]}

    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    assert text == '{"a":1}'
    assert "嗯……" not in text  # thought parts are skipped
    done = chunks[-1]
    assert done.usage is not None
    assert (done.usage.input_tokens, done.usage.output_tokens) == (11, 6)


async def test_google_thinking_budget_for_25(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-test")
    client, bodies = _capture(_google_events())
    node = NodeSection(id="n3", adapter="google_api", model="gemini-2.5-pro", thinking="medium")
    adapter = GoogleAdapter(node, client=client)
    _ = [c async for c in adapter.chat(_request(model="gemini-2.5-pro"))]
    assert bodies[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 4096}


async def test_google_block_reason_is_content_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-test")
    client, _ = _capture([{"promptFeedback": {"blockReason": "SAFETY"}}])
    adapter = GoogleAdapter(
        NodeSection(id="n3", adapter="google_api", model="gemini-3.5-flash"), client=client
    )
    chunks = [c async for c in adapter.chat(_request(model="gemini-3.5-flash"))]
    errors = [c for c in chunks if c.type is ChunkType.ERROR]
    assert errors and errors[0].error is not None
    assert errors[0].error.kind is ErrorKind.CONTENT_REFUSAL


async def test_google_endpoint_uses_model_in_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(
            200, text=_sse([{"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}])
        )

    adapter = GoogleAdapter(
        NodeSection(id="n3", adapter="google_api", model="gemini-3.5-flash"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    _ = [c async for c in adapter.chat(_request(model="gemini-3.5-flash"))]
    assert "models/gemini-3.5-flash:streamGenerateContent?alt=sse" in urls[0]

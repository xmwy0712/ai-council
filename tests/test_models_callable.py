"""全量模型干跑回归：注册表内每个模型都必须能构造出合法的调用负载。

与 tools/check_models.py 同源的离线校验（不发网络请求）：
每个 (厂商, 模型) 真实实例化适配器 → 构造含思考档位的 ChatRequest →
生成请求负载并断言 model id、思考旋钮、伴生字段、Anthropic 预算钳制
全部符合注册表声明。新增模型条目若数据不全（缺 display/verified/source、
上下文非法、旋钮不可翻译）会在此测试中直接失败。
"""

from __future__ import annotations

import pytest

from council.adapters import _resolve_factory
from council.adapters.anthropic_api import AnthropicAdapter
from council.adapters.google_api import GoogleAdapter
from council.adapters.openai_api import OpenAIAdapter
from council.core.config import NodeSection
from council.core.contracts import ChatMessage, ChatRequest, ResponseFormat, Role
from council.registry import Registry

# 干跑校验的对象是**随包发布的已策展条目**。自动发现写进用户数据目录的覆盖层是
# 刻意只带最少信息的用户数据（公开目录没报的上下文就不写），不该让它决定测试
# 用例集，否则套件会随机器状态漂移。
CURATED = Registry.load(include_discovered=False)

_REQUIRED_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "DASHSCOPE_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "SILICONFLOW_API_KEY",
    "MODEL_API_KEY",
]


@pytest.fixture(autouse=True)
def _placeholder_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _REQUIRED_KEYS:
        monkeypatch.setenv(key, "sk-dry-run-placeholder")


def _request(model_id: str, thinking: str | None) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role=Role.SYSTEM, content="你是评审。"),
            ChatMessage(role=Role.USER, content="用一句话回答：1+1=?"),
        ],
        model=model_id,
        thinking=thinking,
        response_format=ResponseFormat.JSON,
        # 足够大：Anthropic 的 budget_tokens 必须 < max_tokens，
        # 太小会把档位钳制到 1024，干扰校验。
        max_output_tokens=65536,
    )


def _registry_cases() -> list[tuple[str, str]]:
    registry = CURATED
    cases: list[tuple[str, str]] = []
    for provider in registry.providers.values():
        if provider.id == "fake":
            continue
        for model in provider.models.values():
            cases.append((provider.id, model.id))
    return cases


CASES = _registry_cases()


@pytest.mark.parametrize(("provider_id", "model_id"), CASES)
def test_model_builds_valid_payload(provider_id: str, model_id: str) -> None:
    registry = CURATED
    model = registry.model(model_id)
    assert model is not None
    spec = registry.thinking_spec(provider_id, model_id)
    scalar_style = spec.style != "none"

    translation = (
        registry.translate_thinking(provider_id, model_id, "high") if model.thinking else None
    )
    if model.thinking and scalar_style:
        assert translation is not None, f"{model_id}: 标量旋钮 high 档不可翻译"
    if model.thinking and not scalar_style:
        assert translation is None, f"{model_id}: 嵌套旋钮厂商不应产生翻译"

    thinking = "high" if (model.thinking and translation) else None
    if provider_id == "cli_session":
        # CLI 适配器不构造 HTTP 负载：校验订阅默认解析为不传 --model
        from council.adapters.cli_session import CliSessionAdapter

        cli_name = model_id.split(":", 1)[0]
        node = NodeSection(
            id="chk", adapter=provider_id, model=model_id, settings={"cli": cli_name}
        )
        adapter = CliSessionAdapter(node)
        assert adapter._model is None, f"{model_id} 应走登录态默认模型"
        return
    node = NodeSection(id="chk", adapter=provider_id, model=model_id, thinking=thinking)
    factory = _resolve_factory(node)
    assert factory is not None, f"{provider_id}: 适配器无法解析"
    adapter = factory(node)
    req = _request(model_id, thinking)

    if isinstance(adapter, OpenAIAdapter):
        payload = adapter._payload(req)
        assert payload["model"] == model_id
        assert payload.get("messages")
        if translation is not None and thinking:
            assert translation.param in payload, f"思考参数 {translation.param} 未出现"
            for extra_key in dict(translation.extra):
                assert extra_key in payload, f"伴生字段 {extra_key} 未合并"
    elif isinstance(adapter, AnthropicAdapter):
        payload = adapter._payload(req)
        assert "max_tokens" in payload
        if translation is not None and thinking:
            if spec.style == "enum_effort":
                # Claude 4.6+/5：adaptive thinking + output_config.effort
                assert (payload.get("thinking") or {}).get("type") == "adaptive"
                assert (payload.get("output_config") or {}).get("effort") == translation.value
                assert "budget_tokens" not in (payload.get("thinking") or {})
            else:
                block = payload.get("thinking") or {}
                assert block.get("budget_tokens") == translation.value
        else:
            assert "thinking" not in payload, "非思考模型不应携带 thinking 块"
    elif isinstance(adapter, GoogleAdapter):
        payload = adapter._payload(req)
        if translation is not None and thinking:
            config = (payload.get("generationConfig") or {}).get("thinkingConfig") or {}
            expected = "thinkingBudget" if spec.style == "thinking_budget" else "thinkingLevel"
            assert expected in config
    else:  # pragma: no cover - 防御新适配器类型
        pytest.fail(f"未预期的适配器类型 {type(adapter).__name__}")

    # 元数据完备性（自建 vllm 豁免 source）
    assert model.display, f"{model_id}: 缺 display"
    assert model.verified, f"{model_id}: 缺 verified 日期"
    assert provider_id == "vllm" or model.source, f"{model_id}: 缺 source 链接"
    assert (model.max_context_tokens or 0) > 0, f"{model_id}: max_context_tokens 非法"


def test_cli_subscription_defaults_resolve_to_no_model_flag() -> None:
    """订阅默认模型（空后缀）必须不向 CLI 传 --model。"""
    from council.adapters.cli_session import CliSessionAdapter

    for entry_id, cli in (("codex:", "codex"), ("claude:", "claude"), ("agy:", "agy")):
        node = NodeSection(id="chk", adapter="cli_session", model=entry_id, settings={"cli": cli})
        adapter = CliSessionAdapter(node)
        assert adapter._model is None, f"{entry_id} 应走登录态默认模型"


def test_registry_has_no_unknown_generation_slugs() -> None:
    """世代防伪回归：gpt-6 / 3.7-pro / 3.6-pro 等捏造 slug 永不存在。"""
    registry: Registry = CURATED
    for bad in ("gpt-6", "gemini-3.7-pro", "gemini-3.6-pro", "deepseek-v4-reasoner"):
        assert registry.model(bad) is None, bad

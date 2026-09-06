"""Registry: TOML is the only source of model knowledge."""

from __future__ import annotations

import pytest

from council.core.config import parse_config
from council.registry.loader import Registry, RegistryError


def test_builtin_providers_load() -> None:
    registry = Registry.load()
    assert {"openai_api", "anthropic_api", "google_api", "cli_session"} <= set(registry.providers)


def test_model_lookup() -> None:
    registry = Registry.load()
    model = registry.model("gpt-5.2")
    assert model is not None
    assert model.thinking is True
    assert model.verified == "2026-09-06"


def test_no_fabricated_generation_slugs() -> None:
    """GPT-6 家族仅 gpt-6-astra；Gemini Pro 线旗舰仍是 3.1——伪造世代一律不收录。"""
    registry = Registry.load()
    for bad in ("gpt-6", "gemini-3.7-pro", "gemini-3.6-pro"):
        assert registry.model(bad) is None, bad
    assert registry.model("gpt-6-astra") is not None
    assert registry.model("gemini-3.8-flash") is not None
    assert registry.model("glm-5.3") is not None
    assert registry.model("glm-5.3-flash") is not None


def test_thinking_translation_openai() -> None:
    registry = Registry.load()
    translation = registry.translate_thinking("openai_api", "gpt-5.2", "high")
    assert translation is not None
    assert translation.style == "enum_effort"
    assert translation.param == "reasoning_effort"
    assert translation.value == "high"


def test_thinking_translation_anthropic_budget() -> None:
    registry = Registry.load()
    translation = registry.translate_thinking("anthropic_api", "claude-sonnet-4-6", "medium")
    assert translation is not None
    assert translation.style == "budget_tokens"
    assert translation.value == 8192


def test_gemini_generations_use_different_knobs() -> None:
    registry = Registry.load()
    three = registry.translate_thinking("google_api", "gemini-3.5-flash", "high")
    two_five = registry.translate_thinking("google_api", "gemini-2.5-pro", "high")
    assert three is not None and three.style == "thinking_level" and three.value == "HIGH"
    assert two_five is not None and two_five.style == "thinking_budget"
    assert isinstance(two_five.value, int)


def test_unknown_level_returns_none() -> None:
    registry = Registry.load()
    assert registry.translate_thinking("openai_api", "gpt-5.2", "ultra") is None


def test_cli_has_no_thinking_control() -> None:
    registry = Registry.load()
    assert registry.translate_thinking("cli_session", None, "high") is None


def test_user_registry_overrides_builtin(tmp_path) -> None:
    (tmp_path / "openai.toml").write_text(
        """
[provider]
id = "openai_api"
display = "OpenAI"
base_url = "https://api.openai.com/v1"
secret_env = "OPENAI_API_KEY"
[provider.thinking]
style = "enum_effort"
param = "reasoning_effort"
[provider.thinking.levels]
low = "low"
medium = "medium"
high = "high"
[[models]]
id = "gpt-custom"
display = "Custom"
thinking = true
verified = "2026-09-02"
""",
        encoding="utf-8",
    )
    registry = Registry.load(user_dir=tmp_path)
    assert registry.model("gpt-custom") is not None
    assert registry.model("gpt-5.2") is not None  # builtin models survive


def test_user_registry_adds_a_new_provider(tmp_path) -> None:
    (tmp_path / "moonshot.toml").write_text(
        """
[provider]
id = "moonshot_api"
display = "Moonshot"
base_url = "https://api.moonshot.cn/v1"
secret_env = "MOONSHOT_API_KEY"
[[models]]
id = "kimi-k2"
thinking = false
""",
        encoding="utf-8",
    )
    registry = Registry.load(user_dir=tmp_path)
    assert registry.provider("moonshot_api") is not None
    assert registry.model("kimi-k2") is not None


def test_malformed_user_toml_raises(tmp_path) -> None:
    (tmp_path / "broken.toml").write_text("provider = [", encoding="utf-8")
    with pytest.raises(RegistryError, match="不是合法 TOML"):
        Registry.load(user_dir=tmp_path)


def test_unknown_thinking_style_rejected(tmp_path) -> None:
    (tmp_path / "bad.toml").write_text(
        """
[provider]
id = "x"
[provider.thinking]
style = "telepathy"
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="未知的 thinking style"):
        Registry.load(user_dir=tmp_path)


def test_validate_config_warns_on_unknown_model() -> None:
    registry = Registry.load()
    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "a", "adapter": "openai_api", "model": "made-up-model"},
                {
                    "id": "b",
                    "adapter": "google_api",
                    "model": "gemini-3.5-flash",
                    "thinking": "high",
                },
            ],
        }
    )
    warnings = registry.validate_config(config)
    assert any("made-up-model" in w for w in warnings)
    assert not any("gemini-3.5-flash" in w and "不支持" in w for w in warnings)


def test_validate_config_warns_on_unsupported_thinking() -> None:
    registry = Registry.load()
    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "a", "adapter": "openai_api", "model": "gpt-5.2", "thinking": "ultra"},
                {"id": "b", "adapter": "anthropic_api", "model": "claude-sonnet-4-6"},
            ],
        }
    )
    warnings = registry.validate_config(config)
    assert any("ultra" in w for w in warnings)


def test_validate_config_flags_cli_without_profile() -> None:
    registry = Registry.load()
    config = parse_config(
        {
            "council": {"active_nodes": 2},
            "nodes": [
                {"id": "a", "adapter": "cli_session", "model": "whatever"},
                {"id": "b", "adapter": "fake", "model": "fake/b"},
            ],
        }
    )
    warnings = registry.validate_config(config)
    assert any("settings.cli" in w for w in warnings)


# --------------------------------------------------- 2026-09-05 厂商扩展


def test_openai_compatible_vendors_load() -> None:
    """10 家新厂商全部数据驱动接入：一份 TOML，零 Python。"""
    registry = Registry.load()
    vendors = {
        "deepseek",
        "moonshot",
        "zhipu",
        "dashscope",
        "xai",
        "openrouter",
        "siliconflow",
        "ollama",
        "vllm",
        "meta",
    }
    assert vendors <= set(registry.providers)
    for vid in vendors:
        provider = registry.providers[vid]
        assert provider.adapter == "openai_api", vid
    # 厂商声明的 adapter 由工厂解析器兑现：deepseek 节点拿到 OpenAIAdapter
    from council.adapters import _resolve_factory
    from council.core.config import NodeSection

    node = NodeSection(id="a", adapter="deepseek", model="deepseek-v4-flash")
    assert _resolve_factory(node) is not None


def test_local_servers_need_no_secret() -> None:
    registry = Registry.load()
    for vid in ("ollama", "vllm"):
        provider = registry.providers[vid]
        assert provider.secret_required is False, vid
    # 云厂商仍然要求密钥
    assert registry.providers["deepseek"].secret_required is True


def test_deepseek_effort_maps_medium_to_high() -> None:
    """官方枚举为 low / high(默认) / max：medium 向上映射，high 顶到 max。"""
    registry = Registry.load()
    medium = registry.translate_thinking("deepseek", "deepseek-v4-flash", "medium")
    high = registry.translate_thinking("deepseek", "deepseek-v4-flash", "high")
    assert medium is not None and medium.value == "high"
    assert high is not None and high.value == "max"


def test_dashscope_thinking_budget_carries_extra() -> None:
    registry = Registry.load()
    translation = registry.translate_thinking("dashscope", "qwen3.8-max", "medium")
    assert translation is not None
    assert translation.style == "thinking_budget"
    assert translation.param == "thinking_budget"
    assert isinstance(translation.value, int)
    assert dict(translation.extra).get("enable_thinking") is True


def test_ollama_effort_is_native() -> None:
    registry = Registry.load()
    translation = registry.translate_thinking("ollama", "qwen3:32b", "high")
    assert translation is not None
    assert translation.style == "enum_effort"
    assert translation.param == "think"
    assert translation.value == "high"


def test_nested_object_knobs_stay_none() -> None:
    """GLM / Kimi / OpenRouter / vLLM 的思考开关是嵌套对象，旋钮不翻译。"""
    registry = Registry.load()
    for provider_id, model_id in (
        ("zhipu", "glm-5.2"),
        ("moonshot", "kimi-k3"),
        ("openrouter", "openai/gpt-6-astra"),
        ("vllm", "qwen3-32b"),
    ):
        assert registry.translate_thinking(provider_id, model_id, "high") is None, provider_id

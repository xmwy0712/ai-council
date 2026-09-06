"""全量模型干跑校验：注册表中每个模型都必须能构造出合法的调用负载。

离线运行（不发任何网络请求）：为每个 (厂商, 模型) 真实实例化适配器、
构造 ChatRequest（含思考档位）并生成请求负载，验证——
  1. 适配器可解析（含 provider.adapter 委托）；
  2. 负载结构合法（model id / 消息体 / 关键字段）；
  3. 思考旋钮按注册表声明出现在负载里（标量旋钮必须可翻译）；
  4. 元数据完备（display / verified / source，自建端点豁免 source）。
退出码非 0 表示存在不可调用或数据不全的条目。
"""

from __future__ import annotations

import os
import sys

# 干跑所需密钥：全部注入占位值（真实调用会在鉴权处被拒，但负载构造不受影响）
_PRESET_KEYS = [
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
for _key in _PRESET_KEYS:
    os.environ.setdefault(_key, "sk-dry-run-placeholder")

from council.adapters import _resolve_factory  # noqa: E402
from council.adapters.anthropic_api import AnthropicAdapter  # noqa: E402
from council.adapters.cli_session import CliSessionAdapter  # noqa: E402
from council.adapters.google_api import GoogleAdapter  # noqa: E402
from council.adapters.openai_api import OpenAIAdapter  # noqa: E402
from council.core.config import NodeSection  # noqa: E402
from council.core.contracts import ChatMessage, ChatRequest, ResponseFormat, Role  # noqa: E402
from council.registry import get_registry  # noqa: E402


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


def _openai_node(provider_id: str, model_id: str, thinking: str | None = None) -> NodeSection:
    return NodeSection(id="chk", adapter=provider_id, model=model_id, thinking=thinking)


def check_model(provider_id: str, model_id: str) -> list[str]:
    issues: list[str] = []
    registry = get_registry()
    model = registry.model(model_id)
    if model is None:
        return ["模型不在注册表"]
    spec = registry.thinking_spec(provider_id, model_id)

    node = _openai_node(provider_id, model_id)
    factory = _resolve_factory(node)
    if factory is None:
        adapter_ref = registry.providers[provider_id].adapter
        return [f"适配器无法解析（provider.adapter={adapter_ref!r}）"]
    adapter = factory(node)

    # --- 思考旋钮一致性：声明可思考的模型，标量旋钮必须能翻译出值
    translation = (
        registry.translate_thinking(provider_id, model_id, "high") if model.thinking else None
    )
    scalar_style = spec.style not in ("none",)
    if model.thinking and scalar_style and translation is None:
        issues.append("thinking=true 且为标量旋钮，但 high 档翻译为 None")
    # 嵌套旋钮厂商：翻译必须是 None（交由服务端默认）
    if model.thinking and not scalar_style and translation is not None:
        issues.append("嵌套旋钮厂商不应产生翻译")

    thinking = "high" if (model.thinking and translation) else None
    # 适配器实例在构造时读取节点思考档位，必须与请求一致
    node = _openai_node(provider_id, model_id, thinking)
    adapter = factory(node)

    req = _request(model_id, thinking)

    # --- 按适配器类型校验负载
    if isinstance(adapter, OpenAIAdapter):
        payload = adapter._payload(req)
        if payload.get("model") != model_id:
            issues.append(f"负载 model={payload.get('model')!r} 与注册表不一致")
        if "messages" not in payload:
            issues.append("负载缺少 messages")
        if translation is not None and req.thinking:
            if translation.param and translation.param not in payload:
                issues.append(f"思考参数 {translation.param!r} 未出现在负载中")
            for extra_key in dict(translation.extra):
                if extra_key not in payload:
                    issues.append(f"伴生字段 {extra_key!r} 未合并进负载")
    elif isinstance(adapter, AnthropicAdapter):
        payload = adapter._payload(req)
        if "max_tokens" not in payload:
            issues.append("Anthropic 负载缺少必填 max_tokens")
        if translation is not None and req.thinking:
            block = payload.get("thinking") or {}
            if block.get("budget_tokens") != translation.value:
                issues.append(f"thinking.budget_tokens={block.get('budget_tokens')!r} 与档位不符")
        elif req.thinking:
            issues.append("Anthropic 非思考模型不应携带 thinking 块")
    elif isinstance(adapter, GoogleAdapter):
        payload = adapter._payload(req)
        generation = payload.get("generationConfig") or {}
        if translation is not None and req.thinking:
            config = generation.get("thinkingConfig") or {}
            expected = "thinkingBudget" if spec.style == "thinking_budget" else "thinkingLevel"
            if expected not in config:
                issues.append(f"generationConfig.thinkingConfig 缺少 {expected}")
    else:
        issues.append(f"未预期的适配器类型 {type(adapter).__name__}")

    # --- 元数据完备性
    if not model.display:
        issues.append("缺少 display")
    if not model.verified:
        issues.append("缺少 verified 日期")
    if provider_id != "vllm" and not model.source:
        issues.append("缺少 source 链接（自建 vllm 豁免）")
    if (model.max_context_tokens or 0) <= 0:
        issues.append("max_context_tokens 非法")
    return issues


def check_cli_profiles() -> list[str]:
    """cli_session：订阅默认模型（空后缀）必须解析为 model=None（不传 --model）。"""
    issues: list[str] = []
    for entry_id, cli in (("codex:", "codex"), ("claude:", "claude"), ("agy:", "agy")):
        node = NodeSection(id="chk", adapter="cli_session", model=entry_id, settings={"cli": cli})
        try:
            adapter = CliSessionAdapter(node)
            if adapter._model is not None:
                issues.append(f"{entry_id} 应解析为订阅默认（None），实际 {adapter._model!r}")
        except Exception as err:
            issues.append(f"{entry_id} 构造失败：{err}")
    return issues


def main() -> int:
    registry = get_registry(reload=True)
    total = 0
    failures = 0
    print(f"{'厂商':<14}{'模型':<36}结果")
    print("-" * 72)
    for provider_id in sorted(registry.providers):
        provider = registry.providers[provider_id]
        if provider_id == "fake":
            continue
        for model_id in provider.models:
            total += 1
            label = f"{provider_id:<14}{model_id:<36}"
            if provider_id == "cli_session":
                issues = check_cli_profiles()
            else:
                issues = check_model(provider_id, model_id)
            if issues:
                failures += 1
                print(f"{label}✗ {'；'.join(issues)}")
            else:
                print(f"{label}✓")

    # cli_session 三个订阅默认条目的独立校验
    cli_issues = check_cli_profiles()
    if cli_issues:
        failures += 1
        print(f"{'cli_session':<14}{'订阅默认模型':<36}✗ {'；'.join(cli_issues)}")

    print("-" * 72)
    print(f"共 {total} 个模型，{failures} 个存在问题")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

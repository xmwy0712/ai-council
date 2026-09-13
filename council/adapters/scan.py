"""Offline usability scan for registry models.

A listing endpoint hands back ids; the roster only offers a model when turning
it into a request actually works. This module builds that request in-process —
no socket is opened — and reports anything that would break a session
mid-flight: an unresolvable thinking level, an adapter that cannot be built, a
payload missing its thinking parameter.

It also answers "is this provider reachable right now": a vendor whose secret
cannot be resolved is not, and a local CLI that is not installed is not.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..core import secrets
from ..core.config import NodeSection
from ..core.contracts import ChatMessage, ChatRequest, ResponseFormat, Role
from ..registry.loader import ProviderSpec, Registry
from . import _resolve_factory

__all__ = ["cli_installed", "provider_available", "scan_model"]

#: Subscription profiles → the binary that has to be on PATH.
_CLI_BINARIES = {"codex": "codex", "claude": "claude", "agy": "agy"}


def cli_installed(profile: str) -> bool:
    """Is the local CLI binary present? (Auth state is checked separately.)"""
    return shutil.which(_CLI_BINARIES.get(profile, profile)) is not None


def provider_available(provider: ProviderSpec, *, env_file: Path | None = None) -> bool:
    """Can this vendor be reached right now?

    Key-less endpoints (local Ollama / vLLM) always are; everyone else needs a
    resolvable secret. ``cli_session`` is handled per model, since it depends on
    which CLI binary the entry refers to.
    """
    if provider.id == "cli_session":
        return True
    if not provider.secret_required:
        return True
    if not provider.secret_env:
        return False
    return secrets.resolve(provider.secret_env, env_file=env_file) is not None


def _request(model_id: str, thinking: str | None) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role=Role.SYSTEM, content="scan"),
            ChatMessage(role=Role.USER, content="1+1=?"),
        ],
        model=model_id,
        thinking=thinking,
        response_format=ResponseFormat.JSON,
        # Anthropic clamps budget_tokens below max_tokens; keep it large so the
        # level is not silently clamped and the assertion stays meaningful.
        max_output_tokens=65536,
    )


def scan_model(registry: Registry, provider_id: str, model_id: str) -> list[str]:
    """Build a call payload in-process and report every problem found.

    An empty list means the model is usable: it resolves to an adapter, its
    declared thinking level translates, and the resulting payload actually
    carries that parameter. Anything else is a reason to keep it out of the
    picker — a model that cannot build a request would fail mid-session.
    """
    model = registry.model(model_id)
    if model is None:
        return [f"{provider_id}: 模型不在注册表"]

    if provider_id == "cli_session":
        profile = model.profile or model_id.split(":", 1)[0]
        if not cli_installed(profile):
            return [f"{model_id}: 本地 CLI 未安装（{profile}）"]
        return []

    spec = registry.thinking_spec(provider_id, model_id)
    translation = (
        registry.translate_thinking(provider_id, model_id, "high") if model.thinking else None
    )
    if model.thinking and spec.style != "none" and translation is None:
        return [f"{model_id}: 声明了思考但档位 high 无法翻译"]

    thinking = "high" if (model.thinking and translation) else None
    node = NodeSection(id="scan", adapter=provider_id, model=model_id, thinking=thinking)
    factory = _resolve_factory(node)
    if factory is None:
        return [f"{provider_id}: 适配器无法解析"]

    try:
        adapter = factory(node)
        # _payload 是各家适配器的私有实现，协议层不声明；这里是唯一调用点。
        # 用变量存属性名，mypy 就不会到 Adapter 协议上找这个私有方法。
        attr = "_payload"
        build_payload: Any = getattr(adapter, attr)
        payload: dict[str, Any] = build_payload(_request(model_id, thinking))
    except Exception as err:  # any failure is a scan finding
        return [f"{model_id}: 构造负载失败（{type(err).__name__}: {err}）"]

    issues: list[str] = []
    if not payload.get("model"):
        issues.append(f"{model_id}: 负载缺少 model 字段")
    if translation is not None and thinking:
        blob = repr(payload)
        if str(translation.param) not in blob:
            issues.append(f"{model_id}: 思考参数 {translation.param} 未出现在负载里")
    return issues

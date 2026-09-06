"""Data-driven model registry.

Models and thinking levels live in TOML, never in business logic. Built-in
files ship in ``council/registry/*.toml``; users drop additional or overriding
files in ``<data_dir>/registry/*.toml`` — a user file with the same provider id
merges over the built-in one, and a model with the same id replaces the
built-in entry.

Two things are deliberately *not* here: CLI read-only flags (those are safety
code, not user-editable data) and prices (they change weekly and we will not
ship unverified numbers; cost stays ``None`` until someone fills verified
values, see docs/PROVIDERS.md).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from ..core.paths import data_dir

__all__ = [
    "ModelSpec",
    "ProviderSpec",
    "Registry",
    "RegistryError",
    "ThinkingExtra",
    "ThinkingSpec",
    "ThinkingTranslation",
    "get_registry",
]

_KNOWN_THINKING_STYLES: Final = frozenset(
    {"enum_effort", "budget_tokens", "thinking_budget", "thinking_level", "none"}
)

# 伴生字段：某些厂商的思考开关需要同时发送一个固定伙伴参数
# （例如阿里 DashScope 的 enable_thinking），它们不属于「档位→值」的映射，
# 因此单独挂在 ThinkingSpec.extra 上随请求一起发出。
ThinkingExtra: TypeAlias = MappingProxyType[str, Any]


class RegistryError(RuntimeError):
    """A registry TOML file is malformed or references something unknown."""


@dataclass(frozen=True)
class ThinkingSpec:
    """How an abstract level ("low"/"medium"/"high") maps to a provider knob."""

    style: str = "none"
    param: str = ""
    levels: MappingProxyType[str, str | int] = field(default_factory=lambda: MappingProxyType({}))
    extra: ThinkingExtra = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ThinkingTranslation:
    style: str
    param: str
    value: str | int
    extra: ThinkingExtra = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    display: str = ""
    thinking: bool = False
    structured_output: bool = True
    streaming: bool = True
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    price_input_usd: float | None = None
    price_output_usd: float | None = None
    thinking_style: ThinkingSpec | None = None  # per-model override
    profile: str = ""  # cli_session profile (codex / claude / agy)
    verified: str = ""
    source: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display: str = ""
    base_url: str | None = None
    secret_env: str | None = None
    docs: str = ""
    thinking: ThinkingSpec = field(default_factory=ThinkingSpec)
    models: MappingProxyType[str, ModelSpec] = field(default_factory=lambda: MappingProxyType({}))
    # 本地/自建端点（Ollama、vLLM 等）不需要密钥
    secret_required: bool = True
    # 声明「本厂商用哪个适配器协议实现」——加上它，
    # 新增一家 OpenAI 兼容厂商就只需一份 TOML，零 Python 代码。
    adapter: str = ""


def _extra_from(raw: Any) -> ThinkingExtra:
    """伴生字段表：随思考参数一起发出的固定伙伴参数。"""
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, dict):
        raise RegistryError("thinking.extra 必须是一张表")
    return MappingProxyType({str(key): value for key, value in raw.items()})


def _thinking_from(raw: dict[str, Any]) -> ThinkingSpec:
    style = str(raw.get("style", "none"))
    if style not in _KNOWN_THINKING_STYLES:
        raise RegistryError(f"未知的 thinking style：{style!r}")
    levels_raw = raw.get("levels") or {}
    if not isinstance(levels_raw, dict):
        raise RegistryError("thinking.levels 必须是一张表")
    levels: dict[str, str | int] = {}
    for key, value in levels_raw.items():
        if not isinstance(value, (str, int)):
            raise RegistryError(f"thinking.levels[{key!r}] 必须是字符串或整数")
        levels[str(key)] = value
    return ThinkingSpec(
        style=style,
        param=str(raw.get("param", "")),
        levels=MappingProxyType(levels),
        extra=_extra_from(raw.get("extra")),
    )


def _model_from(raw: dict[str, Any], provider: str) -> ModelSpec:
    model_id = str(raw.get("id", "")).strip()
    if not model_id:
        raise RegistryError(f"provider {provider} 存在缺 id 的模型条目")
    override: ThinkingSpec | None = None
    if "thinking_style" in raw:
        override = _thinking_from(
            {
                "style": raw["thinking_style"],
                "param": raw.get("param", ""),
                "levels": raw.get("levels") or {},
                "extra": raw.get("extra"),
            }
        )
    return ModelSpec(
        id=model_id,
        provider=provider,
        display=str(raw.get("display", model_id)),
        thinking=bool(raw.get("thinking", False)),
        structured_output=bool(raw.get("structured_output", True)),
        streaming=bool(raw.get("streaming", True)),
        max_context_tokens=(
            int(raw["max_context_tokens"]) if "max_context_tokens" in raw else None
        ),
        max_output_tokens=(int(raw["max_output_tokens"]) if "max_output_tokens" in raw else None),
        price_input_usd=(float(raw["price_input_usd"]) if "price_input_usd" in raw else None),
        price_output_usd=(float(raw["price_output_usd"]) if "price_output_usd" in raw else None),
        thinking_style=override,
        profile=str(raw.get("profile", "")),
        verified=str(raw.get("verified", "")),
        source=str(raw.get("source", "")),
    )


def _provider_from(raw: dict[str, Any], *, origin: str) -> ProviderSpec:
    meta = raw.get("provider")
    if not isinstance(meta, dict) or not meta.get("id"):
        raise RegistryError(f"{origin}: 缺少 [provider] id")
    provider_id = str(meta["id"])
    thinking = _thinking_from(meta.get("thinking") or {})
    models: dict[str, ModelSpec] = {}
    for entry in raw.get("models") or []:
        if not isinstance(entry, dict):
            raise RegistryError(f"{origin}: [[models]] 条目必须是表")
        model = _model_from(entry, provider_id)
        models[model.id] = model
    return ProviderSpec(
        id=provider_id,
        display=str(meta.get("display", provider_id)),
        base_url=str(meta["base_url"]) if meta.get("base_url") else None,
        secret_env=str(meta["secret_env"]) if meta.get("secret_env") else None,
        docs=str(meta.get("docs", "")),
        thinking=thinking,
        models=MappingProxyType(models),
        secret_required=bool(meta.get("secret_required", True)),
        adapter=str(meta.get("adapter", "")),
    )


class Registry:
    def __init__(self, providers: dict[str, ProviderSpec]) -> None:
        self.providers: MappingProxyType[str, ProviderSpec] = MappingProxyType(providers)
        models: dict[str, ModelSpec] = {}
        for provider in providers.values():
            models.update(provider.models)
        self.models: MappingProxyType[str, ModelSpec] = MappingProxyType(models)

    # --------------------------------------------------------------- loading

    @classmethod
    def load(cls, user_dir: Path | None = None) -> Registry:
        merged: dict[str, dict[str, Any]] = {}
        for name, data in cls._load_dir(None):
            merged[name] = data
        extra = user_dir if user_dir is not None else data_dir() / "registry"
        for name, data in cls._load_dir(extra):
            if name in merged:
                merged[name] = _deep_merge(merged[name], data)
            else:
                merged[name] = data
        providers: dict[str, ProviderSpec] = {}
        for name, data in merged.items():
            spec = _provider_from(data, origin=name)
            providers[spec.id] = spec
        return cls(providers)

    @staticmethod
    def _load_dir(root: Path | None) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if root is None:
            package = files("council.registry")
            for item in sorted(package.iterdir()):
                if item.name.endswith(".toml"):
                    out.append((item.name, tomllib.loads(item.read_text(encoding="utf-8"))))
            return out
        if not root.is_dir():
            return out
        for item in sorted(root.glob("*.toml")):
            try:
                out.append((item.name, tomllib.loads(item.read_text(encoding="utf-8"))))
            except tomllib.TOMLDecodeError as err:
                raise RegistryError(f"注册表 {item} 不是合法 TOML：{err}") from err
        return out

    # --------------------------------------------------------------- lookups

    def provider(self, provider_id: str) -> ProviderSpec | None:
        return self.providers.get(provider_id)

    def model(self, model_id: str) -> ModelSpec | None:
        return self.models.get(model_id)

    def thinking_spec(self, provider_id: str, model_id: str | None) -> ThinkingSpec:
        provider = self.providers.get(provider_id)
        if provider is None:
            return ThinkingSpec()
        if model_id is not None:
            model = provider.models.get(model_id)
            if model is not None and model.thinking_style is not None:
                return model.thinking_style
        return provider.thinking

    def translate_thinking(
        self, provider_id: str, model_id: str | None, level: str
    ) -> ThinkingTranslation | None:
        """Map an abstract level to a provider value. ``None`` = no thinking control."""
        spec = self.thinking_spec(provider_id, model_id)
        if spec.style == "none":
            return None
        value = spec.levels.get(level)
        if value is None:
            return None
        return ThinkingTranslation(
            style=spec.style, param=spec.param, value=value, extra=spec.extra
        )

    # ------------------------------------------------------------ validation

    def validate_config(self, config: Any) -> list[str]:
        """Soft warnings — unknown models are allowed (users add them to TOML),
        but they deserve a visible note instead of a silent shrug."""
        from ..core.config import NodeSection  # local import: registry must not

        warnings: list[str] = []
        for node in config.nodes:
            assert isinstance(node, NodeSection)
            provider = self.providers.get(node.adapter)
            model = self.models.get(node.model)
            if node.adapter == "cli_session":
                profile = model.profile if model else ""
                if not profile and node.settings.get("cli") not in ("codex", "claude", "agy"):
                    warnings.append(
                        f"节点 {node.id} 的 cli_session 需要 nodes.settings.cli ∈ codex/claude/agy"
                    )
                continue
            if provider is None:
                warnings.append(f"节点 {node.id} 的适配器 {node.adapter!r} 不在注册表中")
                continue
            if model is None:
                warnings.append(
                    f"节点 {node.id} 的模型 {node.model!r} 不在注册表（能力信息缺失，"
                    f"思考等级可能被忽略）"
                )
                continue
            if node.thinking and not model.thinking:
                warnings.append(
                    f"模型 {model.id} 不支持思考等级，节点 {node.id} 的 thinking 设置将被忽略"
                )
            elif (
                node.thinking
                and model.thinking
                and self.translate_thinking(node.adapter, node.model, node.thinking) is None
            ):
                warnings.append(f"模型 {model.id} 不支持思考等级 {node.thinking!r}")
        return warnings


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key == "models" and isinstance(value, list) and isinstance(out.get(key), list):
            by_id = {m.get("id"): m for m in out[key] if isinstance(m, dict)}
            for entry in value:
                if isinstance(entry, dict):
                    by_id[entry.get("id")] = entry
            out[key] = list(by_id.values())
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


_registry: Registry | None = None


def get_registry(*, reload: bool = False) -> Registry:
    global _registry
    if _registry is None or reload:
        _registry = Registry.load()
    return _registry

"""Data-driven model registry.

Models and thinking levels live in TOML, never in business logic. Built-in
files ship in ``council/registry/*.toml``; users drop additional or overriding
files in ``<data_dir>/registry/*.toml``.

Three layers, and the precedence is deliberate:

1. built-in (``council/registry/*.toml``) — the curated, doc-verified baseline
2. user (``<data_dir>/registry/*.toml``) — a same-named file merges over the
   built-in one, a same-id model replaces the built-in entry
3. discovered (``<data_dir>/registry/discovered/*.toml``) — written by
   :mod:`council.registry.discovery`. These only ever *fill gaps*: an id that
   already exists in layer 1 or 2 is ignored.

Layer 3 losing to layer 2 and layer 1 is the whole point. Discovery knows
nothing but model ids and a few numbers; the built-ins carry verified thinking
protocols, and a wrong one is a hard 400. If discovery could win, a file
written months ago would silently strip the capabilities of a model the
registry has since learned about properly. Making it structural rather than
"we re-filter on write" means a stale overlay cannot shadow curated data even
if it is never rewritten again.

Two things are deliberately *not* here: CLI read-only flags (those are safety
code, not user-editable data) and prices (they change weekly and we will not
ship unverified numbers; cost stays ``None`` until someone fills verified
values, see docs/PROVIDERS.md).
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from ..core.paths import data_dir

__all__ = [
    "AUTO_DIR_NAME",
    "ModelSpec",
    "ProviderSpec",
    "Registry",
    "RegistryError",
    "ThinkingExtra",
    "ThinkingSpec",
    "ThinkingTranslation",
    "get_registry",
    "registry_at",
    "registry_root",
    "use_registry_root",
]

#: Sub-directory of the user registry that auto-discovery owns. Kept separate
#: from hand-written files so that layer 3 can never outrank layer 2.
AUTO_DIR_NAME: Final = "discovered"


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
    # 推理模型（GPT-5.6/6 等）不接受 temperature 参数
    omit_temperature: bool = False
    #: Written by online discovery. The UI keeps these in a separate group so
    #: an auto-found id never masquerades as a curated one.
    discovered: bool = False


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
    # 部分思考模型（如 Moonshot K 系）固定 temperature=1.0，
    # 传其他值直接 400 —— 声明后适配器省略 temperature
    omit_temperature: bool = False
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
        omit_temperature=bool(raw.get("omit_temperature", False)),
        discovered=bool(raw.get("discovered", False)),
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
        omit_temperature=bool(meta.get("omit_temperature", False)),
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
    def load(cls, user_dir: Path | None = None, *, include_discovered: bool = True) -> Registry:
        """Build the registry. ``include_discovered=False`` yields the curated
        baseline only — which is what discovery needs in order to tell "new to
        the user" apart from "new to the world"."""
        # Accumulate by *declared provider id*, not by filename: two files that
        # name the same provider must layer onto each other. Keying by filename
        # meant a stray ``my-openai.toml`` replaced the entire provider — every
        # curated model and every discovered id gone — instead of merging.
        merged: dict[str, dict[str, Any]] = {}
        origins: dict[str, str] = {}
        for name, data in cls._load_dir(None):
            _accumulate(merged, origins, data, name, _deep_merge)
        extra = user_dir if user_dir is not None else data_dir() / "registry"
        for name, data in cls._load_dir(extra):
            _accumulate(merged, origins, data, name, _deep_merge)
        # Layer 3: auto-discovered ids, gap-filling only. `_load_dir` globs
        # ``*.toml``, so the sub-directory is naturally invisible to layer 2.
        if include_discovered:
            for name, data in cls._load_dir(extra / AUTO_DIR_NAME):
                _accumulate(merged, origins, data, name, _merge_missing)
        providers: dict[str, ProviderSpec] = {}
        for key, data in merged.items():
            spec = _provider_from(data, origin=origins[key])
            providers[spec.id] = spec
        return cls(providers)

    @staticmethod
    def _load_dir(root: Path | None) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if root is None:
            package = files("council.registry")
            # `Traversable` is not orderable, so sort by name explicitly.
            for item in sorted(package.iterdir(), key=lambda entry: entry.name):
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


def _declared_id(data: dict[str, Any]) -> str:
    meta = data.get("provider")
    if isinstance(meta, dict) and meta.get("id"):
        return str(meta["id"])
    return ""


def _accumulate(
    target: dict[str, dict[str, Any]],
    origins: dict[str, str],
    data: dict[str, Any],
    name: str,
    merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> None:
    """Fold one file into the running picture, grouped by declared provider id.

    A file that declares no ``[provider] id`` keeps its filename as the key so
    that :func:`_provider_from` still gets to raise the readable error, instead
    of the file quietly disappearing.
    """
    provider_id = _declared_id(data)
    key = provider_id or name
    if key in target:
        target[key] = merge(target[key], data)
    else:
        target[key] = data
        origins[key] = name


def _merge_missing(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Supply only what ``base`` lacks. Auto-discovery never outranks curation.

    Provider-level scalars are filled in only when absent; ``models`` entries
    are appended only for ids that do not already exist. Anything already
    curated wins unconditionally — that is the guarantee that makes a stale
    overlay harmless.
    """
    out = dict(base)
    models = list(out.get("models") or [])
    known = {entry.get("id") for entry in models if isinstance(entry, dict)}
    for entry in extra.get("models") or []:
        if isinstance(entry, dict) and entry.get("id") not in known:
            models.append(entry)
            known.add(entry.get("id"))
    if models:
        out["models"] = models
    for key, value in extra.items():
        if key == "models":
            continue
        if key == "provider" and isinstance(value, dict):
            base_meta = out.get("provider")
            if not isinstance(base_meta, dict):
                out["provider"] = dict(value)
            else:
                merged_meta = dict(base_meta)
                for meta_key, meta_value in value.items():
                    merged_meta.setdefault(meta_key, meta_value)
                out["provider"] = merged_meta
        elif key not in out:
            out[key] = value
    return out


_registry_cache: dict[Path, Registry] = {}
#: Set when the data directory is overridden (`--data-dir`). The config, the
#: session store and the discovered overlays all move together; the registry has
#: to move with them, or an overlay ends up written somewhere nothing reads.
_root_override: Path | None = None


def registry_root() -> Path:
    """Where the process-wide registry is read from right now."""
    return _root_override if _root_override is not None else data_dir() / "registry"


def use_registry_root(root: Path | None) -> None:
    """Pin the process-wide registry to ``root`` (``None`` restores the default)."""
    global _root_override
    _root_override = root


@contextmanager
def registry_at(root: Path | None) -> Iterator[None]:
    """Scoped :func:`use_registry_root`, for the web app's lifetime.

    Scoped rather than plain-set on purpose: a test suite that starts several
    servers must not leave the last one's data directory pinned for the next.
    """
    global _root_override
    previous = _root_override
    _root_override = root
    try:
        yield
    finally:
        _root_override = previous


def get_registry(*, reload: bool = False) -> Registry:
    """The cached registry for the current root.

    Keyed by root rather than held in a single slot, so switching data
    directories does not throw away the other root's parse.
    """
    root = registry_root()
    if reload or root not in _registry_cache:
        _registry_cache[root] = Registry.load(root)
    return _registry_cache[root]

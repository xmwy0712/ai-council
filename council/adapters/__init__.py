"""Adapter registry.

Adding a vendor is meant to cost exactly one new module plus one registry TOML.
Nothing in ``council.core`` changes.
"""

from __future__ import annotations

from collections.abc import Callable

from ..core.config import Config, ConfigError, NodeSection
from ..core.contracts import Adapter
from .anthropic_api import AnthropicAdapter
from .cli_session import CliSessionAdapter
from .fake import FakeAdapter
from .generic_http import GenericHttpAdapter
from .google_api import GoogleAdapter
from .openai_api import OpenAIAdapter

__all__ = ["AdapterFactory", "build_adapters", "register"]

AdapterFactory = Callable[[NodeSection], Adapter]

_FACTORIES: dict[str, AdapterFactory] = {}


def register(name: str, factory: AdapterFactory) -> None:
    _FACTORIES[name] = factory


register("fake", lambda node: FakeAdapter(node.id, model=node.model))
register("openai_api", lambda node: OpenAIAdapter(node))
register("anthropic_api", lambda node: AnthropicAdapter(node))
register("google_api", lambda node: GoogleAdapter(node))
register("cli_session", lambda node: CliSessionAdapter(node))
register("generic_http", lambda node: GenericHttpAdapter(node))


def build_adapters(config: Config) -> dict[str, Adapter]:
    """Instantiate one adapter per configured node."""
    adapters: dict[str, Adapter] = {}
    for node in config.nodes:
        if not node.enabled:
            continue
        factory = _FACTORIES.get(node.adapter)
        if factory is None:
            known = ", ".join(sorted(_FACTORIES)) or "(none)"
            raise ConfigError(
                f"节点 {node.id!r} 引用了未注册的适配器 {node.adapter!r}；已知适配器：{known}"
            )
        adapters[node.id] = factory(node)
    return adapters

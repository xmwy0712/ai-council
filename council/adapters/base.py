"""Shared helpers for adapters.

Everything here is pure configuration resolution: adapters and the engine must
agree on how a node overrides a global knob, and there is exactly one place
where that is decided.
"""

from __future__ import annotations

from ..core.config import Config, NodeSection
from ..core.contracts import TimeoutSpec

__all__ = [
    "resolve_max_output_tokens",
    "resolve_max_retries",
    "resolve_temperature",
    "resolve_thinking",
    "resolve_timeout",
]


def resolve_timeout(node: NodeSection, config: Config) -> TimeoutSpec:
    over = node.overrides
    return TimeoutSpec(
        connect_s=over.connect_s if over.connect_s is not None else config.timeout.connect_s,
        idle_s=over.idle_s if over.idle_s is not None else config.timeout.idle_s,
        total_s=over.total_s if over.total_s is not None else config.timeout.total_s,
    )


def resolve_temperature(node: NodeSection, config: Config) -> float:
    return (
        node.overrides.temperature
        if node.overrides.temperature is not None
        else config.budget.temperature
    )


def resolve_max_output_tokens(node: NodeSection, config: Config) -> int:
    return (
        node.overrides.max_output_tokens
        if node.overrides.max_output_tokens is not None
        else config.budget.max_output_tokens
    )


def resolve_thinking(node: NodeSection, config: Config) -> str | None:
    del config
    return node.overrides.thinking if node.overrides.thinking is not None else node.thinking


def resolve_max_retries(node: NodeSection, config: Config) -> int:
    return (
        node.overrides.max_retries
        if node.overrides.max_retries is not None
        else config.failure.max_retries
    )

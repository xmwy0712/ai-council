"""Thinking-level resolution for discovered models.

The rule under test: a newly discovered model may claim a thinking control only
when a *per-model* signal says it supports one, and the knob (style / param /
levels) always comes from something the registry already declares. No signal, or
an ambiguous knob, means "claim nothing" — the model stays selectable, it just
offers no thinking control until someone verifies one.
"""

from __future__ import annotations

from types import MappingProxyType

import httpx
import pytest
from test_discovery import _handler, _registry, _run, _spec

from council.registry.discovery import (
    CatalogEntry,
    DiscoveredModel,
    DiscoveryOptions,
    resolve_thinking,
)
from council.registry.loader import Registry, ThinkingSpec


def _effort_spec() -> ThinkingSpec:
    return ThinkingSpec(
        style="enum_effort",
        param="reasoning_effort",
        levels=MappingProxyType({"none": "none", "low": "low", "medium": "medium"}),
    )


def _budget_spec() -> ThinkingSpec:
    return ThinkingSpec(
        style="thinking_budget",
        param="thinking_budget",
        levels=MappingProxyType({"low": 1024, "medium": 8192, "high": 32768}),
        extra=MappingProxyType({"enable_thinking": True}),
    )


# --------------------------------------------------------- vendor-native signal


def test_anthropic_effort_levels_are_taken_verbatim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``capabilities.effort`` is the vendor naming its own protocol."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-fable-6",
                        "display_name": "Claude Fable 6",
                        "capabilities": {
                            "thinking": {"supported": True, "types": {"adaptive": {}}},
                            "effort": {
                                "supported": True,
                                "low": True,
                                "medium": True,
                                "high": True,
                                "xhigh": True,
                                "max": True,
                            },
                        },
                    }
                ],
                "has_more": False,
            },
        )

    monkeypatch.setenv("TEST_KEY", "sk-test")
    from council.registry.discovery import discover

    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec(
                "anthropic_api",
                adapter="",
                secret_env="TEST_KEY",
                thinking=_budget_spec(),
            )
        ),
        transport=httpx.MockTransport(handle),
    )
    plan = report.providers[0].new[0].thinking
    assert plan is not None
    assert (plan.style, plan.param) == ("enum_effort", "effort")
    assert plan.level_names == ("low", "medium", "high", "xhigh", "max")


def test_anthropic_without_effort_inherits_the_budget_protocol(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4.x-era model takes budget_tokens; the provider block already says so."""
    routes = {
        "vendor.test/v1/models": {
            "data": [
                {
                    "id": "claude-sonnet-4-7",
                    "capabilities": {"thinking": {"supported": True, "types": {"enabled": {}}}},
                }
            ],
            "has_more": False,
        }
    }
    monkeypatch.setenv("TEST_KEY", "sk-test")
    from council.registry.discovery import discover

    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec(
                "anthropic_api",
                adapter="",
                base_url="https://vendor.test",
                secret_env="TEST_KEY",
                thinking=_budget_spec(),
            )
        ),
        transport=_handler(routes),
    )
    plan = report.providers[0].new[0].thinking
    assert plan is not None
    assert plan.style == "thinking_budget"
    assert dict(plan.levels) == {"low": 1024, "medium": 8192, "high": 32768}
    assert dict(plan.extra) == {"enable_thinking": True}


def test_anthropic_without_capabilities_claims_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No capability block at all is not a licence to assume support."""
    routes = {"vendor.test/v1/models": {"data": [{"id": "claude-mystery-9"}], "has_more": False}}
    monkeypatch.setenv("TEST_KEY", "sk-test")
    from council.registry.discovery import discover

    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec(
                "anthropic_api",
                adapter="",
                base_url="https://vendor.test",
                secret_env="TEST_KEY",
                thinking=_budget_spec(),
            )
        ),
        transport=_handler(routes),
    )
    assert report.providers[0].new[0].thinking is None


# ---------------------------------------------------------- Google generation


def test_google_knob_comes_from_the_same_generation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini 3 takes an enum level, 2.5 takes an integer budget, and mixing 400s."""
    level = ThinkingSpec(
        style="thinking_level",
        param="thinkingLevel",
        levels=MappingProxyType({"low": "LOW", "high": "HIGH"}),
    )
    budget = ThinkingSpec(
        style="thinking_budget",
        param="thinkingBudget",
        levels=MappingProxyType({"low": 1024, "high": 8192}),
    )
    routes = {
        "g.test/v1beta/models": {
            "models": [
                {
                    "name": "models/gemini-3.9-flash",
                    "thinking": True,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-2.7-pro",
                    "thinking": True,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-5.0-ultra",
                    "thinking": True,
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
    }
    monkeypatch.setenv("TEST_KEY", "sk-test")
    from council.registry.discovery import discover

    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec(
                "google_api",
                adapter="",
                base_url="https://g.test",
                secret_env="TEST_KEY",
                models=("gemini-3.8-flash", "gemini-2.5-pro"),
                model_styles={"gemini-3.8-flash": level, "gemini-2.5-pro": budget},
            )
        ),
        transport=_handler(routes),
    )
    by_id = {m.id: m for m in report.providers[0].new}
    assert by_id["gemini-3.9-flash"].thinking is not None
    assert by_id["gemini-3.9-flash"].thinking.style == "thinking_level"
    assert by_id["gemini-2.7-pro"].thinking is not None
    assert by_id["gemini-2.7-pro"].thinking.style == "thinking_budget"
    # A brand-new generation has no curated sibling to borrow a knob from.
    assert by_id["gemini-5.0-ultra"].thinking is None


def test_google_skips_model_without_the_vendor_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    level = ThinkingSpec(
        style="thinking_level", param="thinkingLevel", levels=MappingProxyType({"low": "LOW"})
    )
    routes = {
        "g.test/v1beta/models": {
            "models": [
                {
                    "name": "models/gemini-3.9-lite",
                    "supportedGenerationMethods": ["generateContent"],
                }
            ]
        }
    }
    monkeypatch.setenv("TEST_KEY", "sk-test")
    from council.registry.discovery import discover

    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec(
                "google_api",
                adapter="",
                base_url="https://g.test",
                secret_env="TEST_KEY",
                models=("gemini-3.8-flash",),
                model_styles={"gemini-3.8-flash": level},
            )
        ),
        transport=_handler(routes),
    )
    assert report.providers[0].new[0].thinking is None


# ---------------------------------------------------------------- catalogue


def test_catalogue_supplies_reasoning_when_the_vendor_says_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenAI-compatible listings carry ids only; the catalogue fills the gap."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {
                    "id": "openai/gpt-6-quasar",
                    "supported_parameters": ["reasoning", "tools"],
                    "reasoning": {"supported_efforts": ["high", "medium", "low", "xhigh"]},
                }
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "gpt-6-quasar"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_effort_spec()),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    plan = report.providers[0].new[0].thinking
    assert plan is not None
    assert (plan.style, plan.param) == ("enum_effort", "reasoning_effort")
    assert plan.level_names == ("low", "medium", "high", "xhigh")


def test_unknown_effort_names_are_dropped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An effort name we do not recognise is how you get a 400, so it never ships."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {
                    "id": "openai/gpt-6-quasar",
                    "supported_parameters": ["reasoning"],
                    "reasoning": {"supported_efforts": ["low", "turbo", "ultra"]},
                }
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "gpt-6-quasar"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_effort_spec()),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    plan = report.providers[0].new[0].thinking
    assert plan is not None
    assert plan.level_names == ("low",)


def test_catalogue_reasoning_parameter_alone_is_a_signal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No effort list, but the model takes a reasoning parameter."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {
                    "id": "openai/gpt-6-quasar",
                    "supported_parameters": ["reasoning_effort"],
                }
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "gpt-6-quasar"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_effort_spec()),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    plan = report.providers[0].new[0].thinking
    assert plan is not None
    assert plan.level_names == ("none", "low", "medium")


# --------------------------------------------------------------- refusals


def test_no_signal_means_no_claim(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {"vendor.test/v1/models": {"data": [{"id": "id-only-model"}]}}
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_effort_spec()),),
        monkeypatch=monkeypatch,
    )
    assert report.providers[0].new[0].thinking is None


def test_provider_without_a_declared_knob_cannot_claim_thinking(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """moonshot-style: nothing declared provider-wide and no sibling to learn from."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {
                    "id": "v/lonely-model",
                    "supported_parameters": ["reasoning"],
                    "reasoning": {"supported_efforts": ["low", "high"]},
                }
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "lonely-model"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v"),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    assert report.providers[0].new[0].thinking is None


def test_siblings_disagreeing_on_the_knob_is_refused() -> None:
    """Two curated generations with different knobs teach us nothing about a third."""
    provider = _spec(
        "google_api",
        adapter="",
        models=("gemini-3.8-flash", "gemini-4.0-pro"),
        model_styles={
            "gemini-3.8-flash": ThinkingSpec(
                style="thinking_level",
                param="thinkingLevel",
                levels=MappingProxyType({"low": "LOW"}),
            ),
            "gemini-4.0-pro": ThinkingSpec(
                style="thinking_budget", param="thinkingBudget", levels=MappingProxyType({"low": 1})
            ),
        },
    )
    model = DiscoveredModel(id="gemini-9.9-x", native_thinking=True)
    assert resolve_thinking(provider, model) is None


def test_provider_block_wins_over_siblings(tmp_path) -> None:
    """A provider-wide declaration is the curated statement of the knob."""
    provider = _spec("v", thinking=_effort_spec())
    model = DiscoveredModel(id="brand-new", catalog_reasoning=True)
    plan = resolve_thinking(provider, model)
    assert plan is not None
    assert plan.param == "reasoning_effort"


def test_catalog_entry_shape_is_small_and_price_free() -> None:
    """The catalogue type must not grow a price field: we do not write those."""
    entry = CatalogEntry(id="x", supports_reasoning=True, efforts=("low",))
    assert not any("price" in name for name in entry.__dataclass_fields__)


# ------------------------------------------------------------- round trip


def test_resolved_thinking_round_trips_into_the_registry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What we write must come back as a usable thinking spec, not just as TOML."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {
                    "id": "v/brand-new",
                    "supported_parameters": ["reasoning"],
                    "reasoning": {"supported_efforts": ["low", "medium", "high"]},
                }
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "brand-new"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_effort_spec()),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    assert report.written == ("v.toml",)

    merged = Registry.load(user_dir=tmp_path / "registry").providers["v"].models["brand-new"]
    assert merged.discovered is True
    assert merged.thinking is True
    assert merged.thinking_style is not None
    assert merged.thinking_style.style == "enum_effort"
    assert merged.thinking_style.param == "reasoning_effort"
    assert sorted(merged.thinking_style.levels) == ["high", "low", "medium"]


def test_budget_style_extra_survives_the_round_trip(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion parameters (DashScope's enable_thinking) must not be lost."""
    routes = {
        "catalog.test/api/v1/models": {
            "data": [
                {"id": "v/qwen-new", "supported_parameters": ["reasoning"]},
            ]
        },
        "vendor.test/v1/models": {"data": [{"id": "qwen-new"}]},
    }
    _run(
        tmp_path,
        routes,
        specs=(_spec("v", thinking=_budget_spec()),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    merged = Registry.load(user_dir=tmp_path / "registry").providers["v"].models["qwen-new"]
    assert merged.thinking_style is not None
    assert merged.thinking_style.style == "thinking_budget"
    assert dict(merged.thinking_style.levels) == {"low": 1024, "medium": 8192, "high": 32768}
    assert dict(merged.thinking_style.extra) == {"enable_thinking": True}

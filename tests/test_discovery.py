"""Online model discovery: probes, filtering, and the write-back guarantee.

Every test here is offline: the HTTP layer is an ``httpx.MockTransport`` and the
registry is built in-process, so nothing reaches a vendor and nothing depends on
what a vendor happens to serve today.
"""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from council.registry.discovery import (
    DiscoveredModel,
    DiscoveryOptions,
    ProviderDiscovery,
    discover,
    load_state,
    overlay_dir,
    save_state,
    state_path,
)
from council.registry.loader import ModelSpec, ProviderSpec, Registry, ThinkingSpec

_NOW = datetime(2026, 9, 11, 5, 0, 0, tzinfo=UTC)


def _spec(
    provider_id: str,
    *,
    adapter: str = "openai_api",
    base_url: str = "https://vendor.test/v1",
    secret_env: str = "TEST_KEY",
    secret_required: bool = True,
    models: tuple[str, ...] = (),
    thinking: ThinkingSpec | None = None,
    model_styles: dict[str, ThinkingSpec] | None = None,
) -> ProviderSpec:
    """A provider spec with enough shape to exercise thinking resolution."""
    styles = model_styles or {}
    return ProviderSpec(
        id=provider_id,
        display=provider_id,
        base_url=base_url,
        secret_env=secret_env,
        secret_required=secret_required,
        adapter=adapter,
        thinking=thinking or ThinkingSpec(),
        models=MappingProxyType(
            {
                mid: ModelSpec(
                    id=mid,
                    provider=provider_id,
                    thinking=mid in styles,
                    thinking_style=styles.get(mid),
                )
                for mid in models
            }
        ),
    )


def _registry(*specs: ProviderSpec) -> Registry:
    return Registry({spec.id: spec for spec in specs})


def _handler(routes: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        for prefix, payload in routes.items():
            if key.startswith(prefix):
                if payload is None:
                    return httpx.Response(404, json={"error": "not found"})
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "unrouted"})

    return httpx.MockTransport(handle)


def _run(
    tmp_path: Path,
    routes: dict[str, Any],
    *,
    specs: tuple[ProviderSpec, ...],
    monkeypatch: pytest.MonkeyPatch,
    options: DiscoveryOptions | None = None,
    secret: str | None = "sk-test",
    **kwargs: Any,
):
    if secret is not None:
        monkeypatch.setenv("TEST_KEY", secret)
    return discover(
        data=tmp_path,
        options=options or DiscoveryOptions(catalog_url=""),
        registry=_registry(*specs),
        transport=_handler(routes),
        now=_NOW,
        **kwargs,
    )


# ------------------------------------------------------------------ filtering


@pytest.mark.parametrize(
    "model_id",
    [
        "text-embedding-3-large",
        "whisper-1",
        "tts-1-hd",
        "dall-e-3",
        "gpt-image-1",
        "omni-moderation-latest",
        "gpt-4o-audio-preview",
        "gpt-4o-realtime-preview",
        "davinci-002",
        "babbage-002",
        "gpt-3.5-turbo-instruct",
        "gpt-4-0613",
        "gpt-4-turbo-2024-04-09",
        "claude-3-5-sonnet-20241022",
        "BAAI/bge-reranker-v2",
    ],
)
def test_non_chat_models_are_rejected(
    model_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing endpoint happily returns things a chat adapter cannot drive."""
    routes = {"vendor.test/v1/models": {"data": [{"id": model_id}, {"id": "gpt-5.5"}]}}
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    discovered = [m.id for p in report.providers for m in p.new]
    assert model_id not in discovered
    assert "gpt-5.5" in discovered


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5.5-preview",
        "gpt-5.5-chat-latest",
        "qwen3.8-vl",
        "glm-4v",
        "inclusionai/ling-3.0-flash-vl:free",
        "meta-llama/Llama-3.3-70B-Instruct",
    ],
)
def test_usable_chat_models_survive_the_filter(
    model_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vision, preview, ``:free`` and open-weights instruct ids are all chat."""
    routes = {"vendor.test/v1/models": {"data": [{"id": model_id}]}}
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    assert [m.id for p in report.providers for m in p.new] == [model_id]


def test_extra_exclude_patterns_are_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {"vendor.test/v1/models": {"data": [{"id": "gpt-5.5"}, {"id": "internal-x"}]}}
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v"),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="", exclude_patterns=("internal-",)),
    )
    assert [m.id for p in report.providers for m in p.new] == ["gpt-5.5"]


# ------------------------------------------------------------------- probes


def test_openai_probe_reads_context_window_when_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "vendor.test/v1/models": {
            "data": [{"id": "gpt-5.5", "name": "GPT 5.5", "context_length": 400_000}]
        }
    }
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    model = report.providers[0].new[0]
    assert model.id == "gpt-5.5"
    assert model.display == "GPT 5.5"
    assert model.max_context_tokens == 400_000


def test_newest_first_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Newest first, so the picker opens on what actually changed."""
    routes = {
        "vendor.test/v1/models": {
            "data": [
                {"id": "old-model", "created": 1_600_000_000},
                {"id": "new-model", "created": 1_780_000_000},
                {"id": "undated-model"},
            ]
        }
    }
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    assert [m.id for m in report.providers[0].new] == [
        "new-model",
        "old-model",
        "undated-model",
    ]


def test_anthropic_probe_paginates_and_sends_version_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("after_id") == "page-2":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "claude-fable-6",
                            "display_name": "Claude Fable 6",
                            "max_input_tokens": 500_000,
                            "max_tokens": 128_000,
                            "created_at": "2026-09-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "page-2"}],
                "has_more": True,
                "last_id": "page-2",
            },
        )

    monkeypatch.setenv("TEST_KEY", "sk-test")
    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(_spec("anthropic_api", adapter="", secret_env="TEST_KEY")),
        transport=httpx.MockTransport(handle),
        now=_NOW,
    )
    assert len(seen) == 2, "has_more=true must trigger a second page"
    assert seen[0].headers.get("anthropic-version")
    assert seen[0].headers.get("x-api-key") == "sk-test"
    model = next(m for p in report.providers for m in p.new if m.id == "claude-fable-6")
    assert model.max_context_tokens == 500_000
    assert model.max_output_tokens == 128_000
    assert model.created == 1_788_220_800


def test_google_probe_strips_prefix_filters_methods_and_paginates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("pageToken") == "tok":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-4.0-pro",
                            "displayName": "Gemini 4.0 Pro",
                            "inputTokenLimit": 2_097_152,
                            "outputTokenLimit": 65_536,
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/text-embedding-005",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                    {
                        "name": "models/gemini-4.0-flash",
                        "supportedGenerationMethods": ["generateContent", "countTokens"],
                    },
                ],
                "nextPageToken": "tok",
            },
        )

    monkeypatch.setenv("TEST_KEY", "sk-test")
    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(
            _spec("google_api", adapter="", base_url="https://g.test", secret_env="TEST_KEY")
        ),
        transport=httpx.MockTransport(handle),
        now=_NOW,
    )
    ids = {m.id for p in report.providers for m in p.new}
    assert ids == {"gemini-4.0-flash", "gemini-4.0-pro"}
    assert "text-embedding-005" not in ids
    assert len(seen) == 2, "nextPageToken must trigger a second page"
    # The key must travel as a header; a query parameter ends up in logs and
    # proxy access records.
    assert "key" not in seen[0].url.params
    assert seen[0].headers.get("x-goog-api-key") == "sk-test"


def test_ollama_style_provider_needs_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {"localhost/v1/models": {"data": [{"id": "qwen3:32b"}]}}
    report = _run(
        tmp_path,
        routes,
        specs=(
            _spec(
                "ollama",
                base_url="http://localhost:11434/v1",
                secret_required=False,
            ),
        ),
        monkeypatch=monkeypatch,
        secret=None,
    )
    assert report.providers[0].status == "ok"
    assert [m.id for m in report.providers[0].new] == ["qwen3:32b"]


# ------------------------------------------------------------------ outcomes


def test_missing_secret_is_skipped_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_KEY", raising=False)
    report = discover(
        data=tmp_path,
        options=DiscoveryOptions(catalog_url=""),
        registry=_registry(_spec("v")),
        transport=_handler({}),
        now=_NOW,
    )
    assert report.providers[0].status == "no_secret"
    assert report.providers[0].new == ()
    assert report.new_total == 0


def test_cli_session_is_reported_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run(
        tmp_path,
        {},
        specs=(_spec("cli_session", adapter="", base_url="", secret_env=""),),
        monkeypatch=monkeypatch,
        secret=None,
    )
    assert report.providers[0].status == "unsupported"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"unexpected": "shape"}),
        httpx.Response(200, json={"data": "not a list"}),
    ],
)
def test_bad_response_degrades_to_error_without_raising(
    response: httpx.Response, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One vendor misbehaving must never take the process with it."""
    routes = {"vendor.test/v1/models": response}
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    assert report.providers[0].status == "error"
    assert report.providers[0].detail
    assert report.written == ()


def test_one_vendor_failing_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "bad.test/v1/models": httpx.Response(503, text="down"),
        "good.test/v1/models": {"data": [{"id": "good-model"}]},
    }
    report = _run(
        tmp_path,
        routes,
        specs=(
            _spec("bad", base_url="https://bad.test/v1"),
            _spec("good", base_url="https://good.test/v1"),
        ),
        monkeypatch=monkeypatch,
    )
    by_id = {p.provider: p for p in report.providers}
    assert by_id["bad"].status == "error"
    assert by_id["good"].status == "ok"
    assert by_id["good"].new[0].id == "good-model"


# ------------------------------------------------------------------- writing


def test_only_unknown_ids_are_written_and_curated_ones_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "vendor.test/v1/models": {
            "data": [{"id": "curated-model", "context_length": 1}, {"id": "brand-new"}]
        }
    }
    spec = _spec("v", models=("curated-model",))
    report = _run(tmp_path, routes, specs=(spec,), monkeypatch=monkeypatch)
    assert [m.id for m in report.providers[0].new] == ["brand-new"]
    assert report.written == ("v.toml",)

    payload = tomllib.loads((overlay_dir(tmp_path) / "v.toml").read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in payload["models"]]
    assert ids == ["brand-new"], "a curated id must never be rewritten by discovery"
    assert payload["models"][0]["discovered"] is True


def test_overlay_cannot_shadow_curated_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing guarantee: curated wins, whatever the overlay says.

    Hand-crafted here rather than generated, because the risk is precisely a
    hostile or merely stale overlay — not the one we just wrote.
    """
    monkeypatch.setenv("TEST_KEY", "sk-test")
    user_dir = tmp_path / "registry"
    (user_dir / "discovered").mkdir(parents=True)
    (user_dir / "discovered" / "v.toml").write_text(
        '[provider]\nid = "v"\n'
        '[[models]]\nid = "curated-model"\nthinking = false\n'
        'max_context_tokens = 1\ndiscovered = true\ndisplay = "hijacked"\n',
        encoding="utf-8",
    )
    registry = Registry.load(user_dir=user_dir)
    model = registry.providers["openai_api"].models["gpt-5.2"]
    assert model.discovered is False
    assert model.display != "hijacked"
    assert model.thinking is True

    # And the same overlay *using the id it is allowed to add* does land.
    (user_dir / "discovered" / "openai_api.toml").write_text(
        '[provider]\nid = "openai_api"\n[[models]]\nid = "totally-new"\ndiscovered = true\n',
        encoding="utf-8",
    )
    merged = Registry.load(user_dir=user_dir).providers["openai_api"].models
    assert "totally-new" in merged
    assert merged["totally-new"].discovered is True


def test_handwritten_file_still_outranks_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 keeps its documented power — this change must not demote users."""
    user_dir = tmp_path / "registry"
    user_dir.mkdir(parents=True)
    (user_dir / "openai_api.toml").write_text(
        '[provider]\nid = "openai_api"\n'
        '[[models]]\nid = "gpt-5.2"\nmax_context_tokens = 999\ndisplay = "mine"\n',
        encoding="utf-8",
    )
    model = Registry.load(user_dir=user_dir).providers["openai_api"].models["gpt-5.2"]
    assert model.max_context_tokens == 999
    assert model.display == "mine"


def test_overlay_is_removed_when_nothing_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay_dir(tmp_path)
    ov.mkdir(parents=True)
    stale = ov / "v.toml"
    stale.write_text('[provider]\nid = "v"\n[[models]]\nid = "gone"\n', encoding="utf-8")

    routes = {"vendor.test/v1/models": {"data": [{"id": "just-a-model"}]}}
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", models=("just-a-model",)),),
        monkeypatch=monkeypatch,
    )
    assert report.providers[0].new == ()
    assert not stale.exists(), "ids a vendor stopped advertising must not linger"


def test_absent_curated_ids_are_reported_but_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {"vendor.test/v1/models": {"data": [{"id": "still-here"}]}}
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v", models=("still-here", "retired-last-year")),),
        monkeypatch=monkeypatch,
    )
    assert report.providers[0].absent == ("retired-last-year",)


@pytest.mark.parametrize(
    "hostile",
    [
        'evil"id',
        "evil\\id",
        "evil\nid",
        "a" * 200,
        "",
        "  ",
        "with space",
        "semi;colon",
    ],
)
def test_hostile_ids_are_dropped_or_escaped(
    hostile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload is data: it must not be able to break the TOML we emit."""
    routes = {"vendor.test/v1/models": {"data": [{"id": hostile}]}}
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    for provider in report.providers:
        for model in provider.new:
            assert "\n" not in model.id and '"' not in model.id and "\\" not in model.id
    if report.written:
        text = (overlay_dir(tmp_path) / report.written[0]).read_text(encoding="utf-8")
        tomllib.loads(text)  # must still parse


def test_hostile_display_name_cannot_break_the_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "vendor.test/v1/models": {
            "data": [{"id": "fine-id", "name": 'quote" and \\ backslash\nnewline'}]
        }
    }
    report = _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    assert report.written
    payload = tomllib.loads((overlay_dir(tmp_path) / report.written[0]).read_text(encoding="utf-8"))
    assert payload["models"][0]["id"] == "fine-id"
    assert "\n" not in payload["models"][0]["display"]


def test_written_overlay_round_trips_through_the_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "vendor.test/v1/models": {
            "data": [
                {"id": "new-a", "context_length": 128_000},
                {"id": "new-b"},
            ]
        }
    }
    _run(tmp_path, routes, specs=(_spec("v"),), monkeypatch=monkeypatch)
    merged = Registry.load(user_dir=tmp_path / "registry").providers["v"].models
    assert {"new-a", "new-b"} <= set(merged)
    assert merged["new-a"].max_context_tokens == 128_000
    assert merged["new-a"].discovered is True
    assert merged["new-a"].thinking is False, (
        "discovery cannot know the thinking protocol, so it must not claim one"
    )


# ---------------------------------------------------------- catalogue / state


def test_catalogue_only_enriches_an_unambiguous_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = {
        "data": [
            {"id": "vendor/one-model", "context_length": 200_000},
            {"id": "other/one-model", "context_length": 111},
        ]
    }
    routes = {
        "catalog.test/api/v1/models": catalog,
        "vendor.test/v1/models": {"data": [{"id": "ambig-model"}, {"id": "two-model"}]},
    }
    routes["catalog.test/api/v1/models"]["data"].append(
        {"id": "vendor/two-model", "context_length": 300_000}
    )
    report = _run(
        tmp_path,
        routes,
        specs=(_spec("v"),),
        monkeypatch=monkeypatch,
        options=DiscoveryOptions(catalog_url="https://catalog.test/api/v1/models"),
    )
    by_id = {m.id: m for m in report.providers[0].new}
    assert by_id["two-model"].max_context_tokens == 300_000
    assert by_id["ambig-model"].max_context_tokens is None, (
        "two owners ship that name — refusing to guess is the point"
    )


def test_state_round_trips_and_survives_garbage(tmp_path: Path) -> None:
    assert load_state(tmp_path) == {}
    save_state({"enabled": True, "last_check_at": "x"}, tmp_path)
    assert load_state(tmp_path)["enabled"] is True
    assert state_path(tmp_path).is_file()

    state_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_state(tmp_path) == {}


def test_report_serialises_for_the_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {"vendor.test/v1/models": {"data": [{"id": "brand-new"}]}}
    report = _run(tmp_path, routes, specs=(_spec("v", models=("old",)),), monkeypatch=monkeypatch)
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["new_total"] == 1
    assert payload["providers"][0]["absent"] == ["old"]
    assert payload["providers"][0]["new"][0]["id"] == "brand-new"


def test_provider_discovery_json_shape() -> None:
    entry = ProviderDiscovery(
        provider="v",
        display="V",
        status="ok",
        advertised=1,
        new=(DiscoveredModel(id="m", max_context_tokens=5),),
    )
    payload = entry.to_json()
    assert payload["new"] == [
        {
            "id": "m",
            "display": "m",
            "max_context_tokens": 5,
            "thinking": False,
            "thinking_levels": [],
        }
    ]


def test_loopback_probes_bypass_the_system_proxy() -> None:
    """A local Ollama/vLLM must not be sent through HTTP_PROXY.

    With a proxy set, the probe came back as the proxy's own 502 instead of the
    truthful "connection refused" — which reads like the vendor is broken when
    actually nothing is listening locally.
    """
    import httpx

    from council.registry.discovery import _proxy_mounts

    mounts = _proxy_mounts()
    assert set(mounts) == {"all://127.0.0.1", "all://localhost", "all://[::1]"}
    assert all(isinstance(t, httpx.BaseTransport) for t in mounts.values())

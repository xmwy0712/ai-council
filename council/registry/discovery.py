"""Online model discovery for the registry.

When the user turns it on, AI Council asks the vendors' *own* model-listing
endpoints what they currently serve, and writes the ids it has not seen before
into ``<data_dir>/registry/discovered/<provider>.toml``.

The registry already declares which protocol each vendor speaks, so three
probes cover thirteen of the fourteen providers:

===========================  ==========================================
probe                        providers
===========================  ==========================================
``openai_api`` protocol      9 vendors + ``openai_api`` itself — the
                             plain OpenAI-compatible ``GET {base}/models``
Anthropic                    ``GET {base}/v1/models`` (+ ``anthropic-version``)
Google                       ``GET {base}/v1beta/models`` (``?key=``)
===========================  ==========================================

``cli_session`` has no endpoint: what a local subscription can reach is the
CLI's business, and guessing it would be inventing data.

What discovery deliberately does **not** do
-------------------------------------------
* It never overwrites a curated entry. Curated entries carry verified thinking
  protocols and context windows; a listing endpoint carries ids and little
  else, and a wrong thinking parameter is a hard 400. The loader enforces this
  structurally (see :mod:`council.registry.loader`), so the rule survives even
  if an overlay file is never rewritten.
* It never writes prices. ``docs/PROVIDERS.md`` and the loader both say we do
  not ship numbers we have not verified; a gateway's price for someone else's
  model is exactly such a number.
* It never invents a thinking protocol. A discovered model claims a thinking
  control only when a **per-model** signal says it supports one — the vendor's
  own listing endpoint, or the public catalogue matched by exact id — and the
  knob then comes from what the registry already declares, never from us. No
  signal, or an ambiguous knob, means the model is offered without a thinking
  control rather than with a guessed one. See :func:`resolve_thinking`.
* It never enables itself. Off by default, so the promise that the app talks to
  nothing but model endpoints holds out of the box.
* It is never reachable from model output. Only the server's own startup path
  and an explicit user action call into here.

Every response is treated as untrusted data: size-capped, shape-checked, and
escaped on the way back out to TOML. A malformed or hostile payload degrades to
"discovered nothing" — it can never corrupt the registry or crash a session.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import httpx

from .. import __version__
from ..core import secrets
from ..core.paths import data_dir
from .loader import AUTO_DIR_NAME, ProviderSpec, Registry, ThinkingSpec

__all__ = [
    "DEFAULT_CATALOG_URL",
    "DISCOVERED_STATE_NAME",
    "DiscoveredModel",
    "DiscoveryOptions",
    "DiscoveryReport",
    "ProviderDiscovery",
    "discover",
    "load_state",
    "overlay_dir",
    "save_state",
    "state_path",
]

#: OpenRouter's catalogue is public — no key, one request, and it is a vendor's
#: own endpoint rather than a third party we invented. It doubles as the native
#: list for the ``openrouter`` provider itself.
DEFAULT_CATALOG_URL: Final = "https://openrouter.ai/api/v1/models"

#: Listing endpoints hand back everything an account can see, and a lot of it
#: can never drive a chat turn: embeddings, speech, image generators — plus
#: OpenAI's legacy completions line and dated snapshots such as ``gpt-4-0613``
#: or ``claude-3-5-sonnet-20241022``. Letting those through would bury the
#: handful of genuinely new chat models under a wall of history.
#:
#: Deliberately *not* matched: ``-preview``, ``-latest``, ``-vl``, ``-vision``
#: and ``:free``/``:nitro`` — all of those are usable chat models.
_BUILTIN_EXCLUDE_RE: Final = re.compile(
    r"(?:"
    r"embed|whisper|tts|dall-?e|moderation|audio|realtime|transcrib|speech|rerank|bge|"
    r"image|sora|stable-diffusion|flux|recraft|midjourney|guard|"
    r"^(?:davinci|babbage|curie|ada|gpt-3\.5-turbo-instruct)(?:-|$)|"
    r"-(?:19|20)\d{2}-\d{2}-\d{2}$|"
    r"-\d{8}$|"
    r"-\d{4}$"
    r")",
    re.IGNORECASE,
)

DISCOVERED_STATE_NAME: Final = "updates.json"

_ANTHROPIC_VERSION: Final = "2023-06-01"
_MAX_PAGES: Final = 10
_MAX_BODY_BYTES: Final = 8 * 1024 * 1024
_MAX_MODEL_ID: Final = 128  # matches NodeSection.model's max_length
_MAX_DISPLAY: Final = 64
_MAX_NEW_PER_PROVIDER: Final = 200
#: Ids are user-visible and get written into TOML; keep them to a boring charset
#: so a hostile payload cannot smuggle control characters or path separators.
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")

#: The effort vocabulary this project is willing to emit. Anything a catalogue
#: reports outside this set is dropped rather than passed through — an unknown
#: level name is exactly the "wrong knob" that comes back as a hard 400.
_KNOWN_EFFORTS: Final[tuple[str, ...]] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: Catalogue parameter names that signal reasoning support.
_REASONING_PARAMS: Final[frozenset[str]] = frozenset(
    {"reasoning", "include_reasoning", "reasoning_effort"}
)

#: ``gemini-3.8-flash`` -> ``gemini-3``. Used only to pick a Google thinking
#: knob from the generation the registry already curates, because Gemini 3+
#: takes an enum level while 2.5 takes an integer budget and mixing them 400s.
_FAMILY_TAG_RE: Final = re.compile(r"^([a-z]+)-(\d+)")

_GENERATED_BANNER: Final = (
    "AI Council 自动发现的模型清单 —— 请勿手工编辑。\n"
    "手工新增/覆盖请放到上一级目录的 registry/*.toml，那个目录优先级更高。\n"
    "本文件只补内置注册表里没有的 id；内置条目（含已核实的思考档位与上下文）永不被覆盖。\n"
    "thinking 只在该厂商自己的接口（或公开目录对同一 id）给出可核实信号时才声明，\n"
    "参数名与档位一律取自注册表已声明的厂商声明，从不发明；需要更精确的档位请按\n"
    "docs/PROVIDERS.md 核实后手写一份 registry/*.toml。"
)


class DiscoveryError(RuntimeError):
    """A vendor answered, but not in a shape we are willing to trust."""


@dataclass(frozen=True)
class DiscoveredModel:
    """One model a vendor stated it currently serves.

    The ``*_thinking``/``*_efforts`` fields are per-model *evidence*: they are
    what decides whether a discovered model may claim a thinking control at all
    (see :func:`resolve_thinking`). ``thinking`` is the conclusion drawn from
    that evidence, and ``None`` means we refuse to claim one.

    ``created`` is used for ordering only and is never written out, so it cannot
    become a fact the registry has to keep true.
    """

    id: str
    display: str = ""
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    created: int | None = None
    #: The vendor's own listing endpoint said thinking is supported.
    native_thinking: bool | None = None
    #: Exact effort level names the vendor itself reported.
    native_efforts: tuple[str, ...] = ()
    #: What the public catalogue matched by exact id claims for this model.
    catalog_reasoning: bool = False
    catalog_efforts: tuple[str, ...] = ()
    #: Resolved by the caller; never guessed, see :func:`resolve_thinking`.
    thinking: ThinkingPlan | None = None


@dataclass(frozen=True)
class CatalogEntry:
    """A row of the public catalogue — context window and reasoning only.

    Prices are deliberately absent: a gateway's price for another vendor's model
    is not a number this project is willing to write down.
    """

    id: str
    display: str = ""
    max_context_tokens: int | None = None
    supports_reasoning: bool = False
    efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThinkingPlan:
    """How a discovered model should drive its vendor's thinking knob.

    Every field traces back to something already declared in the registry
    (``style``/``param``/``extra``) or to a per-model signal from the vendor
    itself (``levels``, for the effort styles). Nothing here is invented.
    """

    style: str
    param: str
    levels: tuple[tuple[str, str | int], ...] = ()
    extra: tuple[tuple[str, Any], ...] = ()

    @property
    def level_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.levels)

    def to_json(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "param": self.param,
            "levels": list(self.level_names),
        }


@dataclass(frozen=True)
class ProviderDiscovery:
    provider: str
    display: str
    status: str  # ok | no_secret | unsupported | error
    detail: str = ""
    advertised: int = 0
    new: tuple[DiscoveredModel, ...] = ()
    #: Curated ids this vendor did not advertise. Informational only — removal
    #: is a human decision, and our curated list is often *wider* than a
    #: vendor's listing endpoint because it covers previews and aliases.
    absent: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display": self.display,
            "status": self.status,
            "detail": self.detail,
            "advertised": self.advertised,
            "new": [
                {
                    "id": m.id,
                    "display": m.display or m.id,
                    "max_context_tokens": m.max_context_tokens,
                    "thinking": m.thinking is not None,
                    "thinking_levels": list(m.thinking.level_names) if m.thinking else [],
                }
                for m in self.new
            ],
            "absent": list(self.absent),
        }


@dataclass(frozen=True)
class DiscoveryReport:
    checked_at: str
    providers: tuple[ProviderDiscovery, ...] = ()
    written: tuple[str, ...] = ()

    @property
    def new_total(self) -> int:
        return sum(len(p.new) for p in self.providers)

    @property
    def failed(self) -> tuple[ProviderDiscovery, ...]:
        return tuple(p for p in self.providers if p.status == "error")

    def to_json(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "written": list(self.written),
            "new_total": self.new_total,
            "providers": [p.to_json() for p in self.providers],
        }


@dataclass(frozen=True)
class DiscoveryOptions:
    """Everything discovery needs from config, so this module stays decoupled."""

    #: Extra substrings to reject on top of the built-in non-chat filter.
    exclude_patterns: tuple[str, ...] = ()
    catalog_url: str = DEFAULT_CATALOG_URL
    timeout_s: float = 20.0
    max_new_per_provider: int = _MAX_NEW_PER_PROVIDER


# --------------------------------------------------------------------- state


def state_path(data: Path | None = None) -> Path:
    return (data or data_dir()) / DISCOVERED_STATE_NAME


def overlay_dir(data: Path | None = None) -> Path:
    return (data or data_dir()) / "registry" / AUTO_DIR_NAME


def load_state(data: Path | None = None) -> dict[str, Any]:
    """Runtime state: the UI toggle plus the last report. Never raises."""
    path = state_path(data)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(state: dict[str, Any], data: Path | None = None) -> None:
    path = state_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------- thinking plan


def _family_tag(model_id: str) -> str:
    """``gemini-3.8-flash`` / ``gemini-2.5-pro`` -> ``gemini-3`` / ``gemini-2``."""
    match = _FAMILY_TAG_RE.match(model_id.lower())
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _sibling_thinking(
    provider: ProviderSpec, model_id: str, *, same_family: bool = False
) -> ThinkingSpec | None:
    """The thinking declaration curated models of this provider already carry.

    ``param`` and ``style`` are facts about a vendor's API, not about one model,
    so borrowing them from siblings is reuse rather than invention. ``levels``
    is only borrowed when every matching sibling agrees on it — a level list
    that differs between siblings describes those models, not this one, and
    guessing which applies is how you get a 400.

    Returns ``None`` whenever the answer is not unambiguous.
    """
    tag = _family_tag(model_id) if same_family else ""
    by_knob: dict[tuple[str, str, tuple[str, ...]], list[ThinkingSpec]] = {}
    for sibling in provider.models.values():
        if not sibling.thinking or sibling.id == model_id:
            continue
        if same_family and tag and _family_tag(sibling.id) != tag:
            continue
        spec = sibling.thinking_style or provider.thinking
        if spec is None or spec.style in ("", "none"):
            continue
        by_knob.setdefault((spec.style, spec.param, tuple(sorted(spec.extra))), []).append(spec)
    if len(by_knob) != 1:
        # Either nothing to learn from, or siblings disagree about the knob.
        return None
    (style, param, _), specs = next(iter(by_knob.items()))
    level_sets = {tuple(sorted(spec.levels.items())) for spec in specs}
    levels = dict(next(iter(level_sets))) if len(level_sets) == 1 else {}
    return ThinkingSpec(
        style=style,
        param=param,
        levels=MappingProxyType(levels),
        extra=specs[0].extra,
    )


def resolve_thinking(provider: ProviderSpec, model: DiscoveredModel) -> ThinkingPlan | None:
    """Decide how a newly discovered model drives its vendor's thinking knob.

    Two questions, answered in order, and a refusal is a valid answer:

    1. **Does this model support thinking?** Only a per-model signal counts — the
       vendor's own listing endpoint, or the public catalogue matched by exact
       id. A provider-wide declaration says nothing about a model we have never
       seen; sending ``reasoning_effort`` to something that does not take it is
       the classic hard 400.
    2. **Which knob, and which levels?** The knob comes from the vendor's
       declaration, never from us: the provider's own block, or what its curated
       siblings agree on. Levels come from the vendor's reported effort names
       when the knob is an effort enum, otherwise from the declaration itself.

    ``None`` means "claim nothing", which is always safe: the model is still
    selectable, it just offers no thinking control until someone verifies one.
    """
    efforts_from_vendor = tuple(e for e in model.native_efforts if e in _KNOWN_EFFORTS)

    if efforts_from_vendor:
        # Anthropic reports the effort levels outright; that is the vendor
        # stating its own protocol, so it wins.
        return ThinkingPlan(
            style="enum_effort",
            param="effort",
            levels=tuple((name, name) for name in efforts_from_vendor),
        )

    supported = model.native_thinking is True or model.catalog_reasoning
    if not supported:
        return None

    if provider.id == "google_api":
        # Gemini 3+ takes thinkingLevel, 2.5 takes thinkingBudget; the vendor's
        # listing only says "thinking: true". Take the knob from siblings of the
        # same generation, and refuse when there are none.
        base = _sibling_thinking(provider, model.id, same_family=True)
    else:
        base = provider.thinking
        if base.style in ("", "none"):
            base = _sibling_thinking(provider, model.id)
    if base is None or base.style in ("", "none"):
        return None

    if base.style == "enum_effort":
        catalogue = tuple(e for e in model.catalog_efforts if e in _KNOWN_EFFORTS)
        names = catalogue or tuple(base.levels)
        if not names:
            return None
        levels: list[tuple[str, str | int]] = [(name, name) for name in names]
    else:
        levels = list(base.levels.items())
    if not levels:
        return None
    return ThinkingPlan(
        style=base.style,
        param=base.param,
        levels=tuple(levels),
        extra=tuple(base.extra.items()),
    )


# -------------------------------------------------------------------- probes


#: Loopback hosts must never traverse a proxy. With ``HTTP_PROXY`` set, a probe
#: of a local Ollama/vLLM comes back as a misleading ``502`` from the proxy
#: instead of the truthful "connection refused" — and a user running local
#: models behind a corporate proxy could never probe them at all.
_LOOPBACK_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1", "localhost", "[::1]")


def _proxy_mounts() -> dict[str, httpx.BaseTransport]:
    """Route loopback URLs straight out, ignoring any system proxy."""
    direct = httpx.HTTPTransport(trust_env=False)
    return {f"all://{host}": direct for host in _LOOPBACK_HOSTS}


def _probe_kind(provider: ProviderSpec) -> str:
    """Which of the three shapes this provider answers to."""
    if provider.id == "cli_session":
        return "unsupported"
    if provider.id == "anthropic_api":
        return "anthropic"
    if provider.id == "google_api":
        return "google"
    if provider.id == "openai_api" or provider.adapter == "openai_api":
        return "openai"
    return "unsupported"


def _clean_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_MODEL_ID:
        return ""
    return candidate if _ID_RE.match(candidate) else ""


def _clean_display(raw: Any, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    text = " ".join(raw.split())[:_MAX_DISPLAY]
    return text or fallback


def _positive_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = int(raw)
    return value if value > 0 else None


def _epoch(raw: Any) -> int | None:
    """Epoch seconds from either a unix number or an RFC 3339 string."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    if isinstance(raw, str) and raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _truthy_flag(value: Any) -> bool:
    """Capability flags arrive as ``true``, ``{"supported": true}`` or a count."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        inner = value.get("supported")
        return inner is True or (
            isinstance(inner, (int, float)) and not isinstance(inner, bool) and inner > 0
        )
    if isinstance(value, (int, float)):
        return value > 0
    return False


def _effort_names(raw: Any) -> tuple[str, ...]:
    """Level names from an effort-capability map, in the project's own order.

    A level counts as supported only when its own flag says so; a bare entry we
    cannot interpret is treated as unsupported rather than assumed.
    """
    if not isinstance(raw, dict):
        return ()
    if not _truthy_flag(raw):
        return ()
    return tuple(name for name in _KNOWN_EFFORTS if _truthy_flag(raw.get(name)))


def _payload(response: httpx.Response, *, provider: str) -> dict[str, Any]:
    if len(response.content) > _MAX_BODY_BYTES:
        raise DiscoveryError(f"{provider} 的响应超过 {_MAX_BODY_BYTES // 1024 // 1024} MiB，已忽略")
    try:
        payload = response.json()
    except ValueError as err:
        raise DiscoveryError(f"{provider} 返回的不是合法 JSON") from err
    if not isinstance(payload, dict):
        raise DiscoveryError(f"{provider} 返回的 JSON 顶层不是对象")
    return payload


def _wanted(model_id: str, extra: Sequence[str]) -> bool:
    """Is this id a plausible chat model? Reject, never assume."""
    if _BUILTIN_EXCLUDE_RE.search(model_id):
        return False
    lowered = model_id.lower()
    return not any(pattern.lower() in lowered for pattern in extra if pattern)


def _probe_openai(
    provider: ProviderSpec,
    client: httpx.Client,
    secret: str | None,
    options: DiscoveryOptions,
) -> list[DiscoveredModel]:
    base = (provider.base_url or "").rstrip("/")
    if not base:
        raise DiscoveryError("注册表没有为它声明 base_url，无法推断 /models 地址")
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    response = client.get(f"{base}/models", headers=headers)
    response.raise_for_status()
    entries = _payload(response, provider=provider.id).get("data")
    if not isinstance(entries, list):
        raise DiscoveryError("响应里没有 data 数组")

    found: list[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = _clean_id(entry.get("id"))
        if not model_id or not _wanted(model_id, options.exclude_patterns):
            continue
        # `context_length`/`name` are not part of OpenAI's own payload but are
        # part of the shape OpenRouter and a few compatible gateways return, so
        # they are picked up opportunistically rather than required.
        found.append(
            DiscoveredModel(
                id=model_id,
                display=_clean_display(entry.get("name"), model_id),
                max_context_tokens=_positive_int(entry.get("context_length")),
                created=_epoch(entry.get("created")),
            )
        )
    return found


def _probe_anthropic(
    provider: ProviderSpec,
    client: httpx.Client,
    secret: str | None,
    options: DiscoveryOptions,
) -> list[DiscoveredModel]:
    base = (provider.base_url or "https://api.anthropic.com").rstrip("/")
    headers = {"Accept": "application/json", "anthropic-version": _ANTHROPIC_VERSION}
    if secret:
        headers["x-api-key"] = secret

    found: list[DiscoveredModel] = []
    after_id: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"limit": 1000}
        if after_id:
            params["after_id"] = after_id
        response = client.get(f"{base}/v1/models", headers=headers, params=params)
        response.raise_for_status()
        payload = _payload(response, provider=provider.id)
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise DiscoveryError("响应里没有 data 数组")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = _clean_id(entry.get("id"))
            if not model_id or not _wanted(model_id, options.exclude_patterns):
                continue
            # /v1/models reports this model's own capabilities, including which
            # effort levels it accepts — the strongest evidence available.
            capabilities = entry.get("capabilities")
            capabilities = capabilities if isinstance(capabilities, dict) else {}
            thinking = capabilities.get("thinking")
            found.append(
                DiscoveredModel(
                    id=model_id,
                    display=_clean_display(entry.get("display_name"), model_id),
                    max_context_tokens=_positive_int(entry.get("max_input_tokens")),
                    max_output_tokens=_positive_int(entry.get("max_tokens")),
                    created=_epoch(entry.get("created_at")),
                    native_thinking=_truthy_flag(thinking),
                    native_efforts=_effort_names(capabilities.get("effort")),
                )
            )
        if not payload.get("has_more"):
            break
        after_id = _clean_id(payload.get("last_id"))
        if not after_id:
            break
    return found


def _probe_google(
    provider: ProviderSpec,
    client: httpx.Client,
    secret: str | None,
    options: DiscoveryOptions,
) -> list[DiscoveredModel]:
    base = (provider.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    headers = {"Accept": "application/json"}
    if secret:
        # Google's generativelanguage API takes the key as a query parameter.
        headers["x-goog-api-key"] = secret

    found: list[DiscoveredModel] = []
    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        response = client.get(f"{base}/v1beta/models", headers=headers, params=params)
        response.raise_for_status()
        payload = _payload(response, provider=provider.id)
        entries = payload.get("models")
        if not isinstance(entries, list):
            raise DiscoveryError("响应里没有 models 数组")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            model_id = _clean_id(
                str(name).split("/", 1)[1] if isinstance(name, str) and "/" in name else name
            )
            if not model_id or not _wanted(model_id, options.exclude_patterns):
                continue
            methods = entry.get("supportedGenerationMethods")
            # A model that cannot generateContent cannot drive a council turn.
            if isinstance(methods, list) and methods and "generateContent" not in methods:
                continue
            found.append(
                DiscoveredModel(
                    id=model_id,
                    display=_clean_display(entry.get("displayName"), model_id),
                    max_context_tokens=_positive_int(entry.get("inputTokenLimit")),
                    max_output_tokens=_positive_int(entry.get("outputTokenLimit")),
                    # The vendor only says *whether* thinking is supported; which
                    # knob to use is decided from curated siblings of the same
                    # generation (see `resolve_thinking`).
                    native_thinking=_truthy_flag(entry.get("thinking")),
                )
            )
        page_token = payload.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            break
    return found


def _catalog_index(client: httpx.Client, url: str) -> dict[str, CatalogEntry]:
    """Suffix index of a public catalogue, for context window and reasoning.

    Also reads the catalogue's per-model reasoning statement — whether the model
    takes a reasoning parameter, and which efforts it accepts. Prices are
    deliberately not read: a gateway's price for another vendor's model is not a
    number this project is willing to write down.
    """
    if not url:
        return {}
    try:
        response = client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = _payload(response, provider="catalog")
    except (httpx.HTTPError, DiscoveryError):
        return {}
    index: dict[str, CatalogEntry] = {}
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or "/" not in raw_id:
            continue
        suffix = raw_id.split("/", 1)[1].split(":", 1)[0]
        if suffix in index:
            # Ambiguous suffix (several owners ship the same name): refuse to
            # guess, which is the same reason we match on nothing else.
            index[suffix] = CatalogEntry(id="")
            continue
        params = entry.get("supported_parameters")
        params = params if isinstance(params, list) else []
        reasoning = entry.get("reasoning")
        reasoning = reasoning if isinstance(reasoning, dict) else {}
        efforts_raw = reasoning.get("supported_efforts")
        efforts = tuple(
            name for name in _KNOWN_EFFORTS if isinstance(efforts_raw, list) and name in efforts_raw
        )
        index[suffix] = CatalogEntry(
            id=suffix,
            display=_clean_display(entry.get("name"), suffix),
            max_context_tokens=_positive_int(entry.get("context_length")),
            supports_reasoning=any(isinstance(p, str) and p in _REASONING_PARAMS for p in params),
            efforts=efforts,
        )
    return {key: value for key, value in index.items() if value.id}


# ------------------------------------------------------------------- writing


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    escaped = "".join(char for char in escaped if char >= " ")
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _toml_string(str(value))


def _toml_inline_table(pairs: Sequence[tuple[str, Any]]) -> str:
    body = ", ".join(f"{_toml_string(key)} = {_toml_value(value)}" for key, value in pairs)
    return f"{{ {body} }}"


def _render_overlay(
    provider: ProviderSpec,
    models: Sequence[DiscoveredModel],
    *,
    source: str,
    checked_at: str,
) -> str:
    lines = [f"# {line}" if line else "#" for line in _GENERATED_BANNER.splitlines()]
    lines += [
        f"# 生成时间：{checked_at}",
        f"# 本次来源：{source}",
        "",
        "[provider]",
        f"id = {_toml_string(provider.id)}",
        "",
    ]
    for model in models:
        lines.append("[[models]]")
        lines.append(f"id = {_toml_string(model.id)}")
        lines.append(f"display = {_toml_string(model.display or model.id)}")
        if model.max_context_tokens is not None:
            lines.append(f"max_context_tokens = {model.max_context_tokens}")
        if model.max_output_tokens is not None:
            lines.append(f"max_output_tokens = {model.max_output_tokens}")
        plan = model.thinking
        if plan is not None and plan.levels:
            lines.append("thinking = true")
            lines.append(f"thinking_style = {_toml_string(plan.style)}")
            lines.append(f"param = {_toml_string(plan.param)}")
            lines.append(f"levels = {_toml_inline_table(list(plan.levels))}")
            if plan.extra:
                lines.append(f"extra = {_toml_inline_table(list(plan.extra))}")
        lines.append("discovered = true")
        lines.append(f"source = {_toml_string(source)}")
        lines.append(f"verified = {_toml_string(checked_at[:10])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_overlays(
    curated: dict[str, ProviderSpec],
    results: Sequence[ProviderDiscovery],
    *,
    data: Path | None,
    checked_at: str,
    source_by_provider: dict[str, str],
) -> tuple[str, ...]:
    target = overlay_dir(data)
    written: list[str] = []
    for result in results:
        path = target / f"{result.provider}.toml"
        if not result.new:
            # Nothing new: remove a stale overlay rather than leaving ids the
            # vendor has stopped advertising.
            if path.is_file():
                path.unlink()
            continue
        provider = curated.get(result.provider)
        if provider is None:
            continue
        target.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _render_overlay(
                provider,
                result.new,
                source=source_by_provider.get(result.provider, ""),
                checked_at=checked_at,
            ),
            encoding="utf-8",
        )
        written.append(path.name)
    return tuple(sorted(written))


# ------------------------------------------------------------------ discover


def discover(
    *,
    data: Path | None = None,
    options: DiscoveryOptions | None = None,
    env_file: Path | None = None,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
    registry: Registry | None = None,
    now: datetime | None = None,
) -> DiscoveryReport:
    """Probe every provider we know how to probe, and write the gaps.

    Failures are per-provider and never raise: one vendor being down must not
    stop the others, and must never take the server with it.
    """
    options = options or DiscoveryOptions()
    moment = now or datetime.now(UTC)
    checked_at = moment.astimezone().isoformat(timespec="seconds")

    # Curated = built-ins + the user's hand-written files, *excluding* anything
    # discovery itself wrote earlier. Without this the overlay would look like
    # curated data on the next run and every discovered id would be dropped.
    # Pinned to this run's data directory rather than the process-wide default,
    # so discovery and the registry it writes into can never disagree about
    # which directory is in play.
    root = (data or data_dir()) / "registry"
    curated_registry = registry or Registry.load(root, include_discovered=False)
    curated = dict(curated_registry.providers)

    owns_client = client is None
    if client is not None:
        http = client
    elif transport is not None:
        # Test double: it must see *every* request, so no mounts may shadow it.
        http = httpx.Client(
            timeout=options.timeout_s,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": f"ai-council/{__version__}"},
        )
    else:
        http = httpx.Client(
            timeout=options.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": f"ai-council/{__version__}"},
            mounts=_proxy_mounts(),
        )
    try:
        catalog = _catalog_index(http, options.catalog_url)
        results: list[ProviderDiscovery] = []
        sources: dict[str, str] = {}

        for provider_id in sorted(curated):
            provider = curated[provider_id]
            kind = _probe_kind(provider)
            if kind == "unsupported":
                results.append(
                    ProviderDiscovery(
                        provider=provider.id,
                        display=provider.display or provider.id,
                        status="unsupported",
                        detail=(
                            "本地 CLI 的可用模型由订阅决定，没有可查询的接口"
                            if provider.id == "cli_session"
                            else "该厂商没有已知的列模型接口"
                        ),
                    )
                )
                continue

            secret = (
                secrets.resolve(provider.secret_env, env_file=env_file)
                if provider.secret_env
                else None
            )
            if provider.secret_required and not secret:
                results.append(
                    ProviderDiscovery(
                        provider=provider.id,
                        display=provider.display or provider.id,
                        status="no_secret",
                        detail=f"未配置 {provider.secret_env}",
                    )
                )
                continue

            try:
                if kind == "openai":
                    found = _probe_openai(provider, http, secret, options)
                    source = f"GET {(provider.base_url or '').rstrip('/')}/models"
                elif kind == "anthropic":
                    found = _probe_anthropic(provider, http, secret, options)
                    source = f"GET {(provider.base_url or '').rstrip('/')}/v1/models"
                else:
                    found = _probe_google(provider, http, secret, options)
                    source = f"GET {(provider.base_url or '').rstrip('/')}/v1beta/models"
            except (httpx.HTTPError, DiscoveryError, ValueError) as err:
                results.append(
                    ProviderDiscovery(
                        provider=provider.id,
                        display=provider.display or provider.id,
                        status="error",
                        detail=f"{type(err).__name__}: {err}"[:300],
                    )
                )
                continue

            # Fold in what the public catalogue says about the *same* id, then
            # decide the thinking control from the combined per-model evidence.
            advertised: dict[str, DiscoveredModel] = {}
            for model in found:
                hint = catalog.get(model.id)
                advertised[model.id] = (
                    replace(
                        model,
                        display=model.display or hint.display,
                        max_context_tokens=model.max_context_tokens or hint.max_context_tokens,
                        catalog_reasoning=hint.supports_reasoning,
                        catalog_efforts=hint.efforts,
                    )
                    if hint is not None
                    else model
                )

            known = set(provider.models)
            fresh = [
                replace(model, thinking=resolve_thinking(provider, model))
                for mid, model in advertised.items()
                if mid not in known
            ]
            # Newest first: this order is preserved into the overlay, and the
            # registry keeps TOML order, so the picker shows the newest models
            # at the top without the registry needing a timestamp field.
            fresh.sort(key=lambda m: (m.created is not None, m.created or 0), reverse=True)
            results.append(
                ProviderDiscovery(
                    provider=provider.id,
                    display=provider.display or provider.id,
                    status="ok",
                    advertised=len(advertised),
                    new=tuple(fresh[: options.max_new_per_provider]),
                    absent=tuple(sorted(known - set(advertised))),
                )
            )
            sources[provider.id] = source

        written = _write_overlays(
            curated, results, data=data, checked_at=checked_at, source_by_provider=sources
        )
    finally:
        if owns_client:
            http.close()

    return DiscoveryReport(checked_at=checked_at, providers=tuple(results), written=written)

"""Single source of truth for configuration.

Design rules enforced here:

* ``extra = "forbid"`` — a typo in ``config.toml`` is an error, not a silent no-op.
* Semantics the type system cannot express (node count vs. ``active_nodes``,
  judge independence, quorum reachability) are checked in model validators and
  raise :class:`ConfigError` with a human-readable message.
* :func:`fingerprint` lets ``council resume`` detect that the config changed
  under a paused session.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "MAX_NODES",
    "MIN_NODES",
    "SCHEMA_VERSION",
    "AttachmentSection",
    "BudgetSection",
    "Config",
    "ConfigError",
    "CouncilSection",
    "FailurePolicy",
    "FailureSection",
    "JudgeSection",
    "NodeOverrides",
    "NodeRole",
    "NodeSection",
    "SanitizeSection",
    "TimeoutSection",
    "default_config",
    "diff_configs",
    "fingerprint",
    "load_config",
]

SCHEMA_VERSION: Final[int] = 1
MIN_NODES: Final[int] = 1
MAX_NODES: Final[int] = 5

_HEX_COLOR: Final = re.compile(r"^#[0-9a-fA-F]{6}$")
_LANGUAGE: Final = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or is semantically invalid."""

    def __init__(self, message: str, *, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FailurePolicy(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    ASK_USER = "ask_user"


class NodeRole(StrEnum):
    PARTICIPANT = "participant"
    JUDGE = "judge"


class CouncilSection(_Strict):
    max_nodes: int = Field(default=MAX_NODES, ge=MIN_NODES, le=MAX_NODES)
    active_nodes: int = Field(default=3, ge=MIN_NODES, le=MAX_NODES)
    max_rounds: int = Field(default=8, ge=1, le=32)
    debate_rounds: int = Field(default=2, ge=0, le=8)
    language: str = Field(default="zh-CN", pattern=_LANGUAGE.pattern)


class TimeoutSection(_Strict):
    connect_s: float = Field(default=30.0, gt=0, le=600)
    idle_s: float = Field(default=90.0, gt=0, le=3600)
    total_s: float = Field(default=900.0, gt=0, le=7200)


class FailureSection(_Strict):
    policy: FailurePolicy = FailurePolicy.ASK_USER
    min_quorum: int = Field(default=2, ge=1, le=MAX_NODES)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff: Literal["exponential_jitter", "fixed"] = "exponential_jitter"
    circuit_threshold: int = Field(default=3, ge=1, le=20)
    circuit_cooldown_s: float = Field(default=60.0, gt=0, le=3600)


class AttachmentSection(_Strict):
    max_file_mib: float = Field(default=1.0, gt=0, le=32)
    max_total_mib: float = Field(default=4.0, gt=0, le=128)
    max_files: int = Field(default=10, ge=0, le=100)
    # 机器可读类型按用户要求放开：.json 必须严格可解析，.csv 必须可被
    # csv 模块解析；扩展名伪装一律由内容校验拒绝。
    allowed_suffixes: tuple[str, ...] = (".txt", ".md", ".json", ".csv")


class SanitizeSection(_Strict):
    """Display/export cleaning. The event log always keeps raw_text."""

    enabled: bool = True
    disabled_rules: tuple[str, ...] = ()


class BudgetSection(_Strict):
    context_tokens: int = Field(default=60_000, ge=1_000)
    reserve_output_tokens: int = Field(default=4_000, ge=256)
    max_output_tokens: int = Field(default=8_000, ge=256)
    max_chars_review: int = Field(default=4_000, ge=200)
    max_chars_debate: int = Field(default=1_200, ge=100)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    persist_chunks: bool = True


class NodeOverrides(_Strict):
    """Per-node overrides of global knobs. ``None`` means "inherit"."""

    connect_s: float | None = None
    idle_s: float | None = None
    total_s: float | None = None
    max_retries: int | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking: str | None = None
    max_chars: int | None = None


class NodeSection(_Strict):
    id: str = Field(min_length=1, max_length=64)
    display: str = Field(default="", max_length=64)
    adapter: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    thinking: str | None = None
    role: NodeRole = NodeRole.PARTICIPANT
    color: str = Field(default="#3b82f6")
    enabled: bool = True
    overrides: NodeOverrides = Field(default_factory=NodeOverrides)
    #: Adapter-specific knobs (base_url, cli profile, generic_http template...).
    #: Validated by the adapter that consumes them, not by the schema.
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not _HEX_COLOR.match(self.color):
            raise ValueError(f"color 必须是 #RRGGBB 形式，当前为 {self.color!r}")
        if self.role is NodeRole.JUDGE and self.enabled is False:
            raise ValueError("judge 节点不能被禁用；如需人工选择请设置 judge.auto_select = false")
        return self

    @property
    def label(self) -> str:
        return self.display or self.id


class JudgeSection(_Strict):
    enabled: bool = True
    node_id: str = Field(min_length=1, max_length=64)
    auto_select: bool = True
    warn_if_weak_model: bool = True


class Config(_Strict):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    council: CouncilSection = Field(default_factory=CouncilSection)
    timeout: TimeoutSection = Field(default_factory=TimeoutSection)
    failure: FailureSection = Field(default_factory=FailureSection)
    attachments: AttachmentSection = Field(default_factory=AttachmentSection)
    sanitize: SanitizeSection = Field(default_factory=SanitizeSection)
    budget: BudgetSection = Field(default_factory=BudgetSection)
    nodes: list[NodeSection] = Field(default_factory=list)
    judge: JudgeSection | None = None

    # ---------------------------------------------------------------- helpers

    @property
    def participants(self) -> list[NodeSection]:
        return [n for n in self.nodes if n.role is NodeRole.PARTICIPANT and n.enabled]

    @property
    def judge_node(self) -> NodeSection | None:
        if self.judge is None or not self.judge.enabled:
            return None
        for node in self.nodes:
            if node.id == self.judge.node_id:
                return node
        return None

    def node(self, node_id: str) -> NodeSection:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise ConfigError(f"未定义的节点 id：{node_id!r}")

    # -------------------------------------------------------------- semantics

    @model_validator(mode="after")
    def _check_semantics(self) -> Self:
        ids = [n.id for n in self.nodes]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"节点 id 重复：{', '.join(dupes)}")

        participants = self.participants
        if not 1 <= len(participants) <= MAX_NODES:
            raise ValueError(f"参与者节点数量必须为 1..{MAX_NODES}，当前为 {len(participants)}")
        if len(participants) > self.council.max_nodes:
            raise ValueError(
                f"参与者数量 {len(participants)} 超过 council.max_nodes={self.council.max_nodes}"
            )
        if len(participants) != self.council.active_nodes:
            raise ValueError(
                f"council.active_nodes={self.council.active_nodes} 与已启用的参与者数量 "
                f"{len(participants)} 不一致：请改成 {len(participants)}，"
                "或用 enabled=false 调整节点"
            )
        if self.failure.min_quorum > len(participants):
            raise ValueError(
                f"failure.min_quorum={self.failure.min_quorum} 大于参与者数量 {len(participants)}，"
                "会议将永远无法开始"
            )

        if self.judge is not None and self.judge.enabled:
            judge_ids = {n.id for n in self.nodes if n.role is NodeRole.JUDGE}
            if self.judge.node_id not in judge_ids:
                raise ValueError(
                    f"judge.node_id={self.judge.node_id!r} 不存在，或该节点的 role 不是 'judge'"
                )
            participant_ids = {n.id for n in participants}
            if self.judge.node_id in participant_ids:
                raise ValueError(
                    f"judge.node_id={self.judge.node_id!r} 同时是参与者；"
                    "Judge 必须与所有参与者分离（它只做选择与裁定，不参与提案/评审/辩论）"
                )
        return self

    def warnings(self) -> list[str]:
        """Non-fatal advisories. Shown in the UI, never block a session."""
        out: list[str] = []
        judge = self.judge_node
        if judge is not None and self.judge is not None and self.judge.warn_if_weak_model:
            for node in self.participants:
                if node.adapter == judge.adapter and node.model == judge.model:
                    out.append(
                        f"Judge（{judge.id}）与参与者 {node.id} 使用同一 adapter+model"
                        f"（{judge.adapter}/{judge.model}），裁定独立性会下降"
                    )
                    break
        if self.council.debate_rounds == 0:
            out.append("debate_rounds=0，P4 辩论阶段将被跳过，分歧可能不会被消解")
        return out


# --------------------------------------------------------------------- loader


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    version = int(raw.get("schema_version", 1))
    if version > SCHEMA_VERSION:
        raise ConfigError(
            f"配置文件 schema_version={version} 高于本程序支持的 {SCHEMA_VERSION}，"
            "请升级 AI Council"
        )
    # Migrations are appended here as schema_version grows. v1 is the first
    # published schema, so there is nothing to rewrite yet.
    raw["schema_version"] = SCHEMA_VERSION
    return raw


def _format_problems(err: ValidationError) -> list[str]:
    problems: list[str] = []
    for item in err.errors():
        loc = ".".join(str(p) for p in item["loc"]) or "<root>"
        problems.append(f"{loc}: {item['msg']}")
    return problems


def parse_config(raw: dict[str, Any]) -> Config:
    try:
        return Config.model_validate(_migrate(raw))
    except ValidationError as err:
        problems = _format_problems(err)
        detail = "\n  - ".join(problems)
        raise ConfigError(f"配置校验失败：\n  - {detail}", problems=problems) from err


def load_config(path: Path | str) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as err:
        raise ConfigError(f"配置文件必须是 UTF-8：{path}（{err}）") from err
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"配置文件不是合法 TOML：{path}（{err}") from err
    return parse_config(raw)


def fingerprint(config: Config) -> str:
    """Stable hash of the *behavioural* part of the config.

    Used to warn on resume: if the fingerprint changed, the suspended session
    was authored under different rules.
    """
    payload = config.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def diff_configs(before: Config, after: Config, *, limit: int = 20) -> list[str]:
    """Human-readable differences, used when resuming under a changed config.

    Secrets can never appear here: the config model has no secret fields by
    construction — credentials live in the OS keyring or ``.env`` only.
    """
    out: list[str] = []
    _walk(before.model_dump(mode="json"), after.model_dump(mode="json"), "", out)
    return out[:limit]


def _walk(left: Any, right: Any, path: str, out: list[str]) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            _walk(left.get(key), right.get(key), f"{path}.{key}" if path else key, out)
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append(f"{path}: {len(left)} 项 → {len(right)} 项")
            return
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            _walk(a, b, f"{path}[{index}]", out)
        return
    if left != right:
        out.append(f"{path}: {left!r} → {right!r}")


def default_config() -> Config:
    """A three-node council wired to the fake adapter.

    Enough to run ``council run`` end-to-end with zero credentials, which is
    what a stranger needs in their first ten minutes.
    """
    return parse_config(
        {
            "schema_version": SCHEMA_VERSION,
            "council": {"active_nodes": 3, "language": "zh-CN"},
            "nodes": [
                {
                    "id": "n1",
                    "display": "Node A",
                    "adapter": "fake",
                    "model": "fake/a",
                    "role": "participant",
                    "color": "#3b82f6",
                },
                {
                    "id": "n2",
                    "display": "Node B",
                    "adapter": "fake",
                    "model": "fake/b",
                    "role": "participant",
                    "color": "#f59e0b",
                },
                {
                    "id": "n3",
                    "display": "Node C",
                    "adapter": "fake",
                    "model": "fake/c",
                    "role": "participant",
                    "color": "#10b981",
                },
                {
                    "id": "judge",
                    "display": "Judge",
                    "adapter": "fake",
                    "model": "fake/judge",
                    "role": "judge",
                    "color": "#8b5cf6",
                    "thinking": "high",
                },
            ],
            "judge": {"enabled": True, "node_id": "judge", "auto_select": True},
        }
    )

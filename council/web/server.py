"""FastAPI application for the AI Council web UI.

Endpoints are thin translations of :mod:`council.web.sessionhub` calls. The
engine and its event log never leave the process; a browser session is a
WebSocket subscriber plus a handful of JSON calls.

Export is a plain download: the server renders sanitised Markdown and streams
it back with ``Content-Disposition: attachment``, so the only writes ever made
are the event store (application data dir) and whatever the user does with the
downloaded file. That keeps the "no model output writes outside the data dir"
rule intact even in a browser context.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..adapters import build_adapters
from ..adapters.scan import provider_available, scan_model
from ..core import secrets
from ..core.config import Config, FailurePolicy, NodeRole
from ..core.contracts import Adapter
from ..core.ids import new_session_id
from ..core.paths import sessions_db
from ..core.secrets import SecretError
from ..core.state import replay
from ..core.store import EventStore
from ..export import render_markdown
from ..registry import cli_diagnostics, discovery, get_registry, registry_at
from .sessionhub import HubError, SessionManager, UploadRejected

__all__ = ["create_app"]

AdapterBuilder = Callable[[Config], dict[str, Adapter]]

_STATIC_DIR = Path(__file__).parent / "static"

#: Vendor variables the settings panel surfaces for zero-CLI onboarding.
PRESET_SECRETS: tuple[tuple[str, str], ...] = (
    ("OPENAI_API_KEY", "keys.vendor.openai"),
    ("ANTHROPIC_API_KEY", "keys.vendor.anthropic"),
    ("GOOGLE_API_KEY", "keys.vendor.gemini"),
    ("DEEPSEEK_API_KEY", "keys.vendor.deepseek"),
    ("MOONSHOT_API_KEY", "keys.vendor.moonshot"),
    ("ZHIPU_API_KEY", "keys.vendor.zhipu"),
    ("DASHSCOPE_API_KEY", "keys.vendor.dashscope"),
    ("XAI_API_KEY", "keys.vendor.xai"),
    ("OPENROUTER_API_KEY", "keys.vendor.openrouter"),
    ("SILICONFLOW_API_KEY", "keys.vendor.siliconflow"),
    ("MODEL_API_KEY", "keys.vendor.meta"),
)

_KEY_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


class _UploadFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=8 * 1024 * 1024)


class _NodeOverride(BaseModel):
    """会话级阵容覆盖：仅影响本场会议，不写回全局配置。

    档位取值由注册表决定（各厂商档位数量不同：GPT-6 有 5 档、Gemini Flash
    有 4 档、DeepSeek/GLM-5.3 只有 3 档），因此这里不做固定枚举校验，
    交由 _apply_overrides 按注册表拒绝。
    """

    node_id: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    thinking: str | None = Field(default=None, max_length=32)


class _RunTuning(BaseModel):
    """运行参数调优：卡死判定超时、重试次数与失败策略，随会话提交并写入 effective config。

    ``policy`` 只作用于**新开的会谈**：设置里的策略是新建会话时的默认值，
    已开始的会话沿用创建时的策略，不在运行中途被设置页改动影响。
    """

    idle_s: float | None = Field(default=None, gt=0, le=3600)
    total_s: float | None = Field(default=None, gt=0, le=7200)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    policy: str | None = Field(default=None)


class _NewSession(BaseModel):
    question: str = Field(min_length=0, max_length=20000)
    files: list[_UploadFile] = Field(default_factory=list)
    overrides: list[_NodeOverride] = Field(default_factory=list)
    tuning: _RunTuning | None = None


class _ActionBody(BaseModel):
    action: str
    policy: str | None = None


class _KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=_KEY_NAME)
    value: str = Field(min_length=1, max_length=4096)


class _ToggleBody(BaseModel):
    enabled: bool


def _missing(session_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"找不到会话 {session_id}")


def _discovery_options(cfg: Config) -> discovery.DiscoveryOptions:
    section = cfg.updates
    return discovery.DiscoveryOptions(
        exclude_patterns=tuple(section.exclude_patterns),
        catalog_url=section.catalog_url or discovery.DEFAULT_CATALOG_URL,
        timeout_s=section.timeout_s,
    )


def _updates_enabled(cfg: Config, state: dict[str, Any]) -> bool:
    """The panel's toggle outranks the config default.

    A user who only ever touches the web UI has no ``config.toml`` to edit, and
    they are exactly the audience this feature exists for — so their choice,
    recorded in the data dir, wins over the shipped default.
    """
    override = state.get("enabled")
    if isinstance(override, bool):
        return override
    return cfg.updates.enabled


def _manager(request: Request) -> SessionManager:
    manager: SessionManager | None = request.app.state.manager
    if manager is None:
        raise HTTPException(status_code=503, detail="会话管理器尚未就绪")
    return manager


def create_app(
    *,
    data_dir: Path,
    config: Config | None = None,
    adapter_builder: AdapterBuilder = build_adapters,
    resume_timeout_s: float = 0.0,
    discovery_transport: Any = None,
) -> FastAPI:
    """Build the app. ``resume_timeout_s`` defaults to 0 → the hub's own
    generous in-process value is used (test doubles may pass a small one).

    ``discovery_transport`` lets tests drive the model-discovery path through
    ``httpx.MockTransport`` so the suite stays fully offline.
    """
    from .sessionhub import _RESUME_TIMEOUT_S

    effective_timeout = resume_timeout_s or _RESUME_TIMEOUT_S
    update_lock = asyncio.Lock()
    startup_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        nonlocal startup_task
        # Pin the registry to *this* app's data directory for the server's whole
        # lifetime. Without it, `council serve --data-dir X` would read config
        # and sessions from X while the registry — and therefore the adapters
        # and the model picker — still came from the default directory, so a
        # discovered overlay would be written into X and never read back.
        with registry_at(data_dir / "registry"):
            store = await EventStore(sessions_db(data_dir)).open()
            app.state.store = store
            manager = SessionManager(
                store=store,
                config=config if config is not None else manager_default_config(),
                data_dir=data_dir,
                adapter_builder=adapter_builder,
                resume_timeout_s=effective_timeout,
            )
            app.state.manager = manager
            if _due_for_check(manager.config):
                # Background: a slow or unreachable vendor must never delay the
                # server coming up. Cancelled on shutdown.
                startup_task = asyncio.create_task(_refresh_quietly("startup"))
            try:
                yield
            finally:
                if startup_task is not None and not startup_task.done():
                    startup_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await startup_task
                for session_id in list(manager._hubs):
                    await manager.drop(session_id)
                await store.close()

    def manager_default_config() -> Config:
        from ..core.config import default_config, load_config
        from ..core.paths import config_path

        candidate = config_path(data_dir)
        if candidate.is_file():
            return load_config(candidate)
        return default_config()

    # -------------------------------------------------------- model discovery

    def _config_now() -> Config:
        manager: SessionManager | None = getattr(app.state, "manager", None)
        if manager is not None:
            return manager.config
        return config if config is not None else manager_default_config()

    def _due_for_check(cfg: Config) -> bool:
        """Enabled, asked to check on start, and not checked recently."""
        if not cfg.updates.check_on_start:
            return False
        state = discovery.load_state(data_dir)
        if not _updates_enabled(cfg, state):
            return False
        last = state.get("last_check_at")
        if not isinstance(last, str) or not last:
            return True
        try:
            previous = datetime.fromisoformat(last)
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        age_h = (datetime.now(UTC) - previous).total_seconds() / 3600
        return age_h >= cfg.updates.min_interval_h

    async def _discover_once(reason: str) -> None:
        cfg = _config_now()
        report = await asyncio.to_thread(
            discovery.discover,
            data=data_dir,
            options=_discovery_options(cfg),
            env_file=data_dir / ".env",
            transport=discovery_transport,
        )
        # The picker reads the process-wide cache, so without this reload a
        # fresh overlay would not show up until the next restart.
        get_registry(reload=True)
        state = discovery.load_state(data_dir)
        state["reason"] = reason
        state["last_check_at"] = report.checked_at
        state["last_error"] = ""
        state["report"] = report.to_json()
        discovery.save_state(state, data_dir)

    async def _run_discovery(reason: str) -> dict[str, Any]:
        """Sweep once, and share the result with anyone already waiting.

        Pressing "check now" while the start-up check is still running is a
        perfectly reasonable thing to do, so it waits for that answer instead of
        erroring out or firing a second round of vendor requests. The probes are
        blocking HTTP, hence the worker thread.
        """
        already_running = update_lock.locked()
        async with update_lock:
            if not already_running:
                await _discover_once(reason)
        return discovery.load_state(data_dir)

    async def _refresh_quietly(reason: str) -> None:
        """Start-up path: never let a vendor outage become a server error."""
        try:
            await _run_discovery(reason)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # recorded, never fatal
            with contextlib.suppress(OSError):
                state = discovery.load_state(data_dir)
                state["last_error"] = f"{type(err).__name__}: {err}"[:300]
                discovery.save_state(state, data_dir)

    app = FastAPI(
        title="AI Council",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def _no_cache_static_assets(request: Request, call_next: Callable[..., Any]) -> Response:
        """静态资产禁用启发式缓存：代码更新后浏览器必须拿到新版本。

        保留 ETag：内容没变时 304 依旧生效，只禁掉「猜测缓存」。
        """
        response = cast(Response, await call_next(request))
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html", ".json")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------------- meta

    def _node_meta(node: Any) -> dict[str, Any]:
        """节点的展示信息：厂商、模型显示名与思考档位（给阵容编辑器用）。"""
        registry = get_registry()
        model = registry.model(node.model)
        spec = registry.thinking_spec(node.adapter, node.model)
        return {
            "id": node.id,
            "display": node.display or node.id,
            "adapter": node.adapter,
            "model": node.model,
            "model_display": (model.display if model else node.model),
            "thinking": node.thinking or "",
            "thinking_supported": bool(model.thinking) if model else False,
            "thinking_levels": sorted(spec.levels),
        }

    @app.get("/api/meta")
    async def meta(request: Request) -> dict[str, Any]:
        cfg = _manager(request).config
        return {
            "version": __version__,
            "participants": [_node_meta(n) for n in cfg.participants],
            "judge": cfg.judge_node.id if cfg.judge_node else None,
            "judge_node": _node_meta(cfg.judge_node) if cfg.judge_node else None,
            "language": cfg.council.language,
            "warnings": cfg.warnings(),
        }

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict[str, list[dict[str, str | bool | None]]]:
        """三大本地 CLI（codex / claude / agy）的环境检查：安装、版本、鉴权。"""
        return {"clis": await cli_diagnostics.check_all()}

    # ------------------------------------------------------- model list updates

    def _updates_payload() -> dict[str, Any]:
        cfg = _config_now()
        state = discovery.load_state(data_dir)
        report = state.get("report")
        return {
            "enabled": _updates_enabled(cfg, state),
            "config_enabled": cfg.updates.enabled,
            "min_interval_h": cfg.updates.min_interval_h,
            "last_check_at": state.get("last_check_at") or "",
            "last_error": state.get("last_error") or "",
            "report": report if isinstance(report, dict) else None,
            "overlay_dir": str(discovery.overlay_dir(data_dir)),
        }

    @app.get("/api/updates")
    async def updates_status() -> dict[str, Any]:
        """模型清单更新的状态：开关、上次检查时间与上次结果。"""
        return _updates_payload()

    @app.post("/api/updates/enabled")
    async def updates_set_enabled(body: _ToggleBody) -> dict[str, Any]:
        """界面开关。写数据目录，不碰用户的 config.toml。"""
        state = discovery.load_state(data_dir)
        state["enabled"] = body.enabled
        discovery.save_state(state, data_dir)
        return _updates_payload()

    @app.post("/api/updates/refresh")
    async def updates_refresh() -> dict[str, Any]:
        """立即联网刷新一次。未开启时明确拒绝，而不是偷偷联网。"""
        if not _updates_enabled(_config_now(), discovery.load_state(data_dir)):
            raise HTTPException(status_code=409, detail="模型清单更新未开启")
        try:
            await _run_discovery("manual")
        except Exception as err:  # surfaced to the UI, not a 500
            raise HTTPException(
                status_code=502, detail=f"更新失败：{type(err).__name__}: {err}"[:300]
            ) from err
        return _updates_payload()

    @app.get("/api/models")
    async def models_catalog() -> dict[str, Any]:
        """注册表目录，供网页端阵容编辑器的下拉框使用。

        只推荐**此刻真的能用**的模型，两道门槛：
        1. 厂商密钥要解析得到（本地/免密端点除外），本地 CLI 要已安装；
        2. 上次联网探测若是**失败**告终（连不上、被拒绝），这家也不推荐——
           探测失败比"有密钥"是更强的证据，修好后点一次「立即检查」即可恢复。
        每个给出的模型还要能离线构造出一次合法调用负载。
        """
        registry = get_registry()
        env_file = data_dir / ".env"
        # 上次探测结果：error = 那一次都连不上/被拒，比"有密钥"更有说服力
        report = discovery.load_state(data_dir).get("report") or {}
        probed_failed = {
            (p.get("provider") or "")
            for p in (report.get("providers") or [])
            if p.get("status") == "error"
        }
        providers = []
        for pid in sorted(registry.providers):
            provider = registry.providers[pid]
            available = provider_available(provider, env_file=env_file) and pid not in probed_failed
            models = []
            for m in provider.models.values():
                usable = available and not scan_model(registry, pid, m.id)
                models.append(
                    {
                        "id": m.id,
                        "display": m.display or m.id,
                        "thinking": m.thinking,
                        "thinking_levels": (
                            sorted(registry.thinking_spec(provider.id, m.id).levels)
                            if m.thinking
                            else []
                        ),
                        # 自动发现的条目在前端单独成组，绝不与已策展条目混淆。
                        "discovered": m.discovered,
                        "usable": usable,
                    }
                )
            providers.append(
                {
                    "id": provider.id,
                    "display": provider.display,
                    "secret_required": provider.secret_required,
                    "available": available,
                    "models": models,
                }
            )
        return {"providers": providers}

    # ------------------------------------------------------- roster overrides

    def _apply_overrides(
        cfg: Config, overrides: list[_NodeOverride], *, tuning: _RunTuning | None = None
    ) -> Config:
        """把会话级阵容覆盖应用到一个深拷贝上；任何未知输入都拒绝（422）。

        全局配置永远不被污染；续跑时从会话 meta 里读回同一份覆盖后的配置。
        """
        registry = get_registry()
        effective = cfg.model_copy(deep=True)
        by_id = {n.id: n for n in (*effective.nodes,)}
        for override in overrides:
            node = by_id.get(override.node_id)
            if node is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"未知节点 {override.node_id!r}（不在当前配置中）",
                )
            if override.model:
                model = registry.model(override.model)
                if model is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"模型 {override.model!r} 不在注册表中",
                    )
                node.model = override.model
                # 关键：连适配器一起切换到该模型所属厂商——否则演示用
                # fake 节点选了真实模型后仍在本地演戏（0ms 假回复），
                # 用户的 API 密钥与思考参数永远不生效。
                # cli_session 模型：切到 cli_session 适配器并把 settings.cli
                # 设为模型 id 前缀（agy / codex / claude）。
                owner = next(
                    (
                        pid
                        for pid, provider in registry.providers.items()
                        if model.id in provider.models
                    ),
                    None,
                )
                if owner == "cli_session":
                    node.adapter = "cli_session"
                    node.settings["cli"] = model.id.split(":", 1)[0]
                elif owner:
                    node.adapter = owner
            if override.thinking is not None:
                if override.thinking:
                    model = registry.model(node.model)
                    if model is None or not model.thinking:
                        raise HTTPException(
                            status_code=422,
                            detail=f"模型 {node.model!r} 不支持思考等级",
                        )
                    spec = registry.thinking_spec(node.adapter, node.model)
                    if override.thinking not in spec.levels:
                        known = "、".join(sorted(spec.levels)) or "（该模型无可用档位）"
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"模型 {node.model!r} 不支持思考等级 {override.thinking!r}；"
                                f"可选档位：{known}"
                            ),
                        )
                node.thinking = override.thinking or None
        # 用户只给部分节点选了真实模型（其余行的模型框留空 = 保持默认）时，
        # 仍停留在 fake 演示档的节点不应参与本场讨论：它们的 0ms 模板假方案
        # 会混入提案与「选主案」弹窗候选，看起来像多出了没选过的参与者。
        # 规则：只要本场对任一参与者显式选了模型，就把「未被点名 + 仍是
        # fake 演示适配器」的参与者禁用；真实模型默认行不受影响。
        touched = {o.node_id for o in overrides}
        picked_any = any(
            o.model
            for o in overrides
            if (n := by_id.get(o.node_id)) is not None and n.role is NodeRole.PARTICIPANT
        )
        if picked_any:
            for node in effective.nodes:
                if (
                    node.role is NodeRole.PARTICIPANT
                    and node.enabled
                    and node.id not in touched
                    and node.adapter == "fake"
                ):
                    node.enabled = False
            live = [n for n in effective.nodes if n.role is NodeRole.PARTICIPANT and n.enabled]
            if live:
                # 只留 1~2 名讨论者时，quorum 与 active_nodes 随实际参与者收缩：
                # 否则引擎会因低于 min_quorum 永远冻结等待，config 也会因
                # 「active_nodes ≠ 参与者数量」在存储读回时校验失败。
                effective.failure.min_quorum = min(effective.failure.min_quorum, len(live))
                effective.council.active_nodes = len(live)
        # 运行参数调优：覆盖全局 idle_s / total_s / max_retries / failure policy
        if tuning:
            if tuning.idle_s is not None:
                effective.timeout.idle_s = tuning.idle_s
            if tuning.total_s is not None:
                effective.timeout.total_s = tuning.total_s
            if tuning.max_retries is not None:
                effective.failure.max_retries = tuning.max_retries
            if tuning.policy is not None:
                try:
                    effective.failure.policy = FailurePolicy(tuning.policy)
                except ValueError as err:
                    raise HubError(f"未知的失败策略：{tuning.policy}") from err
        return effective

    # --------------------------------------------------------------- sessions

    @app.post("/api/sessions", status_code=202)
    async def create_session(body: _NewSession, request: Request) -> dict[str, str]:
        manager = _manager(request)
        session_id = new_session_id()
        uploads = [(f.name, f.content) for f in body.files]
        try:
            # 放在 try 内：参数覆盖的校验失败（未知模型 / 未知策略）应回 4xx，
            # 而不是冒到外面变成 500
            effective = _apply_overrides(manager.config, body.overrides, tuning=body.tuning)
            await manager.start_new(session_id, body.question, uploads, config=effective)
        except UploadRejected as err:
            raise HTTPException(status_code=400, detail=str(err)) from None
        except HubError as err:
            raise HTTPException(status_code=409, detail=str(err)) from None
        except SecretError as err:
            # 阵容切到真实厂商后缺少对应密钥：明确告诉用户缺哪把钥匙
            raise HTTPException(status_code=409, detail=str(err)) from None
        return {"session_id": session_id}

    @app.post("/api/sessions/{session_id}/resume", status_code=202)
    async def resume_session(
        session_id: str, body: _NewSession, request: Request
    ) -> dict[str, str]:
        manager = _manager(request)
        uploads = [(f.name, f.content) for f in body.files]
        try:
            await manager.resume_existing(session_id, uploads)
        except UploadRejected as err:
            raise HTTPException(status_code=400, detail=str(err)) from None
        except HubError as err:
            raise HTTPException(status_code=409, detail=str(err)) from None
        return {"session_id": session_id}

    @app.get("/api/sessions")
    async def list_sessions(request: Request, limit: int = 50) -> dict[str, Any]:
        return {"sessions": await _manager(request).list_sessions(limit=min(limit, 200))}

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str, request: Request) -> dict[str, Any]:
        try:
            return await _manager(request).digest(session_id)
        except HubError:
            raise _missing(session_id) from None

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        session_id: str, request: Request, since: int = 0, limit: int = 500
    ) -> dict[str, Any]:
        manager = _manager(request)
        if await manager.store.get_session(session_id) is None and manager.hub(session_id) is None:
            raise _missing(session_id)
        events = await manager.events(session_id, since=since, limit=min(limit, 5000))
        return {"events": events}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request) -> dict[str, bool]:
        manager = _manager(request)
        # 运行中的会话先终止并卸载，再抹掉存储
        if (hub := manager.hub(session_id)) is not None:
            with contextlib.suppress(HubError):
                hub.stop()
            await manager.drop(session_id)
        deleted = await manager.store.delete_session(session_id)
        if not deleted:
            raise _missing(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/actions")
    async def session_action(
        session_id: str, body: _ActionBody, request: Request
    ) -> dict[str, Any]:
        manager = _manager(request)
        hub = manager.hub(session_id)
        if hub is None:
            raise _missing(session_id)
        try:
            if body.action == "pause":
                hub.pause()
            elif body.action == "resume":
                hub.resume()
            elif body.action == "stop":
                hub.stop()
            elif body.action == "policy":
                if body.policy is None:
                    raise HubError("切换失败策略需要提供 policy 值")
                hub.set_policy(body.policy)
            else:
                raise HTTPException(status_code=422, detail=f"未知动作：{body.action}")
        except HubError as err:
            raise HTTPException(status_code=409, detail=str(err)) from None
        return await manager.digest(session_id)

    @app.post("/api/sessions/{session_id}/asks/{ask_id}")
    async def answer_ask(
        session_id: str, ask_id: str, body: dict[str, Any], request: Request
    ) -> dict[str, bool]:
        manager = _manager(request)
        hub = manager.hub(session_id)
        if hub is None:
            raise _missing(session_id)
        if not hub.answer(ask_id, body):
            raise HTTPException(status_code=404, detail=f"无效或已失效的提问 {ask_id}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/export")
    async def export_session(session_id: str, request: Request, raw: bool = False) -> Response:
        manager = _manager(request)
        meta = await manager.store.get_session(session_id)
        if meta is None:
            raise _missing(session_id)
        events = await manager.store.events(session_id)
        state = replay(session_id, meta.config, events)
        content = render_markdown(state, meta.config, raw=raw)
        filename = f"council-{session_id}.md"
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---------------------------------------------------------------- keys

    @app.get("/api/keys")
    async def keys_overview(request: Request) -> dict[str, Any]:
        manager = _manager(request)
        env_file = manager._data_dir / ".env"
        statuses = {
            name: secrets.secret_status(name, env_file=env_file) for name, _ in PRESET_SECRETS
        }
        return {
            "presets": [{"name": name, "label": label_key} for name, label_key in PRESET_SECRETS],
            "statuses": statuses,
        }

    @app.get("/api/keys/{name}")
    async def key_status(name: str, request: Request) -> dict[str, str]:
        manager = _manager(request)
        source = secrets.secret_status(name, env_file=manager._data_dir / ".env")
        if source == "unset":
            raise HTTPException(status_code=404, detail=f"密钥 {name} 未配置")
        return {"name": name, "source": source}

    @app.post("/api/keys")
    async def key_set(body: _KeyBody, request: Request) -> dict[str, Any]:
        manager = _manager(request)
        try:
            secrets.set_secret(body.name, body.value)
        except secrets.SecretError as err:
            raise HTTPException(status_code=409, detail=str(err)) from None
        source = secrets.secret_status(body.name, env_file=manager._data_dir / ".env")
        return {
            "ok": True,
            "name": body.name,
            "source": source,
            # env / dotenv win over the keyring at resolve time — say so loudly.
            "shadowed": source in ("env", "dotenv"),
        }

    @app.delete("/api/keys/{name}")
    async def key_delete(name: str, request: Request) -> dict[str, bool]:
        manager = _manager(request)
        source = secrets.secret_status(name, env_file=manager._data_dir / ".env")
        if source != "keyring":
            raise HTTPException(
                status_code=404,
                detail=f"密钥 {name} 不在钥匙串中（环境变量 / .env 需在那里另行移除）",
            )
        try:
            secrets.delete_secret(name)
        except secrets.SecretError as err:
            raise HTTPException(status_code=409, detail=str(err)) from None
        return {"ok": True}

    # ------------------------------------------------------------- websocket

    @app.websocket("/api/sessions/{session_id}/ws")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        manager = cast("SessionManager | None", app.state.manager)
        if manager is None:
            await websocket.close(code=4404)
            return
        hub = manager.hub(session_id)
        if hub is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        hub.subscribe(queue)

        async def sender() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                await websocket.send_json(item)

        sender_task = asyncio.create_task(sender())
        try:
            hello = await websocket.receive_json()
            since = int(hello.get("since", 0) or 0)
            # Replay through the same ordered queue so nothing races the sender.
            stored = await manager.events(session_id, since=since, limit=2000)
            queue.put_nowait({"kind": "replay", "events": stored})
            while True:
                message = await websocket.receive_json()
                kind = message.get("kind")
                if kind == "hello":
                    continue
                if kind == "action":
                    action = str(message.get("action") or "")
                    try:
                        if action == "pause":
                            hub.pause()
                        elif action == "resume":
                            hub.resume()
                        elif action == "stop":
                            hub.stop()
                        elif action == "policy":
                            policy = message.get("policy")
                            if policy:
                                hub.set_policy(str(policy))
                    except HubError as err:
                        queue.put_nowait({"kind": "error", "message": str(err)})
                elif kind == "answer":
                    ask_id = str(message.get("ask_id") or "")
                    payload = message.get("payload")
                    if isinstance(payload, dict) and not hub.answer(ask_id, payload):
                        queue.put_nowait(
                            {"kind": "error", "message": f"无效或已失效的提问 {ask_id}"}
                        )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)
            queue.put_nowait(None)
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task

    # ----------------------------------------------------------------- static

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app

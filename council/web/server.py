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
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..adapters import build_adapters
from ..core import secrets
from ..core.config import Config
from ..core.contracts import Adapter
from ..core.ids import new_session_id
from ..core.paths import sessions_db
from ..core.state import replay
from ..core.store import EventStore
from ..export import render_markdown
from ..registry import get_registry
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


class _NewSession(BaseModel):
    question: str = Field(min_length=0, max_length=20000)
    files: list[_UploadFile] = Field(default_factory=list)
    overrides: list[_NodeOverride] = Field(default_factory=list)


class _ActionBody(BaseModel):
    action: str
    policy: str | None = None


class _KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=_KEY_NAME)
    value: str = Field(min_length=1, max_length=4096)


def _missing(session_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"找不到会话 {session_id}")


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
) -> FastAPI:
    """Build the app. ``resume_timeout_s`` defaults to 0 → the hub's own
    generous in-process value is used (test doubles may pass a small one)."""
    from .sessionhub import _RESUME_TIMEOUT_S

    effective_timeout = resume_timeout_s or _RESUME_TIMEOUT_S

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
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
        try:
            yield
        finally:
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

    @app.get("/api/models")
    async def models_catalog() -> dict[str, Any]:
        """注册表目录：按厂商分组，供网页端阵容编辑器的下拉框使用。"""
        registry = get_registry()
        providers = []
        for pid in sorted(registry.providers):
            provider = registry.providers[pid]
            providers.append(
                {
                    "id": provider.id,
                    "display": provider.display,
                    "secret_required": provider.secret_required,
                    "models": [
                        {
                            "id": m.id,
                            "display": m.display or m.id,
                            "thinking": m.thinking,
                            "thinking_levels": (
                                sorted(registry.thinking_spec(provider.id, m.id).levels)
                                if m.thinking
                                else []
                            ),
                        }
                        for m in provider.models.values()
                    ],
                }
            )
        return {"providers": providers}

    # ------------------------------------------------------- roster overrides

    def _apply_overrides(cfg: Config, overrides: list[_NodeOverride]) -> Config:
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
        return effective

    # --------------------------------------------------------------- sessions

    @app.post("/api/sessions", status_code=202)
    async def create_session(body: _NewSession, request: Request) -> dict[str, str]:
        manager = _manager(request)
        session_id = new_session_id()
        uploads = [(f.name, f.content) for f in body.files]
        effective = _apply_overrides(manager.config, body.overrides)
        try:
            await manager.start_new(session_id, body.question, uploads, config=effective)
        except UploadRejected as err:
            raise HTTPException(status_code=400, detail=str(err)) from None
        except HubError as err:
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
    async def list_sessions(
        request: Request, limit: int = 50
    ) -> dict[str, Any]:
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
            try:
                hub.stop()
            except HubError:
                pass
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
    async def export_session(
        session_id: str, request: Request, raw: bool = False
    ) -> Response:
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
            name: secrets.secret_status(name, env_file=env_file)
            for name, _ in PRESET_SECRETS
        }
        return {
            "presets": [
                {"name": name, "label": label_key} for name, label_key in PRESET_SECRETS
            ],
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

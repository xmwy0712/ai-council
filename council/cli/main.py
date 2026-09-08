"""``council`` command line interface.

The CLI is a thin shell over :class:`council.core.orchestrator.CouncilEngine`:
it wires a SQLite store, the adapter registry and a console intervention
handler, then gets out of the way. Every behaviour it exposes is also available
head-less, which is exactly what the web UI will reuse later.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import typer

from ..adapters import build_adapters
from ..core.config import Config, ConfigError, default_config, load_config
from ..core.contracts import Adapter
from ..core.events import (
    CallCompleted,
    CallFailed,
    ConfigChanged,
    Event,
    EventType,
    PhaseCompleted,
    PhaseStarted,
    SessionStatusChanged,
)
from ..core.ids import new_session_id
from ..core.orchestrator import (
    CouncilEngine,
    DecisionRequest,
    EngineStopped,
    InterventionAction,
    InterventionDecision,
    InterventionRequest,
    SelectionRequest,
    Sink,
)
from ..core.paths import config_path, data_dir, sessions_db
from ..core.state import SessionState, SessionStatus
from ..core.store import EventStore

__all__ = ["app"]


def _force_utf8_console() -> None:
    """Windows consoles boot into a legacy code page (cp936/gbk).

    The frozen exe inherits that, so the first ``⚠`` or ``✓`` in a report would
    raise UnicodeEncodeError and kill the process. Reconfigure both streams to
    UTF-8; ``errors="replace"`` keeps output flowing on exotic terminals.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - redirected / custom stream
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_console()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="AI Council：让多个 AI 就同一个问题做结构化会谈，并收敛出一份可执行方案。",
)


# --------------------------------------------------------------------- config


def _registry_warnings(config: Config) -> list[str]:
    from ..registry import get_registry

    try:
        return get_registry().validate_config(config)
    except Exception as err:
        return [f"注册表加载失败：{err}"]


def _resolve_config(path: Path | None, data: Path) -> tuple[Config, str]:
    candidate = Path(path) if path else config_path(data)
    if candidate.is_file():
        return load_config(candidate), str(candidate)
    if path is not None:
        raise ConfigError(f"配置文件不存在：{candidate}")
    return default_config(), "内置默认配置（fake 适配器，无需任何密钥）"


# ------------------------------------------------------------------ rendering


def _make_sink(verbose: bool) -> Sink:
    async def sink(event: Event) -> None:
        payload = event.payload
        if event.type is EventType.PHASE_STARTED:
            assert isinstance(payload, PhaseStarted)
            typer.echo(f"\n── {payload.phase} 开始（round={payload.round}, V{payload.version}）")
        elif event.type is EventType.CALL_CHUNK:
            if verbose:
                typer.echo(payload.text, nl=False)
        elif event.type is EventType.CALL_COMPLETED:
            assert isinstance(payload, CallCompleted)
            repaired = f"，修复 {payload.repaired} 次" if payload.repaired else ""
            typer.echo(
                f"   ✓ {payload.node_id} @ {payload.phase}（{payload.latency_ms} ms{repaired}）"
            )
        elif event.type is EventType.CALL_FAILED:
            assert isinstance(payload, CallFailed)
            typer.secho(
                f"   ✗ {payload.node_id} @ {payload.phase}：{payload.kind} - {payload.message}",
                fg=typer.colors.RED,
            )
        elif event.type is EventType.SESSION_STATUS:
            assert isinstance(payload, SessionStatusChanged)
            if payload.status in (
                SessionStatus.PAUSED.value,
                SessionStatus.AWAITING_USER.value,
                SessionStatus.STALLED.value,
            ):
                typer.secho(f"   ⏸ {payload.status}：{payload.reason}", fg=typer.colors.YELLOW)
        elif event.type is EventType.PHASE_COMPLETED:
            assert isinstance(payload, PhaseCompleted)
            typer.echo(f"── {payload.phase} 完成")

    return sink


class ConsoleHandler:
    """Answers engine interventions on stdin, in a thread so the loop keeps running."""

    async def on_failure(self, req: InterventionRequest) -> InterventionDecision:
        typer.secho(
            f"\n⚠ 节点 {req.node_id} 在 {req.phase} 失败：{req.kind} - {req.message}",
            fg=typer.colors.YELLOW,
        )
        options: list[tuple[str, InterventionAction]] = [
            ("继续（重试该节点）", InterventionAction.RETRY),
            ("等待（暂停后重试）", InterventionAction.WAIT),
            ("换模型", InterventionAction.SWITCH_MODEL),
            ("换适配器", InterventionAction.SWITCH_ADAPTER),
            ("踢出该节点", InterventionAction.DROP_NODE),
            ("终止会议", InterventionAction.ABORT),
        ]
        for index, (label, _) in enumerate(options, start=1):
            typer.echo(f"  {index}. {label}")
        raw = await asyncio.to_thread(input, "请选择 [1]: ")
        choice = raw.strip() or "1"
        if not choice.isdigit() or not 1 <= int(choice) <= len(options):
            return InterventionDecision(action=InterventionAction.RETRY)
        action = options[int(choice) - 1][1]
        model: str | None = None
        adapter: str | None = None
        if action is InterventionAction.SWITCH_MODEL:
            model = (await asyncio.to_thread(input, "新的 model id: ")).strip() or None
        if action is InterventionAction.SWITCH_ADAPTER:
            adapter = (await asyncio.to_thread(input, "新的 adapter 名: ")).strip() or None
        return InterventionDecision(action=action, model=model, adapter=adapter)

    async def on_select(self, req: SelectionRequest) -> str:
        typer.secho(f"\n请选择主案（{req.reason}）", fg=typer.colors.CYAN)
        for candidate in req.candidates:
            typer.echo(f"  - {candidate}")
        try:
            raw = await asyncio.to_thread(input, "输入节点 id: ")
        except EOFError:
            # 非交互运行（管道 / CI）：无 stdin 可读，保守取首个候选
            typer.secho("（无交互输入，自动采用首个候选）", fg=typer.colors.YELLOW)
            return req.candidates[0] if req.candidates else ""
        raw = raw.strip()
        return raw if raw in req.candidates else (req.candidates[0] if req.candidates else "")

    async def on_decision(self, req: DecisionRequest) -> str:
        typer.secho(f"\n需要你的决定（{req.phase}）：{req.question}", fg=typer.colors.CYAN)
        return (await asyncio.to_thread(input, "请输入你的决定: ")).strip()


async def _ask_drift(drift: ConfigChanged) -> str:
    typer.secho("\n配置已变更，与本次会话建立时不同：", fg=typer.colors.YELLOW)
    for line in drift.diff or ["（无结构化差异）"]:
        typer.echo(f"  - {line}")
    raw = await asyncio.to_thread(input, "沿用旧配置继续 [1] / 用新配置继续 [2]（默认 1）: ")
    return "new" if raw.strip() == "2" else "old"


# ------------------------------------------------------------------- session


async def _execute(
    question: str,
    session_id: str,
    config: Config,
    data: Path,
    verbose: bool,
    files: list[str] | None = None,
) -> SessionState:
    store = await EventStore(sessions_db(data)).open()
    adapters: dict[str, Adapter] = build_adapters(config)
    engine = CouncilEngine(
        store=store,
        config=config,
        adapters=adapters,
        handler=ConsoleHandler(),
        sink=_make_sink(verbose),
        adapter_builder=build_adapters,
        drift_handler=_ask_drift,
    )
    try:
        return await engine.run(session_id, question, files=files)
    finally:
        for adapter in adapters.values():
            await adapter.close()
        await store.close()


def _report(state: SessionState, session_id: str, data: Path) -> None:
    final = state.current_proposal or {}
    typer.echo("\n" + "=" * 60)
    if state.status is SessionStatus.COMPLETED:
        typer.secho("会议完成 · 最终方案", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho(f"会议中断（{state.status.value}）· 当前最新方案", fg=typer.colors.YELLOW)
    typer.echo("=" * 60)
    typer.echo(f"主案作者：{state.selected}    版本：V{state.version}")
    typer.echo("")
    typer.echo(str(final.get("proposal", "")))
    steps = final.get("steps") or []
    if steps:
        typer.echo("\n执行步骤：")
        for index, step in enumerate(steps, start=1):
            typer.echo(f"  {index}. {step}")
    questions = final.get("open_questions") or []
    if questions:
        typer.echo("\n待确认问题：")
        for item in questions:
            typer.echo(f"  - {item}")
    typer.echo("")
    done = sum(1 for c in state.calls.values() if c.status == "completed")
    typer.echo(
        f"轮次 {len(state.rounds)} | 完成调用 {done} | "
        f"tokens {state.usage.input_tokens}↓ {state.usage.output_tokens}↑"
    )
    if state.status is not SessionStatus.COMPLETED:
        typer.echo(f"续跑：council resume {session_id} --data-dir {data}")


# ------------------------------------------------------------------ commands


@app.command()
def run(
    question: str = typer.Argument(..., help="要交给智囊团讨论的问题"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
    session: str | None = typer.Option(None, "--session", help="指定会话 id"),
    file: list[str] | None = typer.Option(None, "--file", "-f", help="附件"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="实时打印模型输出流"),
) -> None:
    """开一场新的会谈。"""
    data = Path(data_dir_) if data_dir_ else data_dir()
    cfg, source = _resolve_config(config, data)
    session_id = session or new_session_id()

    typer.echo(f"配置来源：{source}")
    typer.echo(f"会话 id：{session_id}")
    typer.echo(
        f"参与者（{len(cfg.participants)} 名）："
        + "、".join(n.display or n.id for n in cfg.participants)
    )
    if cfg.judge_node is not None:
        typer.echo(f"Judge：{cfg.judge_node.display or cfg.judge_node.id}")
    for warning in [*cfg.warnings(), *_registry_warnings(cfg)]:
        typer.secho(f"⚠ {warning}", fg=typer.colors.YELLOW)

    try:
        state = asyncio.run(_execute(question, session_id, cfg, data, verbose, files=file))
    except KeyboardInterrupt:
        typer.secho("\n已中断，断点已落盘。", fg=typer.colors.YELLOW)
        typer.echo(f"续跑：council resume {session_id} --data-dir {data}")
        raise typer.Exit(code=130) from None
    except EngineStopped as err:
        typer.secho(f"\n会议被终止：{err}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from None
    except ConfigError as err:
        _print_config_error(err)
        raise typer.Exit(code=2) from None
    _report(state, session_id, data)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="要续跑的会话 id"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
    file: list[str] | None = typer.Option(None, "--file", "-f", help="附件"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="实时打印模型输出流"),
) -> None:
    """从断点继续：已完成的结果一律复用，只重发中断或失败的调用。"""
    data = Path(data_dir_) if data_dir_ else data_dir()
    cfg, _ = _resolve_config(config, data)
    try:
        state = asyncio.run(_execute("", session_id, cfg, data, verbose, files=file))
    except EngineStopped as err:
        typer.secho(f"\n会议被终止：{err}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from None
    except ConfigError as err:
        _print_config_error(err)
        raise typer.Exit(code=2) from None
    _report(state, session_id, data)


@app.command("sessions")
def list_sessions(
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """列出可续跑的会话。"""

    async def _list() -> None:
        data = Path(data_dir_) if data_dir_ else data_dir()
        store = await EventStore(sessions_db(data)).open()
        try:
            rows = await store.list_sessions(limit=limit)
        finally:
            await store.close()
        if not rows:
            typer.echo("还没有任何会话。")
            return
        for row in rows:
            typer.echo(f"{row.session_id}  {row.status:<12}  {row.question[:48]}")

    asyncio.run(_list())


@app.command("export")
def export(
    session_id: str = typer.Argument(..., help="要导出的会话 id"),
    output: Path = typer.Argument(..., help="目标 .md 文件路径（用户主动指定的唯一写入点）"),
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
    raw: bool = typer.Option(False, "--raw", help="跳过净化，导出原始输出"),
) -> None:
    """把会话导出为一份 Markdown 纪要（默认经净化管线）。"""
    from ..core.state import replay
    from ..core.store import EventStore
    from ..export import export_markdown

    async def _run() -> None:
        data = Path(data_dir_) if data_dir_ else data_dir()
        store = await EventStore(sessions_db(data)).open()
        try:
            meta = await store.get_session(session_id)
            if meta is None:
                typer.secho(f"找不到会话 {session_id}", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            state = replay(session_id, meta.config, await store.events(session_id))
            target = export_markdown(state, meta.config, output, raw=raw)
        finally:
            await store.close()
        typer.secho(f"✓ 已导出：{target}", fg=typer.colors.GREEN)

    asyncio.run(_run())


@app.command("config-check")
def config_check(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
) -> None:
    """校验配置文件，输出人类可读的错误。"""
    data = Path(data_dir_) if data_dir_ else data_dir()
    try:
        cfg, source = _resolve_config(config, data)
    except ConfigError as err:
        _print_config_error(err)
        raise typer.Exit(code=2) from None
    typer.secho(f"✓ 配置合法：{source}", fg=typer.colors.GREEN)
    for warning in [*cfg.warnings(), *_registry_warnings(cfg)]:
        typer.secho(f"⚠ {warning}", fg=typer.colors.YELLOW)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（默认只在本机）"),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="监听端口"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    data_dir_: Path | None = typer.Option(None, "--data-dir", help="应用数据目录"),
) -> None:
    """启动本地 Web UI：开新会谈、暂停/恢复、人工干预与导出都走浏览器。"""
    import uvicorn  # type: ignore[import-untyped]

    from ..web.server import create_app

    data = Path(data_dir_) if data_dir_ else data_dir()
    cfg, source = _resolve_config(config, data)
    typer.echo(f"配置来源：{source}")
    for warning in [*cfg.warnings(), *_registry_warnings(cfg)]:
        typer.secho(f"⚠ {warning}", fg=typer.colors.YELLOW)
    web_app = create_app(data_dir=data, config=cfg)
    typer.secho(
        f"✓ AI Council Web UI：http://{host}:{port}  （数据目录：{data}）",
        fg=typer.colors.GREEN,
    )
    uvicorn.run(web_app, host=host, port=port, log_level="info")


def _print_config_error(err: ConfigError) -> None:
    typer.secho(f"✗ {err}", fg=typer.colors.RED)
    for problem in err.problems:
        typer.echo(f"  - {problem}")


key_app = typer.Typer(no_args_is_help=True, help="密钥管理：只写系统钥匙串，绝不回显值。")
app.add_typer(key_app, name="key")


@key_app.command("set")
def key_set(
    name: str = typer.Argument(..., help="密钥名，如 OPENAI_API_KEY"),
    value: str | None = typer.Option(None, "--value", help="值；省略则从 stdin 读取"),
) -> None:
    """把一个密钥写入 OS keyring。"""
    from ..core.secrets import SecretError, set_secret

    if value is None:
        value = typer.prompt(f"请输入 {name} 的值（不回显）", hide_input=True)
    if not value:
        typer.secho("值不能为空。", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    try:
        set_secret(name, value)
    except SecretError as err:
        typer.secho(f"✗ {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    typer.secho(f"✓ {name} 已写入系统钥匙串。", fg=typer.colors.GREEN)


@key_app.command("delete")
def key_delete(name: str = typer.Argument(...)) -> None:
    """从系统钥匙串删除一个密钥。"""
    from ..core.secrets import SecretError, delete_secret

    try:
        delete_secret(name)
    except SecretError as err:
        typer.secho(f"✗ {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    typer.secho(f"✓ {name} 已删除。", fg=typer.colors.GREEN)


@key_app.command("check")
def key_check(name: str = typer.Argument(...)) -> None:
    """只报告密钥是否存在，绝不显示值。"""
    from ..core.secrets import resolve

    typer.echo(f"{name}：{'已配置' if resolve(name) else '未找到'}")


def main() -> None:  # pragma: no cover - console-script shim
    app()


if __name__ == "__main__":  # pragma: no cover - python -m council.cli.main
    app()

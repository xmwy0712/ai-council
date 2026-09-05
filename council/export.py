"""Export: render a session as Markdown and write it to a user-chosen path.

Writing happens only here and only because the user typed an output path —
the one explicit write the model output is allowed to trigger. Sanitising is
applied for display quality; ``raw=True`` bypasses it.
"""

from __future__ import annotations

from pathlib import Path

from .core.config import Config
from .core.state import SessionState, SessionStatus
from .sanitize import sanitize

__all__ = ["export_markdown", "render_markdown"]


def render_markdown(
    state: SessionState,
    config: Config,
    *,
    raw: bool = False,
) -> str:
    final = state.current_proposal or {}
    from .core.config import SanitizeSection

    section = config.sanitize if not raw else SanitizeSection(enabled=False)

    lines: list[str] = [
        "# AI Council 会谈纪要",
        "",
        f"- 会话：`{state.session_id}`",
        f"- 问题：{state.question or '（空）'}",
        f"- 状态：{state.status.value}",
        f"- 主案作者：{state.selected or '（未选定）'}",
        f"- 最终版本：V{state.version}",
        f"- 轮次：{len(state.rounds)}",
        "",
        "## 最终方案",
        "",
        sanitize(str(final.get("proposal", "")), section),
    ]
    steps = final.get("steps") or []
    if steps:
        lines += ["", "### 执行步骤"]
        lines += [f"{i}. {sanitize(str(step), section)}" for i, step in enumerate(steps, start=1)]
    assumptions = final.get("assumptions") or []
    if assumptions:
        lines += ["", "### 前提假设"]
        lines += [f"- {sanitize(str(item), section)}" for item in assumptions]
    risks = final.get("risks") or []
    if risks:
        lines += ["", "### 风险"]
        lines += [f"- {sanitize(str(item), section)}" for item in risks]
    questions = final.get("open_questions") or []
    if questions:
        lines += ["", "### 待确认问题"]
        lines += [f"- {sanitize(str(item), section)}" for item in questions]

    if state.decisions:
        lines += ["", "## 人工决定"]
        for decision in state.decisions:
            lines.append(f"- {sanitize(str(decision.get('decision')), section)}")

    convergence = state.convergence
    if convergence:
        lines += ["", "## 收敛过程"]
        for point in convergence:
            lines.append(
                f"- round {point.get('round')}：必须修改 {point.get('must_fix')}，"
                f"建议 {point.get('should_fix')}，分歧 {point.get('disputes')}，"
                f"{'改善' if point.get('improved') else '未改善'}"
            )

    lines += ["", "---", ""]
    if state.status is not SessionStatus.COMPLETED:
        lines.append(
            f"> 本次会谈未完成（{state.status.value}）。可运行 "
            f"`council resume {state.session_id}` 续跑。"
        )
    return "\n".join(lines)


def export_markdown(
    state: SessionState, config: Config, output: Path, *, raw: bool = False
) -> Path:
    output = Path(output).expanduser()
    if output.exists() and not output.is_file():
        raise ValueError(f"导出目标不是文件：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(state, config, raw=raw), encoding="utf-8")
    return output

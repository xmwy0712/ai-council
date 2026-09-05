"""M4 integration: single-plan gate, attachments through the engine, export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import cfg, make_engine
from typer.testing import CliRunner

from council.adapters.fake import default_reply
from council.cli.main import app
from council.core.config import ConfigError
from council.core.state import SessionStatus

runner = CliRunner()

VIOLATING = json.dumps(
    {
        "proposal": "方案一：独立缓存，读写直连。\n方案二：共享缓存，走消息总线。\n",
        "assumptions": [],
        "risks": [],
        "steps": ["决定后执行"],
        "open_questions": [],
    },
    ensure_ascii=False,
)
SINGLE = json.dumps(
    {
        "proposal": "采用共享缓存单一方案：所有服务统一走消息总线。",
        "assumptions": ["缓存命中率可观测"],
        "risks": ["总线单点需高可用"],
        "steps": ["第一步", "第二步"],
        "open_questions": [],
    },
    ensure_ascii=False,
)


# ------------------------------------------------------- single-plan gate


async def test_multi_plan_proposal_is_auto_corrected(tmp_path) -> None:
    state_box = {"replies": 0}

    def first_violates_then_single(req: Any) -> str:
        if req.metadata.get("node_id") == "n1" and req.metadata.get("phase") == "P1":
            state_box["replies"] += 1
            return VIOLATING if state_box["replies"] == 1 else SINGLE
        return default_reply(req)

    harness = await make_engine(tmp_path, responders={"n1": first_violates_then_single})
    state = await harness.engine.run("s1", "问题")

    assert "n1" in state.proposals
    assert "共享缓存" in state.proposals["n1"]["proposal"]
    completed = [e for e in harness.of_type("CallCompleted") if e.payload.node_id == "n1"]
    assert any(e.payload.repaired == 1 for e in completed)


async def test_persistent_multi_plan_offender_is_rejected(tmp_path) -> None:
    def always_violates(req: Any) -> str:
        if req.metadata.get("node_id") == "n1" and req.metadata.get("phase") == "P1":
            return VIOLATING
        return default_reply(req)

    config = cfg(participants=3)
    config.failure.policy = "continue"
    harness = await make_engine(tmp_path, config=config, responders={"n1": always_violates})
    state = await harness.engine.run("s1", "问题")

    assert "n1" not in state.proposals
    summaries = harness.summaries("P1")
    assert summaries[0]["absentees"] == ["n1"]
    # 1 attempt + 2 strict repairs, then rejection — no silent acceptance.
    p1_calls = [c for c in harness.adapters["n1"].calls if c.metadata.get("phase") == "P1"]
    assert len(p1_calls) == 3


async def test_revision_must_also_be_single_plan(tmp_path) -> None:
    def stuck_judge(req: Any) -> str:
        if req.metadata.get("phase") == "P5" and req.metadata.get("round") == "1":
            return json.dumps(
                {
                    "status": "NEED_REVISION",
                    "must_fix": [{"id": "i1", "severity": "must", "text": "必须改"}],
                    "should_fix": [],
                    "disputes": [],
                    "rationale": "改",
                },
                ensure_ascii=False,
            )
        return default_reply(req)

    state_box = {"author_revisions": 0}

    def revision_violates_then_fixed(req: Any) -> str:
        if req.metadata.get("node_id") == "n1" and req.metadata.get("phase") == "P6":
            state_box["author_revisions"] += 1
            if state_box["author_revisions"] == 1:
                return VIOLATING
            return json.dumps(
                {
                    "proposal": "改为唯一方案：只做共享缓存。",
                    "assumptions": [],
                    "risks": [],
                    "steps": ["执行"],
                    "open_questions": [],
                    "change_summary": "合并两个方案",
                },
                ensure_ascii=False,
            )
        return default_reply(req)

    harness = await make_engine(
        tmp_path,
        responders={"judge": stuck_judge, "n1": revision_violates_then_fixed},
    )
    state = await harness.engine.run("s1", "问题")

    assert state.version == 2
    assert "唯一方案" in state.rounds[0].revision["proposal"]  # type: ignore[index]


# ------------------------------------------------------------- attachments


async def test_engine_loads_attachments_and_reports_truncation(tmp_path) -> None:
    note = tmp_path / "note.md"
    note.write_text("# 背景\n我们要扩张。", encoding="utf-8")
    data = tmp_path / "data.json"
    data.write_text('{"da u": 100}', encoding="utf-8")
    # rewrite as valid JSON
    data.write_text('{"dau": 100}', encoding="utf-8")

    harness = await make_engine(tmp_path)
    state = await harness.engine.run("s1", "问题", files=[str(note), str(data)])

    assert state.status is SessionStatus.COMPLETED
    p0 = harness.summaries("P0")
    assert p0[0]["attachments"] == ["note.md", "data.json"]
    assert p0[0]["truncated"] is False


async def test_resume_without_attachments_is_rejected(tmp_path) -> None:
    note = tmp_path / "note.md"
    note.write_text("正文", encoding="utf-8")
    harness = await make_engine(tmp_path)
    await harness.engine.run("s1", "问题", files=[str(note)])
    await harness.store.close()

    second = await make_engine(tmp_path)
    with pytest.raises(ConfigError, match="--file"):
        await second.engine.run("s1", "")
    await second.store.close()


async def test_engine_rejects_bad_attachment(tmp_path) -> None:
    bad = tmp_path / "fake.json"
    bad.write_text("not json", encoding="utf-8")
    harness = await make_engine(tmp_path)
    from council.core.attachments import AttachmentRejected

    with pytest.raises(AttachmentRejected, match="不是合法 JSON"):
        await harness.engine.run("s1", "问题", files=[str(bad)])
    await harness.store.close()


# ------------------------------------------------------------------ export


def test_export_end_to_end(tmp_path) -> None:
    import asyncio

    async def _seed() -> None:
        harness = await make_engine(tmp_path)
        await harness.engine.run("s1", "一个值得导出的问题")
        await harness.store.close()

    asyncio.run(_seed())
    result = runner.invoke(
        app,
        ["export", "s1", str(tmp_path / "纪要.md"), "--data-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    exported = (tmp_path / "纪要.md").read_text(encoding="utf-8")
    assert "AI Council 会谈纪要" in exported
    assert "最终方案" in exported
    assert "一个值得导出的问题" in exported


def test_render_markdown_respects_sanitize_and_raw() -> None:
    from council.core.config import default_config
    from council.core.state import SessionState
    from council.export import render_markdown

    config = default_config()
    state = SessionState("s1", config)
    state.current_proposal = {
        "proposal": "好的，我来回答。**加粗方案**：用Python做",
        "steps": ["**第一步**"],
    }
    state.status = SessionStatus.COMPLETED
    cleaned = render_markdown(state, config)
    assert "好的，我来回答" not in cleaned
    assert "**加粗方案**" not in cleaned
    assert "用 Python 做" in cleaned
    raw_out = render_markdown(state, config, raw=True)
    assert "**加粗方案**" in raw_out
    assert "好的，我来回答" in raw_out


def test_export_target_path_is_created(tmp_path: Path) -> None:
    from council.core.config import default_config
    from council.core.state import SessionState
    from council.export import export_markdown

    config = default_config()
    state = SessionState("s1", config)
    state.current_proposal = {"proposal": "方案正文"}
    state.status = SessionStatus.COMPLETED
    target = tmp_path / "out" / "纪要.md"
    export_markdown(state, config, target)
    assert target.is_file()
    assert "方案正文" in target.read_text(encoding="utf-8")

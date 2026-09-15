"""回归测试：start_new 返回时会话行必须已经落盘（消除「刚建的会话列不出来」竞态）。

背景（CI 上偶发失败的真正原因，且是真实的用户可见问题）：
  SessionHub.start() 只做 asyncio.create_task(self._run(...)) 就返回，
  而「会话行」是在 _run → engine.run() 内部才写入的。
  于是 POST /api/sessions 返回 202 后立刻 GET /api/sessions，
  可能看不到刚建的那个——界面上表现为「刚开的会谈，历史里要等一下才出现」。

  这个缺陷在 CI 上表现为 test_history_is_untouched_by_ui_change 偶发失败：
      assert '20260915-081937-31a576' in set()   ← 列表为空

修法：start() 里等会话行落盘再返回（等的是「已发生的事实」，不改引擎流程）。

这里用**确定性**方式测契约，而不是多跑几次碰运气：
  监视 store.create_session 是否在 start_new 返回之前被调用过。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import cfg, make_adapters

from council.core.store import EventStore
from council.web import sessionhub as _sessionhub_module
from council.web.sessionhub import SessionManager


@pytest.fixture(autouse=True)
def _fast_select(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sessionhub_module, "_SELECT_TIMEOUT_S", 1.0)


def test_session_row_exists_when_start_new_returns(tmp_path: Path) -> None:
    """start_new 返回时，会话行必须已经在 store 里。

    这是「POST 202 之后立刻列得出」的充要条件。
    """

    async def scenario() -> tuple[bool, bool, int]:
        conf = cfg(participants=3)
        store = EventStore(tmp_path / "sessions.sqlite3")
        await store.open()
        try:
            manager = SessionManager(
                store=store,
                config=conf,
                data_dir=tmp_path,
                adapter_builder=lambda c: make_adapters(c),
            )

            called: list[str] = []
            original = store.create_session

            async def spy(session_id: str, config: Any, **kwargs: Any) -> None:
                called.append(session_id)
                await original(session_id, config, **kwargs)

            store.create_session = spy  # type: ignore[method-assign]

            sid = "race-contract-1"
            await manager.start_new(sid, "竞态契约", config=conf)

            # 契约一：写会话的动作在 start_new 返回前已经发生
            wrote_before_return = sid in called

            # 契约二：立刻查询列表就能看到（不用 sleep 等待）
            metas = await store.list_sessions()
            visible = sid in {m.session_id for m in metas}
            return wrote_before_return, visible, len(metas)
        finally:
            await store.close()

    wrote, visible, total = asyncio.run(scenario())
    assert wrote, "start_new 返回时会话行尚未写入——调用方会扑空"
    assert visible, f"start_new 返回后立刻 list_sessions 看不到它（共 {total} 条）"


def test_await_session_row_times_out_without_raising(tmp_path: Path) -> None:
    """等不到会话行时不该抛错（服务照常响应，会话随后自行出现）。"""

    async def scenario() -> bool:
        conf = cfg(participants=3)
        store = EventStore(tmp_path / "sessions.sqlite3")
        await store.open()
        try:
            manager = SessionManager(
                store=store,
                config=conf,
                data_dir=tmp_path,
                adapter_builder=lambda c: make_adapters(c),
            )
            hub = manager.hub("does-not-exist")
            assert hub is None
            # 直接构造一个 hub 来测等待函数本身
            from council.web.sessionhub import SessionHub

            h = SessionHub(
                session_id="never-written",
                store=store,
                config=conf,
                adapter_builder=lambda c: make_adapters(c),
            )
            return await h._await_session_row(timeout_s=0.05)
        finally:
            await store.close()

    assert asyncio.run(scenario()) is False, "不存在的会话应超时返回 False 而非抛错"


def test_start_new_and_immediate_list_via_api(tmp_path: Path) -> None:
    """HTTP 层复现：POST 之后立刻 GET 列表，必须包含新会话（不重试）。"""
    from fastapi.testclient import TestClient

    from council.web.server import create_app

    app = create_app(
        data_dir=tmp_path, config=cfg(participants=3), adapter_builder=lambda c: make_adapters(c)
    )
    with TestClient(app) as client:
        r = client.post("/api/sessions", json={"question": "立刻可见"})
        assert r.status_code == 202, r.text
        sid = r.json()["session_id"]
        items = client.get("/api/sessions").json()
        rows = items.get("sessions", items)
        assert sid in {s["session_id"] for s in rows}, "POST 返回后立刻列表应能看到它"

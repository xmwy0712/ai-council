"""「换适配器 / 换模型」改成下拉选择后的回归测试。

背景：原先两者都是纯文本框，用户得凭空敲 adapter 名 / model id；
而「换适配器」还需要知道有哪些适配器、换了之后模型还认不认。
现在改成下拉：候选来自 /api/models（含 provider 声明的协议），
并在选适配器时提示它对应的协议，避免换完仍不匹配。

硬约束（用户明确要求）：这个改动不得影响历史记录。见
test_history_is_untouched_by_ui_change。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import cfg, make_adapters
from fastapi.testclient import TestClient

from council.web import sessionhub as _sessionhub_module
from council.web.server import create_app

ASSETS = Path(__file__).resolve().parents[1] / "council" / "web" / "static"


@pytest.fixture(autouse=True)
def _fast_select(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sessionhub_module, "_SELECT_TIMEOUT_S", 1.0)


def _client(tmp_path: Path, config: Any = None, **kwargs: Any) -> TestClient:
    config = config if config is not None else cfg(participants=3)
    app = create_app(
        data_dir=tmp_path, config=config, adapter_builder=lambda c: make_adapters(c, **kwargs)
    )
    return TestClient(app)


# ------------------------------------------------------------------ 后端数据源
def test_models_api_exposes_adapter_per_provider(tmp_path: Path) -> None:
    """/api/models 必须给出每个厂商的调用协议，供前端做联动提示。"""
    with _client(tmp_path) as client:
        data = client.get("/api/models").json()
        providers = data["providers"]
        assert providers, "应有 provider"
        for p in providers:
            assert p.get("adapter"), f"{p['id']} 缺 adapter 字段"


def test_openai_compatible_vendor_reports_its_protocol(tmp_path: Path) -> None:
    """声明 adapter=openai_api 的厂商，接口里应如实报出（而非自己的 id）。"""
    with _client(tmp_path) as client:
        providers = {p["id"]: p for p in client.get("/api/models").json()["providers"]}
        # 注册表里 deepseek / zhipu 等声明了 openai_api
        for vid in ("deepseek", "zhipu", "moonshot"):
            if vid in providers:
                assert providers[vid]["adapter"] == "openai_api", f"{vid} 协议应为 openai_api"


# ------------------------------------------------------------------ 前端
def test_ask_panel_uses_a_picker_for_switch_adapter() -> None:
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "buildAdapterChoices" in js, "应有适配器候选构造器"
    assert "mountChoicePicker" in js, "应挂出选择器而非裸文本框"

    # switch_adapter 分支必须走 mountChoicePicker
    seg = js.split('ask.type === "failure"')[1].split('ask.type === "select"')[0]
    assert 'value === "switch_adapter"' in seg
    assert "mountChoicePicker" in seg, "换适配器应调用选择器"


def test_adapter_picker_lists_builtins_and_vendors() -> None:
    """候选要同时包含内置适配器名与厂商名（两者对工厂等价）。"""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    seg = js.split("function buildAdapterChoices()")[1].split("function ")[0]
    for name in ("openai_api", "anthropic_api", "google_api", "cli_session", "generic_http"):
        assert name in seg, f"内置适配器 {name} 应在候选中"
    assert "MODELS" in seg, "厂商候选应来自 /api/models"


def test_switching_adapter_warns_about_model_compatibility() -> None:
    """选适配器时必须提示协议名，提醒用户确认模型是否匹配。"""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    seg = js.split("function mountChoicePicker(")[1].split("function ")[0]
    assert "ask.adapter.note" in seg, "应有协议提示"
    assert "protocol" in seg

    zh = json.loads((ASSETS / "i18n" / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((ASSETS / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in (
        "ask.adapter.note",
        "ask.adapter.hint",
        "ask.switch_adapter.ph",
        "ask.switch_model.ph",
        "ask.adapters.builtin",
        "ask.adapters.vendor",
    ):
        assert zh.get(key), f"zh 缺 {key}"
        assert en.get(key), f"en 缺 {key}"


def test_model_picker_only_offers_usable_models() -> None:
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    seg = js.split("function buildModelChoices()")[1].split("function ")[0]
    assert "available === false" in seg, "应跳过不可用厂商"
    assert "usable !== false" in seg, "应跳过不可用模型"


# ------------------------------------------------------------------ 历史记录（硬要求）
def test_history_is_untouched_by_ui_change(tmp_path: Path) -> None:
    """UI 改动不得影响历史记录：同一数据目录二次打开，会话与事件完全一致。

    用户明确要求「历史对话记录更新后必须还在」。

    这条测试曾在 CI 上偶发失败（`assert sid in set()` ← 列表为空）。
    根因不是测试写法，而是**产品竞态**：SessionHub.start() 只 create_task 就返回，
    会话行由后台任务稍后才写。已修（见 test_session_visibility.py）。
    所以这里保持严格断言——它正是一道防线。
    """
    with _client(tmp_path) as client:
        r = client.post("/api/sessions", json={"question": "历史保留验证"})
        assert r.status_code == 202, r.text
        sid = r.json()["session_id"]

        first = client.get("/api/sessions").json()
        first_items = first.get("sessions", first)
        first_ids = {s["session_id"] for s in first_items}
        assert sid in first_ids, "新建会话应立即可见（见 test_session_visibility）"

        ev_before = client.get(f"/api/sessions/{sid}/events").json()
        n_before = len(ev_before.get("events", ev_before))
        assert n_before > 0

    # 重新打开同一目录（模拟升级后重启）
    with _client(tmp_path) as client:
        second = client.get("/api/sessions").json()
        second_items = second.get("sessions", second)
        second_ids = {s["session_id"] for s in second_items}
        assert sid in second_ids, "重启后历史会话必须还在"

        # 既有事件一条不少，且 seq 不丢
        ev_after = client.get(f"/api/sessions/{sid}/events").json()
        after_items = ev_after.get("events", ev_after)
        assert len(after_items) >= n_before, "事件数不得减少"
        before_seq = [e.get("seq") for e in (ev_before.get("events", ev_before))]
        after_seq = {e.get("seq") for e in after_items}
        missing = [s for s in before_seq if s not in after_seq]
        assert not missing, f"重启后丢失了 {len(missing)} 条既有事件：{missing[:5]}"

        # 会话仍可打开（配置能反序列化 → 旧记录不会被新代码读崩）
        detail = client.get(f"/api/sessions/{sid}")
        assert detail.status_code == 200

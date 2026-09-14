"""网页端运行参数：total_s（单次调用总时长上限）的传递链测试。

覆盖三层，任一层断掉都会让这个设置变成空头承诺：
  - 前端：index.html 有输入框、app.js 会收集并提交、语言文件有文案
  - 传输：POST /api/sessions 接受 tuning.total_s，越界被拒
  - 落地：引擎实际持有的配置里确实是提交的值
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
        data_dir=tmp_path,
        config=config,
        adapter_builder=lambda c: make_adapters(c, **kwargs),
    )
    return TestClient(app)


# ------------------------------------------------------------------ 前端
def test_settings_page_has_a_total_s_input() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    tune = html.split("tune-grid")[1].split("</div>")[0]
    assert 'id="tune-total"' in tune, "运行参数面板应有「单次调用上限」输入框"
    assert 'min="60"' in tune and 'max="7200"' in tune, "范围应与后端约束一致（60–7200）"


def test_app_js_collects_and_persists_total_s() -> None:
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    collect = js.split("function collectTuning()")[1].split("function ")[0]
    assert "out.total_s" in collect, "提交时必须带上 total_s"

    bind = js.split("function bindTuningInputs()")[1].split("function ")[0]
    assert "tune-total" in js, "应绑定输入框"
    assert "TUNING.total_s" in bind, "变更时应写入 TUNING"
    # 范围夹取与后端 le=7200 / 前端 max=7200 对齐
    assert "7200" in bind, "应把输入夹取在 7200 以内"

    render = js.split("function renderTuningInputs()")[1].split("function ")[0]
    assert "TUNING.total_s" in render, "刷新页面后应回填已保存的值"


def test_locale_has_total_label() -> None:
    for lang in ("zh", "en"):
        data = json.loads((ASSETS / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert data.get("tune.total"), f"{lang} 缺少 tune.total 文案"


# ------------------------------------------------------------------ 传输 + 落地
def test_total_s_from_web_lands_in_engine_config(tmp_path: Path) -> None:
    """网页端提交的 total_s 必须出现在引擎实际持有的配置里。"""
    with _client(tmp_path) as client:
        r = client.post(
            "/api/sessions",
            json={"question": "超大会话", "tuning": {"idle_s": 600, "total_s": 3600}},
        )
        assert r.status_code == 202, r.text
        sid = r.json()["session_id"]
        hub = client.app.state.manager.hub(sid)
        assert hub is not None
        assert hub._config.timeout.total_s == 3600
        assert hub._config.timeout.idle_s == 600


def test_out_of_range_total_s_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        r = client.post("/api/sessions", json={"question": "x", "tuning": {"total_s": 99999}})
        assert 400 <= r.status_code < 500, f"越界值应被拒，实得 {r.status_code}"


def test_default_when_total_not_provided(tmp_path: Path) -> None:
    """不提交时沿用配置默认（900s），不该被写成别的值。"""
    with _client(tmp_path) as client:
        r = client.post("/api/sessions", json={"question": "默认"})
        assert r.status_code == 202, r.text
        sid = r.json()["session_id"]
        hub = client.app.state.manager.hub(sid)
        assert hub is not None
        assert hub._config.timeout.total_s == 900.0

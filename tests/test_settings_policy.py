"""把失败策略搬进设置后的门禁测试。

验证策略确实随新会谈提交并生效。
`_apply_overrides` 是 create_app 内的闭包，无法直接导入，因此走真实 HTTP 路径，
并从**会话重放出的 effective config** 读取结果——这才是引擎实际用的那份。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import cfg, make_adapters
from fastapi.testclient import TestClient

from council.core.config import FailurePolicy
from council.web import sessionhub as _sessionhub_module
from council.web.server import create_app

ASSETS = Path(__file__).resolve().parents[1] / "council" / "web" / "static"


@pytest.fixture(autouse=True)
def _fast_select_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake Judge 转人工选主案：测试里 1 秒兜底自动取首个候选，避免挂等。"""
    monkeypatch.setattr(_sessionhub_module, "_SELECT_TIMEOUT_S", 1.0)


def _client(tmp_path: Path, config: Any = None, **kwargs: Any) -> TestClient:
    config = config if config is not None else cfg(participants=3)
    app = create_app(
        data_dir=tmp_path,
        config=config,
        adapter_builder=lambda c: make_adapters(c, **kwargs),
    )
    return TestClient(app)


def _start(client: TestClient, question: str, tuning: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"question": question}
    if tuning is not None:
        body["tuning"] = tuning
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 202, response.text
    return str(response.json()["session_id"])


def _effective_policy(client: TestClient, session_id: str) -> str:
    """读会话 hub 实际持有的配置里的失败策略。

    这是引擎真正使用的那份 config（hub._config），不是请求体里的期望值。
    """
    hub = _manager_of(client).hub(session_id)
    assert hub is not None, f"会话 {session_id} 的 hub 不存在"
    return str(hub._config.failure.policy)


def _manager_of(client: TestClient) -> Any:
    """从 TestClient 拿到底层 SessionManager。"""
    app = client.app  # type: ignore[attr-defined]
    for attr in ("state",):
        state = getattr(app, attr, None)
        manager = getattr(state, "manager", None) if state else None
        if manager is not None:
            return manager
    raise AssertionError("拿不到 SessionManager")


@pytest.mark.parametrize("policy", ["continue", "pause", "ask_user"])
def test_created_session_reports_the_policy_from_tuning(tmp_path: Path, policy: str) -> None:
    """设置里选的策略，应成为新会话实际使用的失败策略。"""
    with _client(tmp_path) as client:
        sid = _start(client, f"策略 {policy}", tuning={"policy": policy})
        assert _effective_policy(client, sid) == policy, f"{policy} 未生效"


def test_default_session_keeps_configured_policy(tmp_path: Path) -> None:
    """不带 tuning 时，沿用配置里的默认策略（ask_user）。"""
    with _client(tmp_path) as client:
        sid = _start(client, "默认策略")
        assert _effective_policy(client, sid) == FailurePolicy.ASK_USER.value


def test_unknown_policy_is_rejected(tmp_path: Path) -> None:
    """未知策略不能静默通过。"""
    with _client(tmp_path) as client:
        response = client.post(
            "/api/sessions",
            json={"question": "坏策略", "tuning": {"policy": "nonsense"}},
        )
        assert 400 <= response.status_code < 500, f"应拒绝未知策略，实得 {response.status_code}"


def test_session_page_no_longer_carries_policy_and_raw() -> None:
    """会议页去掉策略下拉与 raw 勾选框，只留只读状态与导出按钮。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    session = html.split('id="view-session"')[1]
    assert 'id="policy-select"' not in session, "会议页不应再有策略下拉"
    assert 'id="raw-toggle"' not in session, "会议页不应再有 raw 勾选框"
    assert 'id="btn-export"' in session, "导出按钮应保留"
    assert 'id="raw-chip"' in session, "导出格式应以只读提示反映在会议页"


def test_settings_page_owns_policy_and_export_format() -> None:
    """策略进「运行参数」分组，导出格式进「通用」分组。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    # 设置页到 session 页之间：不能用 </section> 切，里面还有嵌套的 section
    settings = html.split('id="view-settings"')[1].split('id="view-session"')[0]

    tune = settings.split("tune-grid")[1].split("</div>")[0]
    assert 'id="tune-policy"' in tune, "策略应与卡死超时/重试次数同列（运行参数）"

    general_pos = settings.index("settings.group.general")
    keys_pos = settings.index("keys.title")
    raw_pos = settings.index('id="raw-toggle"')
    assert general_pos < raw_pos < keys_pos, "导出格式应落在「通用」分组里"


def test_locale_has_keys_for_relocated_controls() -> None:
    zh = json.loads((ASSETS / "i18n" / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((ASSETS / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in ("settings.raw.enable", "settings.raw.on", "settings.raw.off", "tune.policy"):
        assert zh.get(key), key
        assert en.get(key), key


def test_i18n_dependent_renders_run_after_locale_loads() -> None:
    """依赖译文的首屏渲染必须排在 loadLocale 之后。

    曾经的缺陷：applyExportRaw() 写在 loadLocale 之前，启动时译文表还是空的，
    于是会话页的只读提示直接显示成了键名「settings.raw.off」。
    翻译在加载完成前会是 undefined，写进界面就是键名本身。

    这里用「文件里的出现位置」判定先后：boot() 内 applyExportRaw() 必须在
    await loadLocale(...) 之后。
    """
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    boot = js.split("async function boot()")[1].split("document.addEventListener")[0]

    locale_at = boot.index("await loadLocale(")
    export_at = boot.index("applyExportRaw()")
    assert export_at > locale_at, (
        "applyExportRaw() 必须排在 loadLocale 之后，否则界面会显示 settings.raw.* 键名"
    )

    # 切换语言后也要重刷该提示（否则语言切了、这行文字不变）
    switch = js.split("function switchLang(")[1].split("const POLICY_VALUES")[0]
    assert "applyExportRaw()" in switch, "语言切换后需重刷导出格式提示"
    assert "renderPolicyOptions()" in switch, "语言切换后需重刷失败策略选项"

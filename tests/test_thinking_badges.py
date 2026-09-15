"""回归测试：模型旁的思考档位徽标 + DeepSeek V4.1-Flash 注册。

两件事：
1. 注册表补入官方新模型 deepseek-flash（V4.1-Flash，2026-09-10 发布），
   旧的 deepseek-v4-flash 已下线、仅作兼容路由，不应再作为可选模型出现。
2. 模型下拉每条要显示思考档位徽标，不支持思考的显式标注而非留空。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import cfg, make_adapters
from fastapi.testclient import TestClient

import council
from council.registry import Registry
from council.web.server import create_app

ASSETS = Path(__file__).resolve().parents[1] / "council" / "web" / "static"
TOML = Path(__file__).resolve().parents[1] / "council" / "registry" / "deepseek.toml"


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(Path(council.__file__).parent / "registry", include_discovered=False)


# ---------------------------------------------------------------- 注册表
def test_deepseek_flash_is_registered(registry: Registry) -> None:
    """官方新模型 deepseek-flash 必须可选。"""
    assert registry.model("deepseek-flash") is not None, "缺 deepseek-flash（V4.1-Flash）"


def test_deepseek_flash_has_thinking_levels(registry: Registry) -> None:
    """它有思考档位，且是官方声明的 low / high / max。"""
    model = registry.model("deepseek-flash")
    assert model is not None
    assert model.thinking is True, "deepseek-flash 应支持思考"
    spec = registry.thinking_spec("deepseek", "deepseek-flash")
    assert spec is not None, "应有思考声明"
    assert set(spec.levels) == {"low", "high", "max"}
    assert spec.param == "reasoning_effort"


def test_retired_deepseek_ids_are_not_offered(registry: Registry) -> None:
    """已下线的旧 id 不该继续出现在可选模型里。"""
    for old in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        assert registry.model(old) is None, f"{old} 已下线，不应仍可选"


def test_deepseek_toml_records_verification(registry: Registry) -> None:
    """注册表要留下核实依据（日期 + 来源），便于日后复查。"""
    text = TOML.read_text(encoding="utf-8")
    assert "2026-09-15" in text, "应记录本次核实日期"
    assert "docs" in text or "source" in text, "应记录来源"


# ---------------------------------------------------------------- 接口
def test_models_api_reports_thinking_levels(tmp_path: Path) -> None:
    """每个模型都要带 thinking 与 thinking_levels，供前端渲染徽标。

    注意：``thinking=True`` 不等于有可调档位——例如 kimi-k2.7-code 官方文档写明
    「始终思考，无 reasoning_effort」。那种情况下 thinking_levels 为空是**正确**的，
    前端会显式标「无思考档位」。所以这里只断言字段存在，不要求二者同真。
    """
    app = create_app(
        data_dir=tmp_path, config=cfg(participants=3), adapter_builder=lambda c: make_adapters(c)
    )
    with TestClient(app) as client:
        data = client.get("/api/models").json()
        with_levels = 0
        for p in data["providers"]:
            for m in p["models"]:
                assert "thinking" in m, f"{m['id']} 缺 thinking"
                assert "thinking_levels" in m, f"{m['id']} 缺 thinking_levels"
                assert isinstance(m["thinking_levels"], list), f"{m['id']} 档位表应为列表"
                if m["thinking_levels"]:
                    with_levels += 1
        assert with_levels > 0, "至少应有模型带可调档位"


def test_deepseek_flash_is_usable_via_api(tmp_path: Path) -> None:
    """新模型要真的出现在 /api/models 里（而不是只在 TOML 中）。"""
    app = create_app(
        data_dir=tmp_path, config=cfg(participants=3), adapter_builder=lambda c: make_adapters(c)
    )
    with TestClient(app) as client:
        data = client.get("/api/models").json()
    ids = [m["id"] for p in data["providers"] for m in p["models"]]
    assert "deepseek-flash" in ids, "deepseek-flash 应出现在模型目录里"
    assert "deepseek-v4-flash" not in ids, "已下线的旧 id 不应出现"


# ---------------------------------------------------------------- 前端
def test_picker_renders_a_badge_per_model() -> None:
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "thinkingBadge" in js, "应有档位徽标构造器"
    seg = js.split("function thinkingBadge(")[1].split("function ")[0]
    assert "thinking_levels" in seg, "徽标应读档位表"
    assert "picker.no_thinking" in seg, "不支持思考的应显式标注"

    # 渲染模型条目时要挂上徽标
    render = js.split("group.models.forEach(")[1].split("});")[0]
    assert "thinkingBadge(m)" in render, "模型条目应带徽标"
    assert "mp-name" in render, "名字与徽标应分开成两个元素"


def test_badge_collapses_long_level_lists() -> None:
    """档位多（如 GPT-6 五档）时折叠为前两档 + 「+N」，避免撑破一行。"""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    seg = js.split("function thinkingBadge(")[1].split("function ")[0]
    assert "slice(0, 2)" in seg, "超过三档应只列两个"
    assert "mp-badge-more" in seg, "应显示 +N"


def test_badge_styles_exist() -> None:
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    for cls in (".mp-badge", ".mp-badge-none", ".mp-badge-more", ".mp-opt .mp-name"):
        assert cls in css, f"缺样式 {cls}"


def test_locale_has_no_thinking_label() -> None:
    for lang in ("zh", "en"):
        data = json.loads((ASSETS / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert data.get("picker.no_thinking"), f"{lang} 缺 picker.no_thinking"

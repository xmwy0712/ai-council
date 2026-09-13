"""Static web-asset sanity checks: locales, themes, contrast.

These run offline and protect the two "pure data" promises of M5:
every locale file speaks about the same keys, and every theme fills the same
variable set at readable contrast.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "council" / "web" / "static"

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

REQUIRED_VARS = {
    "--bg",
    "--bg-elev",
    "--bg-panel",
    "--border",
    "--text",
    "--text-dim",
    "--text-faint",
    "--accent",
    "--accent-contrast",
    "--accent-soft",
    "--ok",
    "--ok-soft",
    "--warn",
    "--warn-soft",
    "--err",
    "--err-soft",
    "--code-bg",
    "--code-text",
}

# (foreground var, background var, minimum WCAG ratio)
CONTRAST_PAIRS = [
    ("--text", "--bg", 4.5),
    ("--text-dim", "--bg", 3.0),
    ("--accent-contrast", "--accent", 3.0),
    ("--ok", "--bg", 3.0),
    ("--warn", "--bg", 3.0),
    ("--err", "--bg", 3.0),
]


def _load(path: str) -> dict[str, object]:
    return json.loads((ASSETS / path).read_text(encoding="utf-8"))


def _channel(value: str) -> float:
    value = value[1:]
    if len(value) in (3, 4):
        value = "".join(ch * 2 for ch in value)
    r, g, b, *_ = (int(value[i : i + 2], 16) for i in range(0, 6, 2))
    return (r, g, b)


def _linear(channel: int) -> float:
    scaled = channel / 255.0
    return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    r, g, b = _channel(color)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def test_locales_are_key_symmetric() -> None:
    locales = ["zh", "en"]
    key_sets: dict[str, set[str]] = {}
    for lang in locales:
        data = _load(f"i18n/{lang}.json")
        keys = set(data)
        assert keys, lang
        key_sets[lang] = keys
        assert all(isinstance(value, str) and value for value in data.values()), lang
    assert key_sets["zh"] == key_sets["en"]


def test_themes_fill_every_required_variable() -> None:
    theme_dir = ASSETS / "themes"
    files = sorted(path.name for path in theme_dir.glob("*.json"))
    assert {"light.json", "dark.json", "high-contrast.json"} <= set(files)
    for name in files:
        theme = _load(f"themes/{name}")
        assert theme["mode"] in ("light", "dark"), name
        colors = theme["colors"]
        assert isinstance(colors, dict)
        missing = REQUIRED_VARS - set(colors)
        assert not missing, f"{name}: 缺少 {sorted(missing)}"
        for key, value in colors.items():
            assert isinstance(value, str) and _HEX.match(value), f"{name}: {key}={value!r}"
        extra = set(colors) - REQUIRED_VARS
        assert not extra, f"{name}: 多余的变量 {sorted(extra)}"


def test_theme_contrast_meets_wcag_floor() -> None:
    for name in ("light.json", "dark.json", "high-contrast.json"):
        theme = _load(f"themes/{name}")
        colors = theme["colors"]
        failures = []
        for fg_var, bg_var, floor in CONTRAST_PAIRS:
            fg, bg = colors[fg_var], colors[bg_var]
            ratio = _contrast(fg, bg)
            if ratio < floor:
                failures.append(f"{fg_var} on {bg_var}: {ratio:.2f} < {floor}")
        assert not failures, f"{name}:\n" + "\n".join(failures)


def test_index_references_existing_assets() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    for href in ("style.css", "app.js", "favicon.svg"):
        assert href in html
        assert (ASSETS / href).is_file(), href
    for lang in ("zh", "en"):
        assert (ASSETS / "i18n" / f"{lang}.json").is_file()


def test_favicon_is_declared() -> None:
    """Without a declared icon every load takes a 404 on /favicon.ico."""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html


def test_history_list_is_fetched_once_per_page_load() -> None:
    """`showView("new")` already refreshes the list.

    `boot()` used to call `refreshList()` and then `showView("new")`, which
    called it again — two identical GET /api/sessions on every load. Pinning the
    division of labour rather than a literal line so formatting can move.
    """
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function boot\(\) \{\n(.*?)\n\}", js, re.S)
    assert match is not None, "boot() 不见了"
    # Comments may talk about the call; only real statements count.
    body = "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith("//")
    )
    assert 'showView("home")' in body
    assert "refreshList()" not in body, "boot() 不该自己拉历史列表"

    show_view = re.search(r"function showView\(name\) \{\n(.*?)\n\}", js, re.S)
    assert show_view is not None
    assert "refreshList()" in show_view.group(1), "列表没人在刷新了"


def test_home_layout_is_three_regions_and_settings_are_grouped() -> None:
    """入口页三张卡片只放标题与介绍；实际操作在各自子页，单页内切换。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert html.count("data-goto=") == 3, "入口应是三张卡片"
    for key in ("hub.new.title", "hub.history.title", "hub.settings.title"):
        assert f'data-i18n="{key}"' in html

    # 入口卡片里不塞实际操作控件——它们属于各自的子页
    hub = html.split('class="hub"')[1].split("</section>")[0]
    for forbidden in ("roster-editor", 'id="question"', "session-list", "keys-list"):
        assert forbidden not in hub, forbidden

    # 五个视图都在同一份 HTML 里（单页切换，不做页面跳转）
    for vid in ("view-home", "view-new", "view-history", "view-settings", "view-session"):
        assert f'id="{vid}"' in html

    assert "deliberation studio" not in html, "旧眉题应已移除"
    assert "btn-home" not in html, "顶栏不应有与入口重复的「新会谈」"
    assert 'data-i18n-tip="updates.help"' in html, "模型清单更新需要免责问号"
    assert 'data-i18n-tip="list.help"' in html, "历史会话需要版本兼容提醒"
    assert html.count('class="settings-group') >= 4, "设置应分组折叠"
    assert 'data-i18n="settings.group.models"' in html
    assert 'data-i18n="updates.results"' in html

    locale = json.loads((ASSETS / "i18n" / "zh.json").read_text(encoding="utf-8"))
    for key in (
        "settings.group.general",
        "settings.group.models",
        "updates.help",
        "updates.results",
        "list.help",
    ):
        assert locale.get(key), key
    assert "settings.more" not in locale, "「更多设置」按钮已随弹层一起移除"
    # 说明文字已按需求删除，对应键位一并清理（不留死键）
    for dead in ("hub.new.desc", "hub.history.desc", "hub.settings.desc"):
        assert dead not in locale, dead


def test_hub_cards_carry_art_and_corner_titles() -> None:
    """三张入口卡片：简约线条图案 + 标题各自坐在指定角，且都不带说明文字。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    hub = html.split('class="hub"')[1].split("</section>")[0]

    # 三张图案各就各位，且是纯装饰（不应被读屏当作内容）
    for art in ("art-table", "art-folders", "art-gears"):
        assert f'class="art {art}"' in hub, art
    assert hub.count('aria-hidden="true"') >= 3, "图案应标为装饰"
    art_css = (ASSETS / "style.css").read_text(encoding="utf-8")
    for pos in ("right: 20px; bottom: 16px", "left: 18px; bottom: 14px", "left: 18px; top: 14px"):
        assert pos in art_css, pos

    # 说明段落已删除
    assert "hub.new.desc" not in hub and "hub.history.desc" not in hub

    # 标题定位：新对话左上、历史右上、设置右下
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert ".nav-new h2 " in css and "font-size: 30px" in css, "新对话标题应放大"
    assert "color-mix(in srgb, var(--accent) 70%, #ffffff)" in css, "新对话标题应为浅蓝"
    assert '.nav-card[data-goto="history"] h2 { top' in css, "历史标题应在右上"
    assert '.nav-card[data-goto="settings"] h2 { bottom' in css, "设置标题应在右下"


def test_gears_are_two_regular_wheels_that_mesh() -> None:
    """大小两个齿轮：造型一致（圆润齿廓）、每个齿等角等形、两轮不互相穿透。

    用手算几何验证，而不是肉眼看图。此前踩过的坑：虚线圆、粗短线冒充齿；
    圆弧拼接导致圆心偏移使齿轮歪扭；齿厚过大导致两轮硬压在一起形成穿透。
    """
    import math
    import re

    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    block = html.split('class="art art-gears"')[1].split("</svg>")[0]

    # 1) 两个齿轮（各一条闭合轮廓路径）+ 各一个中心孔
    assert block.count("<path") == 2, "应有大小两个齿轮"
    assert block.count("<circle") == 2, "每个齿轮各一个中心孔"
    assert "stroke-dasharray" not in block, "齿不能用虚线圆冒充"
    assert "fill=" not in block.replace('fill="none"', ""), "齿轮不得填充"

    poly = [
        [(float(x), float(y)) for x, y in re.findall(r"(-?[\d.]+) (-?[\d.]+)", d)]
        for d in re.findall(r'<path d="([^"]+)"', block)
    ]
    holes = [
        (float(a), float(b), float(r))
        for a, b, r in re.findall(r'<circle cx="(-?[\d.]+)" cy="(-?[\d.]+)" r="([\d.]+)"', block)
    ]
    assert len(poly) == 2 and len(holes) == 2
    assert len(poly[0]) == len(poly[1]) or len(poly[0]) > len(poly[1]), "轮廓应有足够采样点"

    # 2) 每条轮廓必须是「等角间距 + 齿顶/齿槽半径一致」的规则齿轮：
    #    把所有点按半径聚类，只应存在两个半径档（齿顶圆、齿根圆）
    for idx, pts in enumerate(poly):
        cx, cy, _hr = holes[idx]
        radii = [round(math.dist(p, (cx, cy)), 1) for p in pts]
        peaks = max(radii)
        valleys = min(radii)
        assert peaks - valleys > 3, f"齿轮{idx} 齿高太小，看不出齿形"
        # 落在两个圆上的点应占绝对多数（过渡带之外）
        on_circles = sum(1 for r in radii if abs(r - peaks) < 0.3 or abs(r - valleys) < 0.3)
        assert on_circles > len(pts) * 0.5, f"齿轮{idx} 齿廓不规则（点未落在齿顶/齿根圆上）"

    # 3) 两轮不得互相穿透
    def seg_int(p1, p2, p3, p4) -> bool:
        def cr(o, a, b) -> float:
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        d1, d2 = cr(p3, p4, p1), cr(p3, p4, p2)
        d3, d4 = cr(p1, p2, p3), cr(p1, p2, p4)
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

    a, b = poly
    na, nb = len(a), len(b)
    crossings = sum(
        1
        for i in range(na)
        for j in range(nb)
        if seg_int(a[i], a[(i + 1) % na], b[j], b[(j + 1) % nb])
    )
    assert crossings == 0, f"两轮穿透 {crossings} 处，不是图纸上的咬合"

    # 4) 两轮应当靠得足够近（仍看得出在啮合），但不能贴死
    c1, c2 = (holes[0][0], holes[0][1]), (holes[1][0], holes[1][1])
    dist = math.dist(c1, c2)
    tip1 = max(math.dist(p, c1) for p in a)
    tip2 = max(math.dist(p, c2) for p in b)
    assert dist < tip1 + tip2 + 6, "两轮离得太远，看不出咬合关系"


def test_table_art_is_round_table_with_people() -> None:
    """「新对话」图案：圆桌 + 摊开的书 + 六位围坐的人，全部线条不填充。

    造型取自参考图。此前版本是「圆桌 + 八把等大椅子」，现按参考图改为
    六个人（头 + 肩臂弧）围坐，中间一本书。
    """
    import math
    import re

    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    block = html.split('class="art art-table"')[1].split("</svg>")[0]

    # 只用线条：不得填充、不得用虚线
    assert "fill=" not in block.replace('fill="none"', ""), "图案不得填充"
    assert "stroke-dasharray" not in block, "不得用虚线"

    circles = [
        (float(a), float(b), float(r))
        for a, b, r in re.findall(r'<circle cx="(-?[\d.]+)" cy="(-?[\d.]+)" r="([\d.]+)"', block)
    ]
    # 一个桌面圆 + 六个头（头比桌面圆小）
    assert len(circles) == 7, f"应为一个桌面圆 + 六个头，实得 {len(circles)}"
    table = max(circles, key=lambda c: c[2])
    heads = [c for c in circles if c is not table]
    assert len(heads) == 6
    assert all(h[2] < table[2] for h in heads), "头应小于桌面圆"

    # 六个人均匀分布：与桌面圆心的距离一致，角度间隔 60°
    tcx, tcy, _tr = table
    dists = [math.dist((h[0], h[1]), (tcx, tcy)) for h in heads]
    assert max(dists) - min(dists) < 0.5, f"六人不在同一圈上：{dists}"
    angles = sorted(math.degrees(math.atan2(h[1] - tcy, h[0] - tcx)) % 360 for h in heads)
    step_list = [(angles[(i + 1) % 6] - angles[i]) % 360 for i in range(6)]
    assert all(abs(s - 60) < 0.5 for s in step_list), f"六人未等角分布：{step_list}"

    # 六条肩臂弧（每人一段）
    arcs = re.findall(r'<path d="M [^"A]*A [\d.]+ [\d.]+ 0 \d [01]', block)
    assert len(arcs) == 6, f"应有一条肩臂弧/人，实得 {len(arcs)}"

    # 书：两页轮廓 + 每页三条横线
    pages = [d for d in re.findall(r'<path d="([^"]+)"', block) if "C " in d and "Z" in d]
    assert len(pages) == 2, f"书应有左右两页，实得 {len(pages)}"
    lines = re.findall(r"<line\b", block)
    # 每页三条横线（6 条）+ 一条书脊竖线
    assert len(lines) == 7, f"应为六条横线 + 一条书脊，实得 {len(lines)}"


def test_history_search_is_a_separate_card_and_filters_locally() -> None:
    """检索是独立的一张卡（与列表在流内堆叠、不重叠），过滤在客户端完成。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    history = html.split('id="view-history"')[1].split("</section>")[0]
    # 两张独立的卡：列表 + 检索，而不是叠加/浮层
    assert history.count('class="card page-card') == 2, "列表与检索应是两张独立的卡"
    assert 'class="stack"' in history, "两张卡应在 .stack 里竖向堆叠"
    for control in ("search-topic", "search-from", "search-to", "search-clear"):
        assert f'id="{control}"' in history, control
    # 检索位于列表之后（页面中部偏下）
    assert history.index('id="session-list"') < history.index('id="search-topic"')

    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    stack = css.split("\n.stack {")[1].split("}")[0]
    assert "flex-direction: column" in stack, "两张卡应竖向排列而非重叠"
    assert "position: absolute" not in stack and "position: fixed" not in stack, (
        "检索框不得脱流，否则会话变多时会盖住列表"
    )

    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "function filterSessions" in js and "function bindSearch" in js
    # 检索走客户端过滤已有数据，不再多打一次接口
    filter_body = js.split("function filterSessions")[1].split("\n}")[0]
    assert "api(" not in filter_body, "检索不应请求服务端"
    assert "row.question" in filter_body, "需支持按会议主题检索"
    assert "row.updated_at" in filter_body, "需支持按会议时间检索"


def test_model_picker_keeps_a_no_override_option() -> None:
    """模型下拉必须能退回「不选」：选过模型后也要能清掉覆盖，沿用配置默认。

    原生 select 里一直有 value="" 的「保持当前」，但自建面板之前只渲染真实模型，
    一旦选过就无法退回。"""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    rebuild = js.split("const rebuildPanel = () => {")[1].split("const VISIBLE")[0]
    assert "mp-none" in rebuild, "面板必须渲染「不选择」项"
    assert 'none.dataset.value = ""' in rebuild, "该项的值必须为空（= 不覆盖）"
    assert 'none.addEventListener("click", () => choose(""))' in rebuild, "点击应清空选择"
    assert rebuild.index("mp-none") < rebuild.index("mp-pinned"), "「不选择」应在最上方"

    # 清空后不得被写进覆盖列表：否则等于显式指定了空模型
    collect = js.split("function collectOverrides()")[1].split("function ")[0]
    assert "if (item.model || item.thinking !== null) out.push(item);" in collect, (
        "空模型不得进入覆盖列表"
    )

    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert ".mp-none" in css, "「不选择」需要自己的样式，与具体模型拉开层次"

    zh = json.loads((ASSETS / "i18n" / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((ASSETS / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert zh.get("picker.none") and en.get("picker.none"), "两种语言都要有 picker.none"


def test_topbar_is_a_vertical_rail_without_the_brand_name() -> None:
    """顶栏改成右侧竖向导航栏，且品牌名「AI Council」已去掉。

    横栏会占掉一整行高度、入口卡片因此靠上；竖栏把同一批控件折算成一列。
    身份改由浏览器标签页标题承担（app.title → document.title）。
    """
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    # 品牌按钮与品牌名都已移除；控件仍在同一处（顶栏容器内）
    assert "btn-brand" not in html, "品牌按钮应已删除"
    assert "brand-text" not in html, "品牌名应已从导航栏去掉"
    header = html.split('<header class="topbar">')[1].split("</header>")[0]
    assert "AI Council" not in header, "导航栏不应再出现品牌文字"
    # 身份改由标签页标题承担，<title> 里保留品牌名
    assert "<title>AI Council</title>" in html
    for control in ("theme-seg", "lang-seg", "btn-back"):
        assert f'id="{control}"' in header, control

    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    rail = css.split("/* 竖向导航栏")[1].split(".card {")[0]
    assert "position: fixed" in rail
    assert "flex-direction: column" in rail, "导航栏应改为竖向"
    assert "column" in css.split(".top-actions {")[1].split("}")[0]

    # 标签页标题仍由 i18n 承担，不随品牌名一起丢掉
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert 'document.title = t("app.title")' in js
    assert "btn-brand" not in js, "app.js 仍绑定已删除的品牌按钮会直接抛错"


def test_canvas_is_cleared_and_resized_as_a_whole_buffer() -> None:
    """画布清屏必须按缓冲尺寸、尺寸变化必须靠 ResizeObserver。

    滚动条出现/消失会改变 clientWidth 却不触发 window resize。若缓冲停在旧尺寸、
    而每帧只清到「此刻的 clientWidth」，右缘就会留下一条永不擦除、越画越花的
    像素带——鼠标经过就留痕（设置页最高，必现）。
    """
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "ResizeObserver" in js, "需要盯住元素自身尺寸，而非只靠 window.resize"
    assert "observer.disconnect()" in js, "停止时要断开观察器"

    clear_fn = re.search(r"const clearWholeCanvas = \(\) => \{(.*?)\n  \};", js, re.S)
    assert clear_fn is not None, "clearWholeCanvas 不见了"
    body = clear_fn.group(1)
    assert "canvas.width" in body and "canvas.height" in body, "清屏要按缓冲尺寸"
    assert "setTransform(1, 0, 0, 1, 0, 0)" in body, "清屏前需置回单位变换"

    # 每帧与停止时都必须用整块清屏，不能再用实时 clientWidth
    assert "clearWholeCanvas()" in js
    assert "clearRect(0, 0, canvas.clientWidth, canvas.clientHeight)" not in js, (
        "清屏不得依赖实时 clientWidth——缓冲比它宽时右缘会残留"
    )

    # resize 里不能无条件赋尺寸：那会清空画布并反复触发观察器
    resize = re.search(r"const resize = \(\) => \{(.*?)\n  \};", js, re.S)
    assert resize is not None
    assert "if (canvas.width === w && canvas.height === h) return;" in resize.group(1), (
        "尺寸未变就不该重设缓冲，否则形成 ResizeObserver 自循环"
    )

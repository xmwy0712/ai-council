"""release_notes 工具的测试：确保发布说明的提取与结构不会静默退化。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.release_notes import build, extract

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_extract_finds_each_released_version() -> None:
    """每个已发布版本都能提取到非空正文。"""
    for ver in ("1.3.0", "1.2.2", "1.2.1", "1.2.0", "1.1.1", "1.1.0", "1.0.0"):
        body = extract(ver, CHANGELOG)
        assert body.strip(), f"{ver} 正文为空"
        # 正文不应滚进下一个版本
        assert not body.lstrip().startswith("## ["), f"{ver} 把下一个版本标题吃进来了"


def test_extract_rejects_unknown_version() -> None:
    with pytest.raises(SystemExit):
        extract("9.9.9", CHANGELOG)


def test_extract_strips_trailing_link_definitions() -> None:
    """文件末尾的链接定义区不应混入正文。"""
    body = extract("1.3.0", CHANGELOG)
    assert "[1.3.0]: https://" not in body


def test_build_includes_plain_summary_for_known_version() -> None:
    """有通俗摘要的版本：先给人看的一句话，技术细节折叠在后。"""
    notes = build("v1.3.0", extract("1.3.0", CHANGELOG))
    assert "## 这次更新了什么" in notes
    assert "**一句话：" in notes
    assert "<details>" in notes and "</details>" in notes
    # 通俗摘要在技术正文之前
    assert notes.index("这次更新了什么") < notes.index("### Added")


def test_build_always_has_install_block_and_link() -> None:
    for ver in ("1.3.0", "1.2.0", "1.0.0"):
        notes = build(f"v{ver}", extract(ver, CHANGELOG))
        assert "### 安装 / Install" in notes, ver
        assert "完整变更对比" in notes or "全部提交" in notes, ver


def test_compare_link_points_at_the_real_previous_tag() -> None:
    """对比链接的前一版必须是真实存在的次新版本。

    曾经的缺陷：按版本号递减推算，把 1.3.0 的前一版写成 v1.2.0，
    而实际应为 v1.2.2（1.2.x 有补丁版）。
    """
    notes = build("v1.3.0", extract("1.3.0", CHANGELOG))
    m = re.search(r"/compare/(v[\d.]+)\.\.\.v1\.3\.0", notes)
    assert m, "对比链接格式不对"
    assert m.group(1) == "v1.2.2", f"前一版应为 v1.2.2，实得 {m.group(1)}"


def test_notes_use_lf_newlines() -> None:
    """发布说明应为 LF：仓库与其他 Markdown 都是 LF。"""
    notes = build("v1.3.0", extract("1.3.0", CHANGELOG))
    assert "\r" not in notes


def test_first_release_has_no_bogus_compare() -> None:
    """首个版本没有前一版，不应生成 v0.0.0 之类的不存在链接。"""
    notes = build("v1.0.0", extract("1.0.0", CHANGELOG))
    assert "v0.0.0" not in notes
    assert "全部提交" in notes

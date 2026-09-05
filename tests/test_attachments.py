"""Attachment loading: type allow-list, content sniffing, hard limits."""

from __future__ import annotations

import json

import pytest

from council.core.attachments import (
    AttachmentRejected,
    load_attachments,
    render_zones,
)
from council.core.config import AttachmentSection, BudgetSection


def _section(**overrides) -> AttachmentSection:
    return AttachmentSection(**overrides)


def test_txt_md_json_csv_all_load(tmp_path) -> None:
    files = {
        "a.txt": "纯文本内容",
        "b.md": "# 标题\n正文",
        "c.json": '{"ok": true, "n": 1}',
        "d.csv": "col1,col2\n1,2\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    loaded = load_attachments([tmp_path / n for n in files], _section())
    assert {a.name for a in loaded} == set(files)


def test_pdf_and_unknown_extensions_rejected(tmp_path) -> None:
    (tmp_path / "e.pdf").write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(AttachmentRejected, match="扩展名"):
        load_attachments([tmp_path / "e.pdf"], _section())


def test_disguised_extension_rejected_by_content_sniff(tmp_path) -> None:
    (tmp_path / "fake.json").write_text("这不是 JSON", encoding="utf-8")
    with pytest.raises(AttachmentRejected, match="不是合法 JSON"):
        load_attachments([tmp_path / "fake.json"], _section())


def test_broken_csv_rejected(tmp_path) -> None:
    (tmp_path / "empty.csv").write_text("\n", encoding="utf-8")
    with pytest.raises(AttachmentRejected, match="CSV"):
        load_attachments([tmp_path / "empty.csv"], _section())


def test_non_utf8_rejected_without_silent_replacement(tmp_path) -> None:
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9 latte")  # latin-1
    with pytest.raises(AttachmentRejected, match="UTF-8"):
        load_attachments([tmp_path / "latin.txt"], _section())


def test_nul_bytes_rejected_as_binary_disguise(tmp_path) -> None:
    (tmp_path / "bin.txt").write_bytes(b"text\x00more")
    with pytest.raises(AttachmentRejected, match="NUL"):
        load_attachments([tmp_path / "bin.txt"], _section())


def test_bom_is_tolerated_and_stripped(tmp_path) -> None:
    (tmp_path / "bom.md").write_bytes(b"\xef\xbb\xbf" + "# 带 BOM 的标题".encode())
    loaded = load_attachments([tmp_path / "bom.md"], _section())
    assert not loaded[0].content.startswith("\ufeff")


def test_symlink_rejected_or_skipped(tmp_path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("内容", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台无法创建符号链接")
    with pytest.raises(AttachmentRejected, match="符号链接"):
        load_attachments([link], _section())


def test_directory_rejected(tmp_path) -> None:
    with pytest.raises(AttachmentRejected, match="不是常规文件"):
        load_attachments([tmp_path], _section())


def test_per_file_size_limit(tmp_path) -> None:
    (tmp_path / "big.md").write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(AttachmentRejected, match="单文件上限"):
        load_attachments([tmp_path / "big.md"], _section(max_file_mib=0.001))


def test_total_limit_across_files(tmp_path) -> None:
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("y" * 2048, encoding="utf-8")
    with pytest.raises(AttachmentRejected, match="总上限"):
        load_attachments([tmp_path / f"f{i}.txt" for i in range(3)], _section(max_total_mib=0.001))


def test_file_count_limit(tmp_path) -> None:
    for i in range(4):
        (tmp_path / f"g{i}.txt").write_text("z", encoding="utf-8")
    with pytest.raises(AttachmentRejected, match="超过上限"):
        load_attachments([tmp_path / f"g{i}.txt" for i in range(4)], _section(max_files=3))


def test_duplicate_paths_load_once(tmp_path) -> None:
    (tmp_path / "once.txt").write_text("content", encoding="utf-8")
    loaded = load_attachments([tmp_path / "once.txt", tmp_path / "once.txt"], _section())
    assert len(loaded) == 1


def test_truncation_keeps_head_and_tail(tmp_path) -> None:
    # ~1200 chars: HEAD-/MID-/TAIL- repeated
    content = "HEAD-" * 400 + "MID-" * 400 + "TAIL-" * 400
    (tmp_path / "long.txt").write_text(content, encoding="utf-8")
    loaded = load_attachments([tmp_path / "long.txt"], _section())
    budget = BudgetSection(context_tokens=1000)  # ~2000 chars budget
    zones, truncated = render_zones(loaded, budget)
    assert truncated
    assert zones.startswith("附件 long.txt（已截断）")
    assert "HEAD-" in zones  # head survives
    assert "TAIL-" in zones  # tail survives
    assert "MID-" not in zones  # middle dropped
    assert "……[中间内容已按上下文预算截断]……" in zones


def test_render_zones_marks_every_attachment_as_data(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("内容一", encoding="utf-8")
    (tmp_path / "b.json").write_text('{"k": 1}', encoding="utf-8")
    loaded = load_attachments([tmp_path / "a.txt", tmp_path / "b.json"], _section())
    zones, truncated = render_zones(loaded, BudgetSection())
    assert not truncated
    assert "内容一" in zones
    assert '{"k": 1}' in zones
    assert zones.count("<data_zone") == 0  # caller wraps the combined zone


def test_json_attachment_is_normalised(tmp_path) -> None:
    (tmp_path / "data.json").write_text(' { "a" : 1 } ', encoding="utf-8")
    loaded = load_attachments([tmp_path / "data.json"], _section())
    parsed = json.loads(loaded[0].content)
    assert parsed == {"a": 1}

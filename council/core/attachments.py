"""Read-only attachment loading.

Hard rules, all enforced here before a byte ever reaches a prompt:

* only extensions in ``[attachments].allowed_suffixes`` (default
  ``.txt / .md / .json / .csv``);
* content must match the extension: ``.json`` must strictly parse, ``.csv``
  must parse as CSV, everything must decode as strict UTF-8 (BOM tolerated) —
  a disguised extension is rejected, never guessed around;
* size limits per file / total / count, symlinks and directories rejected,
  opened read-only and released immediately;
* over the context budget the text is truncated head + tail with an explicit
  omission marker, and the caller is told that truncation happened.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

from .config import AttachmentSection, BudgetSection

__all__ = ["Attachment", "AttachmentRejected", "load_attachments", "render_zones"]

_NUL = "\x00"
_OMISSION_MARK = "\n\n……[中间内容已按上下文预算截断]……\n\n"


class AttachmentRejected(ValueError):
    """An attachment failed a hard rule. Message names the file and the rule."""


@dataclass(frozen=True)
class Attachment:
    name: str
    source: Path
    content: str
    truncated: bool = False

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8"))


def _check_path(path: Path, section: AttachmentSection) -> None:
    if path.is_symlink():
        raise AttachmentRejected(f"{path}: 拒绝符号链接")
    if not path.is_file():
        raise AttachmentRejected(f"{path}: 不是常规文件")
    suffix = path.suffix.lower()
    if suffix not in section.allowed_suffixes:
        allowed = "、".join(section.allowed_suffixes)
        raise AttachmentRejected(f"{path}: 扩展名 {suffix!r} 不在允许列表（{allowed}）")
    limit = section.max_file_mib * 1024 * 1024
    if path.stat().st_size > limit:
        raise AttachmentRejected(
            f"{path}: 文件 {path.stat().st_size} 字节超过单文件上限 {section.max_file_mib:g} MiB"
        )


def _decode_strict(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")  # BOM tolerated, then stripped
    except UnicodeDecodeError as err:
        raise AttachmentRejected(f"{path}: 不是 UTF-8 编码（{err.reason}），拒绝静默替换") from err
    if _NUL in text:
        raise AttachmentRejected(f"{path}: 含有 NUL 字节，疑似二进制伪装")
    return text


def _sniff(suffix: str, text: str, path: Path) -> str:
    """Content must honour the extension. Returns normalised text."""
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as err:
            raise AttachmentRejected(
                f"{path}: 不是合法 JSON（{err.msg}，第 {err.lineno} 行）"
            ) from err
    elif suffix == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        if not rows or not rows[0]:
            raise AttachmentRejected(f"{path}: CSV 为空或没有表头行")
    return text


def load_attachments(
    sources: Sequence[str | PurePath | Path], section: AttachmentSection
) -> list[Attachment]:
    """Load every attachment or raise :class:`AttachmentRejected` on the first
    violation. Nothing is partially loaded."""
    if not sources:
        return []
    if len(sources) > section.max_files:
        raise AttachmentRejected(f"附件 {len(sources)} 个，超过上限 {section.max_files}")
    total_limit = section.max_total_mib * 1024 * 1024
    loaded: list[Attachment] = []
    total = 0
    seen: set[Path] = set()
    for source in sources:
        path = Path(source).expanduser()
        resolved = path.resolve()
        if resolved in seen:
            continue  # same file twice: keep one, silently
        seen.add(resolved)
        _check_path(path, section)
        text = _sniff(path.suffix.lower(), _decode_strict(path), path)
        total += path.stat().st_size
        if total > total_limit:
            raise AttachmentRejected(
                f"{path}: 累计 {total} 字节超过总上限 {section.max_total_mib:g} MiB"
            )
        loaded.append(Attachment(name=path.name, source=resolved, content=text))
    return loaded


def _truncate_middle(text: str, budget_chars: int) -> tuple[str, bool]:
    if len(text) <= budget_chars:
        return text, False
    head = budget_chars * 2 // 3
    tail = budget_chars - head
    return text[:head] + _OMISSION_MARK + text[-tail:], True


def render_zones(attachments: Sequence[Attachment], budget: BudgetSection) -> tuple[str, bool]:
    """Render all attachments as data zones under the context budget.

    Returns ``(zone_text, truncated)``. The budget is a character estimate of
    ``budget.context_tokens`` (roughly 2 chars per token for mixed CJK/ASCII).
    When the content overflows, the *largest* files are halved first (head +
    tail, middle dropped), so every file stays represented and the truncation
    marker is explicit.
    """
    if not attachments:
        return "", False
    usable = max(budget.context_tokens * 2, 0)
    overhead = sum(len(a.name) + 48 for a in attachments)
    usable = max(usable - overhead, 0)

    keep = {i: len(a.content) for i, a in enumerate(attachments)}
    while sum(keep.values()) > usable:
        largest = max(keep, key=lambda index: keep[index])
        keep[largest] = max(keep[largest] // 2, 1)

    parts: list[str] = []
    truncated = False
    for index, attachment in enumerate(attachments):
        slice_size = keep[index]
        content, was_truncated = _truncate_middle(attachment.content, slice_size)
        truncated = truncated or was_truncated
        note = "（已截断）" if was_truncated else ""
        parts.append(f"附件 {attachment.name}{note}（来源 {attachment.source}）：\n{content}")
    return "\n\n".join(parts), truncated

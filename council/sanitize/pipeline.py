"""Text sanitising pipeline for display and export.

The event log always keeps ``raw_text``; sanitising is a view, never a lossy
rewrite of history. Rules live in one table, each individually switchable via
``[sanitize] disabled_rules = [...]``, and protected zones (code fences, inline
code, URLs) are masked before any rule runs so cleaning can never corrupt them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..core.config import SanitizeSection
from ..core.single_plan import _restore, mask_protected

__all__ = ["RULES", "SanitizeRule", "sanitize"]

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_FULLWIDTH = r"\uff01-\uff65"


@dataclass(frozen=True)
class SanitizeRule:
    id: str
    description: str
    apply: Callable[[str], str]


def _strip_bold(text: str) -> str:
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    return text


def _strip_stray_marks(text: str) -> str:
    # Bare asterisk runs that are NOT list bullets (a bullet is `* `) and not
    # the `***` HR (handled elsewhere) are decoration.
    text = text.replace("***", "")
    return re.sub(r"(?m)^\*{1,3}(?=[^\s\n])", "", text)


def _strip_orphan_hashes(text: str) -> str:
    return re.sub(r"(?m)^\s*#{1,6}\s*$\n?", "", text)


def _collapse_hr_floods(text: str) -> str:
    return re.sub(r"(?m)^(?:\s*(?:-{3,}|\*{3,}|_{3,})\s*\n){3,}", "---\n", text)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def _strip_trailing_spaces(text: str) -> str:
    return re.sub(r"(?m)[ \t]+$", "", text)


_LATIN: Final = r"[A-Za-z]+"


def _cjk_latin_space(text: str) -> str:
    text = re.sub(rf"(?<=[{_CJK}]){_LATIN}", r" \g<0>", text)
    text = re.sub(rf"{_LATIN}(?=[{_CJK}])", r"\g<0> ", text)
    return text


def _normalize_punct(text: str) -> str:
    """ASCII punctuation glued to CJK text becomes full-width; the reverse
    direction stays untouched so code and numbers never change."""
    pairs = {",": "，", ":": "：", ";": "；", "?": "？", "!": "！"}
    for half, full in pairs.items():
        text = re.sub(
            rf"([{_CJK}\uff08\uff09()]){re.escape(half)}(?=\s|$)",
            r"\1" + full,
            text,
        )
        text = re.sub(rf"{re.escape(half)}([{_CJK}])", full + r"\1", text)
    return text


_EMOJI_RUN: Final = None  # placeholder to keep rule order obvious; see below
import re as _re  # noqa: E402

_EMOJI_RUN = _re.compile(r"([\U0001F300-\U0001FAFF\u2600-\u27BF])\1{1,}")


def _strip_emoji_spam(text: str) -> str:
    return _EMOJI_RUN.sub(r"\1", text)


_OPENER_PREFIXES: Final[tuple[str, ...]] = (
    "好的，我来",
    "好的,我来",
    "好的，",
    "好的,",
    "好的",
    "没问题，",
    "没问题",
    "当然可以，",
    "当然可以",
    "当然，",
    "明白了，",
    "明白了",
    "收到，",
    "收到",
    "作为一个AI，",
    "作为一个 AI，",
    "作为AI，",
    "作为 AI，",
    "我来回答这个问题：",
    "让我来为您解答：",
)
_BOILERPLATE_CLOSE: Final = re.compile(
    r"\n+(?:希望(?:这|以上)?(?:对你)?有帮助[。.!！]?|如果还有(?:任何)?问题.{0,20}[。.!！]?)\s*$"
)


def _strip_boilerplate_open(text: str) -> str:
    """Drop the leading opener when the text opens with a scripted phrase."""
    stripped = text.lstrip()
    matched = next((prefix for prefix in _OPENER_PREFIXES if stripped.startswith(prefix)), None)
    if matched is not None:
        newline = text.find("\n")
        if newline != -1:
            return text[newline + 1 :]
        return stripped[len(matched) :]
    as_ai = re.match(r"^As an AI[^\n]*\n", text)
    if as_ai:
        return text[as_ai.end() :]
    return text


def _strip_boilerplate_close(text: str) -> str:
    return _BOILERPLATE_CLOSE.sub("", text)


@dataclass(frozen=True)
class _Rule:
    rule: SanitizeRule


RULES: tuple[SanitizeRule, ...] = (
    SanitizeRule("strip_bold", "去除装饰性 ** / __ 强调", _strip_bold),
    SanitizeRule("strip_stray_marks", "去除行首孤立 * 与 *** 装饰", _strip_stray_marks),
    SanitizeRule("strip_orphan_hashes", "删除只有 # 的孤立行", _strip_orphan_hashes),
    SanitizeRule("collapse_hr", "合并泛滥的分隔线", _collapse_hr_floods),
    SanitizeRule("collapse_blank_lines", "合并 3 个以上连续空行", _collapse_blank_lines),
    SanitizeRule("strip_trailing_spaces", "去行尾空格", _strip_trailing_spaces),
    SanitizeRule("cjk_latin_space", "统一中英文之间空格", _cjk_latin_space),
    SanitizeRule("normalize_punct", "统一全角半角标点混用", _normalize_punct),
    SanitizeRule("strip_emoji_spam", "压缩表情堆砌", _strip_emoji_spam),
    SanitizeRule("strip_boilerplate_open", "去除模型套话开头", _strip_boilerplate_open),
    SanitizeRule("strip_boilerplate_close", "去除模型套话结尾", _strip_boilerplate_close),
)


def sanitize(text: str, section: SanitizeSection | None = None) -> str:
    section = section or SanitizeSection()
    if not section.enabled:
        return text
    disabled = set(section.disabled_rules)
    masked, tokens = mask_protected(text)
    for rule in RULES:
        if rule.id in disabled:
            continue
        masked = rule.apply(masked)
    return _restore(masked, tokens)

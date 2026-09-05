"""Single-plan enforcement (spec §8).

Every participant may propose exactly ONE plan. The validator scans the
``proposal`` text for alternative/branch patterns, with code blocks and inline
code masked first so that a quoted example can never trigger a rejection.
A hit is a contract violation: the engine re-prompts (up to two strict repairs)
demanding one merged plan, and a persistent offender is rejected into the
failure policy — never silently accepted.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["SINGLE_PLAN_REPAIR_HINT", "find_multi_plan_violations", "mask_protected"]

_FENCE: Final = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE: Final = re.compile(r"`[^`\n]+`")

# Heading-like alternative plans: 方案一 / 选项 A / Option 2 / Plan B / 【方案二】
_TITLED_ALTERNATIVE: Final = re.compile(
    r"^\s*(?:#{1,6}\s*|【)?\s*(?:方案|选项|Option|PLAN|Plan|option|plan)\s*[-—:]?\s*"
    r"(?:[A-Da-d]|[一二三四五六七八九十]|\d{1,2})\s*(?:】|:|：|\]|)?\s*\S",
    re.MULTILINE,
)
# Prose that proposes alternatives instead of deciding.
_PHRASES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(备选方案|备选|替代方案|另一个方案)"),
    re.compile(r"(另一种(?:方案|思路)|两套方案|两种思路|另一套方案|两个方向)"),
    re.compile(r"Plan\s*B|plan\s*b"),
    re.compile(r"或者可以|你也可以(?:选择|采用)|要么.{0,8}要么"),
    re.compile(r"(?:选项|Option)\s*[A-Da-d]"),
    re.compile(
        r"方案[一二三四五六七八九十1-4]\s*(?:和|与|/|及|或)\s*方案[一二三四五六七八九十1-4]"
    ),
)


def _phrase_violation(masked: str) -> str | None:
    for pattern in _PHRASES:
        match = pattern.search(masked)
        if match:
            return match.group(0)
    return None


# A single mention may be legitimate ("否则按 Plan B 处理" is still a branch);
# two or more *titled* alternatives are the unambiguous violation signal.
_TITLED_THRESHOLD: Final = 2


def mask_protected(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code, inline code and URLs with opaque tokens."""
    tokens: dict[str, str] = {}

    def stash(match: re.Match[str]) -> str:
        token = f"\x00{len(tokens)}\x00"
        tokens[token] = match.group(0)
        return token

    masked = _FENCE.sub(stash, text)
    masked = _INLINE_CODE.sub(stash, masked)
    masked = re.sub(r"https?://\S+", stash, masked)
    return masked, tokens


def _restore(text: str, tokens: dict[str, str]) -> str:
    for token, original in tokens.items():
        text = text.replace(token, original)
    return text


def find_multi_plan_violations(proposal_text: str) -> list[str]:
    """Return human-readable violations; empty list means the plan is single."""
    if not proposal_text.strip():
        return []
    masked, tokens = mask_protected(proposal_text)

    violations: list[str] = []
    titles = [line.strip() for line in _TITLED_ALTERNATIVE.findall(masked) if line.strip()]
    # findall with groups returns the last group; re-scan for full lines.
    titled_lines = [match.group(0).strip() for match in _TITLED_ALTERNATIVE.finditer(masked)]
    del titles
    if len(titled_lines) >= _TITLED_THRESHOLD:
        preview = "；".join(titled_lines[:3])
        violations.append(
            f"出现 {len(titled_lines)} 个并列方案标题（{preview}），必须合并为唯一方案"
        )

    phrase = _phrase_violation(masked)
    if phrase:
        violations.append(
            f"出现备选/分支表述「{phrase}」，不确定性应写入 assumptions 或 open_questions"
        )

    del tokens  # violations are reported on the original wording, not tokens
    return violations


SINGLE_PLAN_REPAIR_HINT = (
    "你的输出违反了「唯一方案」约束。请把所有并列的方案、选项、分支合并为**一套**"
    "可执行方案，并在给出选择理由后只输出这一套；不确定之处写入 assumptions 或 "
    "open_questions，不得以并列方案的形式规避决策。"
)

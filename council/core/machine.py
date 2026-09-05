"""The eight phases of a council session.

Phases are fixed and linear except for the P3→P6 loop. The *state* of a phase
is always taken from a structured enum field of the model output — never
inferred from prose.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["PHASE_ORDER", "Phase", "VerdictStatus"]


class Phase(StrEnum):
    CONTEXT = "P0"
    PROPOSAL = "P1"
    SELECT = "P2"
    REVIEW = "P3"
    DEBATE = "P4"
    VERDICT = "P5"
    REVISION = "P6"
    FINAL = "P7"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.CONTEXT,
    Phase.PROPOSAL,
    Phase.SELECT,
    Phase.REVIEW,
    Phase.DEBATE,
    Phase.VERDICT,
    Phase.REVISION,
    Phase.FINAL,
)


class VerdictStatus(StrEnum):
    """The only three ways a review round may end."""

    PASS = "PASS"
    NEED_REVISION = "NEED_REVISION"
    NEED_USER_DECISION = "NEED_USER_DECISION"

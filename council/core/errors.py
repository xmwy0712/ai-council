"""Error taxonomy shared by every adapter, the engine and the UI.

Every failure in AI Council is classified into one of :class:`ErrorKind`. The
classification alone decides the retry semantics (see :data:`RETRYABLE_KINDS`)
so that no call site has to invent its own policy.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RETRYABLE_KINDS", "ContractError", "CouncilError", "ErrorKind"]


class ErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTENT_REFUSAL = "content_refusal"
    NETWORK = "network"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


#: Kinds that are worth retrying. Authentication failures and content refusals
#: are deterministic: retrying them only burns quota and wall-clock time.
RETRYABLE_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.TIMEOUT, ErrorKind.NETWORK})


class CouncilError(Exception):
    """A classified, node-scoped failure."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        node_id: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.node_id = node_id
        self.retryable = (kind in RETRYABLE_KINDS) if retryable is None else retryable

    def __str__(self) -> str:
        scope = f"[{self.node_id}] " if self.node_id else ""
        return f"{scope}{self.kind.value}: {self.message}"


class ContractError(CouncilError):
    """Model output violated its structured-output contract.

    Contract errors are never retryable as-is: the engine answers them with a
    stricter *repair* prompt. If repair keeps failing the engine escalates to
    ``NEED_USER_DECISION`` — it must never silently degrade to a revision loop.
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str | None = None,
        raw: str = "",
        repair_hint: str = "",
    ) -> None:
        super().__init__(ErrorKind.CONTRACT, message, node_id=node_id, retryable=False)
        self.raw = raw
        self.repair_hint = repair_hint

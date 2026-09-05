"""Identifier generation."""

from __future__ import annotations

import secrets
from datetime import datetime

__all__ = ["new_session_id"]


def new_session_id() -> str:
    """Sortable by creation time, unguessable enough to avoid collisions."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"

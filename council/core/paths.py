"""Application data paths.

The application writes to exactly two places: this data directory (config,
session database, logs) and whatever path the user picks in an explicit export
dialog. Nothing a model says can move that boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["config_path", "data_dir", "sessions_db"]


def data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "ai-council"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "ai-council"


def sessions_db(data: Path | None = None) -> Path:
    return (data or data_dir()) / "sessions.sqlite3"


def config_path(data: Path | None = None) -> Path:
    return (data or data_dir()) / "config.toml"

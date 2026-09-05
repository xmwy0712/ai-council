"""``python -m council`` — also the entry point PyInstaller freezes.

Keeping this thin matters: the frozen binary must be able to reach the same
console script the pip package installs, with no packaging-specific branches.
"""

from __future__ import annotations

from council.cli.main import app

if __name__ == "__main__":
    app()

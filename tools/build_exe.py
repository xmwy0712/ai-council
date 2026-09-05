"""Build a standalone Windows executable for AI Council.

The pip package stays the recommended install path; this is the no-Python
convenience build. Two things make it non-trivial, and both are handled here:

- **Package data**: the web UI (``council/web/static``), the model registry
  TOMLs (``council/registry``) and the prompt templates
  (``council/core/templates``) are not Python modules, so PyInstaller needs
  ``--collect-data council`` or the frozen binary boots without a UI.
- **Lazy imports**: ``council serve`` imports uvicorn/websockets only when the
  command runs. They must be collected explicitly or the exe builds fine and
  fails at ``serve`` time.

Usage::

    python tools/build_exe.py            # one-file exe -> dist/ai-council.exe
    python tools/build_exe.py --zip      # ready-to-run -> dist/ai-council-win64.zip
    python tools/build_exe.py --onedir   # folder build -> dist/ai-council/
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "council" / "__main__.py"

#: Ready-to-run bundle: unzip and double-click, no per-launch unpacking.
ZIP_BASENAME = "ai-council-win64"

#: Data files and lazily-imported modules the frozen build must carry.
BASE_ARGS: list[str] = [
    "--noconfirm",
    "--clean",
    "--name",
    "ai-council",
    "--collect-data",
    "council",
    "--collect-submodules",
    "uvicorn",
    "--hidden-import",
    "websockets",
    "--hidden-import",
    "httptools",
    "--hidden-import",
    "watchfiles",
    # keyring resolves its backend at runtime; without these the frozen build
    # silently loses the Windows credential vault.
    "--hidden-import",
    "keyring.backends.Windows",
    "--hidden-import",
    "keyring.backends.fail",
    "--hidden-import",
    "keyring.backends.null",
]

#: Dev tooling must never ship inside the binary.
EXCLUDE_ARGS: list[str] = [
    "--exclude-module",
    "pytest",
    "--exclude-module",
    "mypy",
    "--exclude-module",
    "ruff",
]


def build(*, onefile: bool, zip_bundle: bool = False) -> int:
    import shutil

    from PyInstaller.__main__ import run  # imported lazily: build-time only

    # Scratch space lives in the OS temp dir so the repo stays clean. The build
    # also lands there first: PyInstaller deletes an existing output before
    # writing, and overwriting by copy is friendlier to locked/protected
    # directories than delete-then-write.
    workdir = Path(tempfile.mkdtemp(prefix="ai-council-build-"))
    staged = workdir / "dist"
    args = [
        str(ENTRY),
        # A zip bundle is the folder build packed up: it starts instantly
        # because nothing has to be unpacked from the exe on every launch.
        "--onefile" if onefile and not zip_bundle else "--onedir",
        *BASE_ARGS,
        *EXCLUDE_ARGS,
        "--distpath",
        str(staged),
        "--workpath",
        str(workdir),
        "--specpath",
        str(workdir),
    ]
    run(args)

    if zip_bundle:
        # Keep one top-level folder inside the zip, so "extract here" does not
        # scatter ~40 files into the user's Downloads directory.
        shutil.make_archive(
            str(staged / ZIP_BASENAME), "zip", root_dir=staged, base_dir="ai-council"
        )

    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for item in staged.iterdir():
        if zip_bundle and item.is_dir():
            continue  # the zip replaces the folder; do not ship both
        target = dist / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
        print(f"built: {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze AI Council into an .exe")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--onedir",
        action="store_true",
        help="build a folder instead of a single file (starts faster)",
    )
    group.add_argument(
        "--zip",
        action="store_true",
        help="build the folder and pack it into a zip (ready-to-run, no unpack)",
    )
    options = parser.parse_args()
    try:
        return build(
            onefile=not (options.onedir or options.zip),
            zip_bundle=options.zip,
        )
    except ImportError:
        print("需要 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the per-user Windows installer around the PyInstaller folder build.

Why an installer exists at all: the one-file exe unpacks itself into ``%TEMP%``
on every launch (1–3 s). The installer pays that cost once, at install time, and
ships start-menu entries — the closest a Python app gets to a native feel.

Requires Inno Setup 6 (``ISCC.exe``). If it is missing we stop with a readable
message instead of half-producing an installer; CI installs it on
windows-latest via ``choco install innosetup``.

Usage::

    python tools/build_installer.py            # ensure payload + compile
    python tools/build_installer.py --no-build # compile only (payload exists)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))  # sibling import: build_exe

from build_exe import ZIP_BASENAME, build  # noqa: E402  (path juggling above)

ISCC_CANDIDATES: tuple[Path, ...] = (
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Users\Lenovo\AppData\Local\Programs\Inno Setup 6\ISCC.exe"),
)


def find_iscc() -> Path | None:
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    return next((path for path in ISCC_CANDIDATES if path.is_file()), None)


def pack_portable_zip(payload: Path) -> Path:
    """Bundle the folder build as a ready-to-run zip (one top-level folder)."""
    archive = shutil.make_archive(
        str(ROOT / "dist" / ZIP_BASENAME),
        "zip",
        root_dir=payload.parent,
        base_dir=payload.name,
    )
    print(f"built: {archive}")
    return Path(archive)


def main() -> int:
    only_compile = "--no-build" in sys.argv

    if not only_compile:
        print(">> building payload (onedir)…")
        build(onefile=False)

    payload = ROOT / "dist" / "ai-council"
    if not (payload / "ai-council.exe").is_file():
        print(f"缺少 PyInstaller 目录产物：{payload}", file=sys.stderr)
        return 2
    if not only_compile:
        pack_portable_zip(payload)

    iscc = find_iscc()
    if iscc is None:
        print(
            "未找到 Inno Setup 6 (ISCC.exe)。\n"
            "  Windows: choco install innosetup  （或 winget install JRSoftware.InnoSetup）\n"
            "  也可从 https://jrsoftware.org/isdl.php 下载安装后重试。\n"
            "仓库里 tools/installer.iss 已就绪，GitHub Actions 会自动构建安装器。",
            file=sys.stderr,
        )
        return 2

    import council

    script = TOOLS / "installer.iss"
    command = [str(iscc), f"/DAppVersion={council.__version__}", str(script)]
    print(">>", " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print("ISCC 编译失败", file=sys.stderr)
        return result.returncode

    setup = ROOT / "dist" / f"AI-Council-Setup-{council.__version__}.exe"
    print(f"built: {setup}" if setup.is_file() else "installer built (path unknown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

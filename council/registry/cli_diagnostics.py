"""三大本地 CLI 应用的环境检查——给网页端 settings 面板用。

对每家 CLI 探测：
  - 是否安装（shutil.which）；
  - 版本（`<cli> --version`）；
  - 鉴权状态（`agy auth status` 等；无对应子命令的留 None）。
全程带短超时，绝不阻塞前端轮询。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass

_PROBE_TIMEOUT_S = 5.0


@dataclass
class CliDiagnostic:
    """单家 CLI 的环境检查结果。"""

    name: str
    executable: str
    installed: bool
    version: str | None = None
    authenticated: bool | None = None  # None = 未知（无对应检测命令）
    auth_detail: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "executable": self.executable,
            "installed": self.installed,
            "version": self.version,
            "authenticated": self.authenticated,
            "auth_detail": self.auth_detail,
            "error": self.error,
        }


async def _run(executable: str, *args: str) -> tuple[int | None, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "", ""
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
    except (TimeoutError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None, "", "probe timeout"
    return (
        proc.returncode,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _which(executable: str) -> str | None:
    return shutil.which(executable)


async def check_codex() -> CliDiagnostic:
    d = CliDiagnostic(name="codex", executable="codex", installed=False)
    if not _which(d.executable):
        return d
    d.installed = True
    code, out, err = await _run(d.executable, "--version")
    if code == 0:
        d.version = (out or err).strip().splitlines()[0][:80] if (out or err).strip() else "ok"
    else:
        d.error = (err or out).strip()[:160]
    d.authenticated = None
    return d


async def check_claude() -> CliDiagnostic:
    d = CliDiagnostic(name="claude", executable="claude", installed=False)
    if not _which(d.executable):
        return d
    d.installed = True
    code, out, err = await _run(d.executable, "--version")
    if code == 0:
        d.version = (out or err).strip().splitlines()[0][:80] if (out or err).strip() else "ok"
    else:
        d.error = (err or out).strip()[:160]
    d.authenticated = None
    return d


async def check_agy() -> CliDiagnostic:
    d = CliDiagnostic(name="agy", executable="agy", installed=False)
    if not _which(d.executable):
        return d
    d.installed = True
    code, out, err = await _run(d.executable, "--version")
    if code == 0:
        d.version = (out or err).strip().splitlines()[0][:80] if (out or err).strip() else "ok"
    else:
        d.error = (err or out).strip()[:160]
        return d
    # Antigravity 没有 auth/login 子命令：鉴权走浏览器 OAuth 缓存，
    # `agy --version` 能跑就说明已安装。auth 状态不做探测。
    d.authenticated = None
    return d


async def check_all() -> list[dict[str, str | bool | None]]:
    return [d.to_dict() for d in await asyncio.gather(check_codex(), check_claude(), check_agy())]

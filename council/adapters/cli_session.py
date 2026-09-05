"""Wrap locally-authenticated CLIs (Codex / Claude Code / Antigravity) as nodes.

Safety invariants, enforced here and nowhere else (never in user-editable data):

* arguments are always passed as an array to ``create_subprocess_exec`` —
  ``shell=True`` does not exist in this codebase;
* every profile runs with its explicit read-only / tools-disabled /
  non-interactive flags (verified flags live in docs/PROVIDERS.md);
* the child runs inside an empty, read-only temporary directory;
* on timeout the whole process tree is terminated, not just the parent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

from ..core.config import NodeSection
from ..core.contracts import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChunkType,
    HealthStatus,
    Usage,
)
from ..core.errors import CouncilError, ErrorKind

__all__ = ["CLI_PROFILES", "CliProfile", "CliSessionAdapter"]

ResultKind = Literal["stdout", "json_result", "json_response"]


@dataclass(frozen=True)
class CliProfile:
    """One local CLI's invocation contract.

    ``argv(prompt, model, cwd)`` MUST include the profile's non-interactive and
    read-only flags. If a flag is ever missing here, that is a security bug —
    do not work around it in callers.
    """

    name: str
    executable: str
    result_kind: ResultKind
    usage_kind: Literal["none", "json"] = "json"

    def argv(self, prompt: str, model: str | None, cwd: str) -> list[str]:
        """Arguments *after* the executable (the adapter prepends it)."""
        raise NotImplementedError


class _CodexProfile(CliProfile):
    def argv(self, prompt: str, model: str | None, cwd: str) -> list[str]:
        args = [
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            cwd,
        ]
        if model:
            args += ["-m", model]
        args.append(prompt)
        return args


class _ClaudeProfile(CliProfile):
    def argv(self, prompt: str, model: str | None, cwd: str) -> list[str]:
        args = [
            "-p",
            prompt,
            "--output-format",
            "json",
            "--disallowedTools",
            "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch",
        ]
        if model:
            args += ["--model", model]
        return args


class _AgyProfile(CliProfile):
    def argv(self, prompt: str, model: str | None, cwd: str) -> list[str]:
        args = ["-p", prompt, "--output-format", "json", "--mode", "plan"]
        if model:
            args += ["--model", model]
        return args


CLI_PROFILES: dict[str, CliProfile] = {
    "codex": _CodexProfile(
        name="codex", executable="codex", result_kind="stdout", usage_kind="none"
    ),
    "claude": _ClaudeProfile(
        name="claude", executable="claude", result_kind="json_result", usage_kind="json"
    ),
    "agy": _AgyProfile(
        name="agy", executable="agy", result_kind="json_response", usage_kind="json"
    ),
}


class CliSessionAdapter:
    def __init__(
        self,
        node: NodeSection,
        *,
        profile: CliProfile | None = None,
        work_dir: str | None = None,
    ) -> None:
        self.id = node.id
        self._node = node
        cli_name = str(node.settings.get("cli") or "")
        if profile is not None:
            self._profile = profile
        elif cli_name in CLI_PROFILES:
            self._profile = CLI_PROFILES[cli_name]
        else:
            known = "、".join(sorted(CLI_PROFILES))
            raise CouncilError(
                ErrorKind.CONTRACT,
                f"cli_session 需要 nodes.settings.cli ∈（{known}），收到 {cli_name!r}",
                node_id=node.id,
            )
        self._model = str(node.model)
        if self._model.startswith(f"{self._profile.name}:"):
            self._model = self._model.split(":", 1)[1] or None  # type: ignore[assignment]
        self._work_dir = work_dir
        self.capabilities = Capabilities(
            streaming=False,
            structured_output=self._profile.result_kind != "stdout",
            max_context=0,
        )

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            code, stdout, stderr = await self._run(
                [self._profile.executable, "--version"], timeout_s=10, cwd=None
            )
        except CouncilError as err:
            return HealthStatus(
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail=err.message,
                error_kind=err.kind.value,
            )
        latency = int((time.monotonic() - started) * 1000)
        if code == 0:
            return HealthStatus(ok=True, latency_ms=latency, detail=stdout.strip()[:80])
        if not shutil.which(self._profile.executable):
            return HealthStatus(
                ok=False,
                latency_ms=latency,
                detail=f"找不到命令 {self._profile.executable}（未安装或不在 PATH）",
                error_kind="auth",
            )
        return HealthStatus(
            ok=False,
            latency_ms=latency,
            detail=(stderr or stdout).strip()[:160],
            error_kind="unknown",
        )

    def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        return self._chat(req)

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------ internals

    async def _chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        prompt = "\n\n".join(
            f"[{message.role.value}]\n{message.content}" for message in req.messages
        )
        cwd = self._cwd()
        argv = [
            self._profile.executable,
            *self._profile.argv(prompt, self._model or None, cwd),
        ]
        code, stdout, stderr = await self._run(argv, timeout_s=req.timeout.total_s, cwd=cwd)
        if code != 0:
            raise _classify_exit(self.id, code, stderr or stdout)
        text, usage = _parse_output(self._profile, stdout)
        if text:
            yield ChatChunk(type=ChunkType.DELTA, text=text)
        yield ChatChunk(type=ChunkType.DONE, usage=usage)

    def _cwd(self) -> str:
        if self._work_dir is not None:
            return self._work_dir
        path = tempfile.mkdtemp(prefix="council-cli-")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o555)  # best-effort read-only on POSIX and Windows
        return path

    async def _run(
        self, argv: Sequence[str], timeout_s: float, cwd: str | None
    ) -> tuple[int, str, str]:
        """Run without a shell, kill the whole tree on timeout."""
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd if cwd is not None else self._cwd(),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,  # type: ignore[arg-type]
            )
        except FileNotFoundError as err:
            raise CouncilError(
                ErrorKind.AUTH,
                f"找不到命令 {argv[0]}（未安装或不在 PATH）",
                node_id=self.id,
            ) from err
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError as err:
            await _kill_tree(process)
            raise CouncilError(
                ErrorKind.TIMEOUT,
                f"CLI 调用超过 {timeout_s:g}s，已终止进程树",
                node_id=self.id,
            ) from err
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        return process.returncode or 0, stdout, stderr


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError, TimeoutError):
        with contextlib.suppress(OSError):
            process.kill()


def _classify_exit(node_id: str, code: int, text: str) -> CouncilError:
    lowered = text.lower()
    if any(
        token in lowered
        for token in ("not logged in", "authentication required", "unauthorized", "401", "login")
    ):
        return CouncilError(ErrorKind.AUTH, f"CLI 未登录或认证失效：{text[:160]}", node_id=node_id)
    if "429" in lowered or "rate limit" in lowered:
        return CouncilError(ErrorKind.RATE_LIMIT, f"CLI 限流：{text[:160]}", node_id=node_id)
    return CouncilError(ErrorKind.UNKNOWN, f"CLI 退出码 {code}：{text[:160]}", node_id=node_id)


def _parse_output(profile: CliProfile, stdout: str) -> tuple[str, Usage]:
    text = stdout.strip()
    if profile.result_kind == "stdout" or not text:
        return text, Usage()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, Usage()
    if not isinstance(data, dict):
        return text, Usage()
    if profile.result_kind == "json_result":
        result = str(data.get("result") or "")
        usage_raw = data.get("usage")
        usage = _usage_from_json(usage_raw)
        cost = data.get("cost_usd") or data.get("total_cost_usd") or data.get("cost")
        if isinstance(cost, (int, float)):
            usage = usage.model_copy(update={"cost_usd": float(cost)})
        return result, usage
    # json_response (agy)
    result = str(data.get("response") or "")
    return result, _usage_from_json(data.get("usage"))


def _usage_from_json(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    input_tokens = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or (input_tokens + output_tokens))
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total)

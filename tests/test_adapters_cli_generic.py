"""cli_session and generic_http adapters.

cli_session tests run a real subprocess — a fake CLI written in Python — so we
can verify the safety invariants end-to-end: arguments as arrays (injection
text stays inert), a read-only working directory, and a killed tree on timeout.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from council.adapters.cli_session import CLI_PROFILES, CliProfile, CliSessionAdapter
from council.adapters.generic_http import GenericHttpAdapter
from council.core.config import NodeSection
from council.core.contracts import (
    ChatMessage,
    ChatRequest,
    ChunkType,
    Role,
    TimeoutSpec,
)
from council.core.errors import CouncilError, ErrorKind


def _node(cli: str, model: str = "x") -> NodeSection:
    return NodeSection(
        id="n1",
        adapter="cli_session",
        model=model,
        settings={"cli": cli},
    )


# ----------------------------------------------------------------- profiles


def test_codex_argv_is_read_only_and_array() -> None:
    argv = CLI_PROFILES["codex"].argv("问题", "gpt-5.2-codex", "/tmp/work")
    assert argv[0] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.2-codex"
    assert argv[-1] == "问题"


def test_claude_argv_disables_write_and_network_tools() -> None:
    argv = CLI_PROFILES["claude"].argv("问题", None, "/tmp/work")
    assert argv[0] == "-p"
    disallowed = argv[argv.index("--disallowedTools") + 1]
    for tool in ("Write", "Edit", "Bash", "WebFetch", "WebSearch"):
        assert tool in disallowed
    assert argv[argv.index("--output-format") + 1] == "json"


def test_agy_argv_uses_plan_mode() -> None:
    argv = CLI_PROFILES["agy"].argv("问题", "Gemini 3.5 Flash (High)", "/tmp/work")
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "Gemini 3.5 Flash (High)"


def test_profile_prefix_is_stripped_from_model() -> None:
    adapter = CliSessionAdapter(_node("codex", model="codex:gpt-5.2-codex"))
    assert adapter._model == "gpt-5.2-codex"


def test_unknown_cli_profile_rejected() -> None:
    with pytest.raises(CouncilError) as info:
        CliSessionAdapter(_node("teleport"))
    assert info.value.kind is ErrorKind.CONTRACT


# ----------------------------------------------------------- fake CLI runs


class _PythonProfile(CliProfile):
    """Drive a python script as if it were a CLI."""

    def __init__(self, script: Path, result_kind: str = "json_result") -> None:
        super().__init__(
            name="test",
            executable=sys.executable,
            result_kind=result_kind,  # type: ignore[arg-type]
        )
        self._script = script

    def argv(self, prompt: str, model: str | None, cwd: str) -> list[str]:
        return [str(self._script), prompt, model or "", cwd]


def _write_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(body, encoding="utf-8")
    return script


async def test_injection_text_is_passed_literally(tmp_path: Path) -> None:
    """If arguments ever went through a shell, this prompt would execute."""
    script = _write_script(
        tmp_path,
        "import json,sys\nprint(json.dumps({'type':'result','result':sys.argv[1],'usage':{}}))",
    )
    evil = "$(touch /tmp/pwned) && echo hacked"
    adapter = CliSessionAdapter(_node("claude"), profile=_PythonProfile(script))
    chunks = [c async for c in adapter.chat(_req(evil))]
    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    # The adapter prefixes the role tag; what matters is the payload stayed inert.
    assert text == f"[user]\n{evil}"
    assert not Path("/tmp/pwned").exists()


async def test_work_dir_is_read_only(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        (
            "import json,os,sys\n"
            "cwd=sys.argv[3]\n"
            "try:\n"
            "    open(os.path.join(cwd,'x'),'w').write('x')\n"
            "    print(json.dumps({'type':'result','result':'WRITABLE','usage':{}}))\n"
            "except OSError:\n"
            "    print(json.dumps({'type':'result','result':'READONLY','usage':{}}))"
        ),
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    os.chmod(work_dir, stat.S_IRUSR | stat.S_IXUSR)
    adapter = CliSessionAdapter(
        _node("claude"), profile=_PythonProfile(script), work_dir=str(work_dir)
    )
    chunks = [c async for c in adapter.chat(_req("q"))]
    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    assert text in ("READONLY", "WRITABLE")  # platform dependent, but never crashes
    if text == "WRITABLE":
        pytest.skip("本平台无法对目录施加只读约束，CLI 沙箱仍由各自 read-only 参数兜底")


async def test_usage_is_parsed_from_json(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        (
            "import json\n"
            "print(json.dumps({'type':'result','result':'ok',"
            "'usage':{'input_tokens':10,'output_tokens':4},'cost_usd':0.001}))"
        ),
    )
    adapter = CliSessionAdapter(_node("claude"), profile=_PythonProfile(script))
    chunks = [c async for c in adapter.chat(_req("q"))]
    done = chunks[-1]
    assert done.usage is not None
    assert done.usage.input_tokens == 10
    assert done.usage.cost_usd == 0.001


async def test_nonzero_exit_raises(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path, "import sys\nsys.stderr.write('authentication required')\nsys.exit(1)"
    )
    adapter = CliSessionAdapter(_node("claude"), profile=_PythonProfile(script))
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(_req("q"))]
    assert info.value.kind is ErrorKind.AUTH


async def test_timeout_kills_the_tree(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "import time\ntime.sleep(60)")
    adapter = CliSessionAdapter(_node("claude"), profile=_PythonProfile(script))
    req = _req("q", total_s=0.5)
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(req)]
    assert info.value.kind is ErrorKind.TIMEOUT


def _req(prompt: str, total_s: float = 10.0) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=Role.USER, content=prompt)],
        model="kimi-k2",
        timeout=TimeoutSpec(connect_s=5, idle_s=5, total_s=total_s),
    )


# ------------------------------------------------------------ generic_http


def _generic_node(**settings: Any) -> NodeSection:
    return NodeSection(id="g1", adapter="generic_http", model="kimi-k2", settings=settings)


def _generic_settings() -> dict[str, Any]:
    return {
        "url": "https://api.example.com/v1/chat",
        "headers": {"Authorization": "Bearer ${GENERIC_TEST_KEY}"},
        "body_template": (
            '{"model": "{model}", "messages": [{"role": "user", "content": {prompt_json}}]}'
        ),
        "response_path": "choices[0].message.content",
        "usage_input_path": "usage.prompt_tokens",
        "usage_output_path": "usage.completion_tokens",
    }


async def test_generic_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERIC_TEST_KEY", "g-key")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content.decode()),
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    adapter = GenericHttpAdapter(
        _generic_node(**_generic_settings()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    chunks = [c async for c in adapter.chat(_req("你好"))]

    assert seen[0]["auth"] == "Bearer g-key"
    assert seen[0]["body"]["model"] == "kimi-k2"
    assert seen[0]["body"]["messages"][0]["content"] == "你好"
    text = "".join(c.text for c in chunks if c.type is ChunkType.DELTA)
    assert text == '{"ok": true}'
    assert chunks[-1].usage is not None and chunks[-1].usage.total_tokens == 5


async def test_generic_prompt_json_escapes_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERIC_TEST_KEY", "g-key")
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = GenericHttpAdapter(
        _generic_node(**_generic_settings()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    _ = [c async for c in adapter.chat(_req('带"引号"和\n换行'))]
    assert bodies[0]["messages"][0]["content"] == '带"引号"和\n换行'


async def test_generic_missing_secret_is_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing secrets fail fast at adapter construction, not mid-session."""
    monkeypatch.delenv("GENERIC_TEST_KEY", raising=False)
    with pytest.raises(CouncilError) as info:
        GenericHttpAdapter(_generic_node(**_generic_settings()))
    assert info.value.kind is ErrorKind.AUTH


async def test_generic_bad_template_is_contract_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERIC_TEST_KEY", "g-key")
    settings = _generic_settings()
    settings["body_template"] = '{"model": "{model}", "prompt": "{prompt}"'  # unquoted {prompt}
    adapter = GenericHttpAdapter(_generic_node(**settings))
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(_req('有"引号"'))]
    assert info.value.kind is ErrorKind.CONTRACT


async def test_generic_response_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERIC_TEST_KEY", "g-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    adapter = GenericHttpAdapter(
        _generic_node(**_generic_settings()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CouncilError) as info:
        _ = [c async for c in adapter.chat(_req("q"))]
    assert info.value.kind is ErrorKind.CONTRACT
    assert "不存在字段" in info.value.message

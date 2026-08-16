"""Codex CLI adapter for one non-interactive SSSF agent turn.

The Python ADW remains the orchestrator. Codex is a bounded worker invoked
through ``codex exec --json`` with a JSON Schema, a role-specific permission
profile, and a persistent thread id. Continuing a role is therefore the same
operation as starting it, except that the command uses ``exec resume``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import CodexRequest, CodexResult
from .utils import now_iso, operator_env

CODEX_PATH = os.environ.get("CODEX_PATH", "codex")
RESULT_SNIPPET_CHARS = 20_000
ARG_VALUE_CHARS = 20_000
LABEL_CHARS = 100


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _toml_string(value: str) -> str:
    """JSON strings are valid TOML basic strings and safe as one argv item."""
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    """Encode the small JSON-like subset used by inline permission tables."""
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_string(str(key))} = {_toml_value(child)}"
            for key, child in value.items()
        ) + " }"
    raise TypeError(f"unsupported TOML override value: {type(value).__name__}")


@lru_cache(maxsize=1)
def environment_problem() -> str:
    """Return a preflight problem, or an empty string when Codex is usable."""
    binary = shutil.which(CODEX_PATH)
    if not binary:
        return f"Codex CLI not found on PATH (looked for {CODEX_PATH!r})"
    try:
        status = subprocess.run(
            [binary, "login", "status"], capture_output=True, text=True,
            timeout=20, env=operator_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"could not check Codex login status: {error}"
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        return "Codex is not logged in; run `codex login`" + (f" ({detail})" if detail else "")
    return ""


def _item_args(item: dict) -> dict:
    kind = str(item.get("type") or "tool")
    if kind == "command_execution":
        return {"command": item.get("command", "")}
    if kind == "file_change":
        return {"changes": item.get("changes") or item.get("files") or []}
    if kind == "mcp_tool_call":
        return {
            "server": item.get("server") or item.get("server_name") or "",
            "tool": item.get("tool") or item.get("tool_name") or "",
            "arguments": item.get("arguments") or {},
        }
    if kind == "web_search":
        return {"query": item.get("query") or ""}
    return {key: value for key, value in item.items()
            if key not in {"id", "type", "status"}}


def _item_label(item: dict, args: dict) -> str:
    kind = str(item.get("type") or "tool")
    if kind == "command_execution":
        detail = str(args.get("command") or "")
        tool = "bash"
    elif kind == "file_change":
        changes = args.get("changes") or []
        detail = ", ".join(
            str(change.get("path") or change.get("file") or change)
            if isinstance(change, dict) else str(change)
            for change in changes[:4]
        )
        tool = "file change"
    elif kind == "mcp_tool_call":
        tool = "mcp"
        detail = "/".join(filter(None, [str(args.get("server") or ""),
                                          str(args.get("tool") or "")]))
    elif kind == "web_search":
        tool, detail = "web search", str(args.get("query") or "")
    else:
        tool, detail = kind.replace("_", " "), ""
    detail = " ".join(detail.split())
    return f"{tool}: {_clip(detail, LABEL_CHARS)}" if detail else tool


class ToolCallTracker:
    """Fold Codex item start/completion events into one SSSF tool span."""

    TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        event_type = str(event.get("type") or "")
        item = event.get("item") or {}
        if not isinstance(item, dict) or item.get("type") not in self.TOOL_ITEMS:
            return None
        item_id = str(item.get("id") or "")
        if event_type == "item.started":
            self._open[item_id] = {"started_at": now_iso(), "clock": time.monotonic()}
            return None
        if event_type != "item.completed":
            return None

        opened = self._open.pop(item_id, {})
        args = _item_args(item)
        status = str(item.get("status") or "completed")
        exit_code = item.get("exit_code")
        ok = status not in {"failed", "error"} and (exit_code in (None, 0))
        record = {
            "tool": str(item.get("type")),
            "tool_call_id": item_id,
            "args": {
                key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                for key, value in args.items()
            },
            "ok": ok,
            "label": _item_label(item, args),
            "started_at": opened.get("started_at") or now_iso(),
            "ended_at": now_iso(),
        }
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        result = (item.get("aggregated_output") or item.get("output") or
                  item.get("result") or item.get("error"))
        if result:
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            record["result_snippet"] = _clip(text, RESULT_SNIPPET_CHARS)
        if exit_code is not None:
            record["exit_code"] = exit_code
        return record


class HookSpoolTailer:
    """Forward trusted Codex hook records while native subagents are running."""

    def __init__(self, path: str, callback: Optional[Callable[[dict], None]]):
        self.path = Path(path) if path else None
        self.callback = callback
        self.offset = self.path.stat().st_size if self.path and self.path.exists() else 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.path or not self.callback:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self._drain()

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self._drain()

    def _drain(self) -> None:
        if not self.path or not self.path.exists() or not self.callback:
            return
        with self.path.open() as handle:
            handle.seek(self.offset)
            for line in handle:
                try:
                    self.callback({"type": "sssf.hook", "hook": json.loads(line)})
                except json.JSONDecodeError:
                    continue
            self.offset = handle.tell()


def _command(request: CodexRequest) -> list[str]:
    common = [
        "--json",
        "--output-schema", request.output_schema_path,
        "--model", request.model,
        "--strict-config",
        "--ignore-user-config",
        "-c", f"developer_instructions={_toml_string(request.developer_instructions)}",
        "-c", f"model_reasoning_effort={_toml_string(request.thinking)}",
        "-c", 'approval_policy="never"',
        "-c", (f"permissions.{request.permission_profile}.filesystem="
               f"{_toml_value(request.permission_filesystem)}"),
        "-c", f"default_permissions={_toml_string(request.permission_profile)}",
        "-c", f"agents.enabled={'true' if request.subagents else 'false'}",
        "--enable" if request.subagents else "--disable", "multi_agent",
    ]
    if request.thread_id:
        return [CODEX_PATH, "exec", "resume", *common, request.thread_id, request.prompt]
    return [CODEX_PATH, "exec", *common, "--cd", request.cwd, request.prompt]


def run(request: CodexRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> CodexResult:
    """Run or resume one Codex thread and stream its JSONL events."""
    problem = environment_problem()
    if problem:
        raise RuntimeError(problem)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = raw_path.with_name("codex_stderr.log")
    env = operator_env()
    if request.hook_spool_path:
        env["SSSF_HOOK_SPOOL"] = request.hook_spool_path

    command = _command(request)
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, cwd=request.cwd, env=env,
    )
    if on_spawn:
        on_spawn(process.pid)

    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        for chunk in process.stderr:
            stderr_chunks.append(chunk)
            with stderr_path.open("a") as handle:
                handle.write(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    hook_tailer = HookSpoolTailer(request.hook_spool_path, on_event)
    hook_tailer.start()

    result = CodexResult(thread_id=request.thread_id)
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                result.thread_id = str(event.get("thread_id") or result.thread_id)
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    result.text = str(item["text"])
            elif event_type == "turn.completed":
                usage = event.get("usage") or {}
                turn_total = result.usage.add_codex_turn(usage)
                result.tokens += turn_total
                result.context_tokens = turn_total
            if on_event:
                on_event(event)

    result.returncode = process.wait()
    stderr_thread.join(timeout=2)
    hook_tailer.stop()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0:
        detail = "".join(stderr_chunks).strip()[-2000:]
        raise RuntimeError(f"codex exited {result.returncode}: {detail or 'no diagnostic output'}")
    if not result.thread_id:
        raise RuntimeError("codex completed without emitting thread.started")
    if not result.text:
        raise RuntimeError("codex completed without a final agent_message")
    return result

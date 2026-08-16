from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULES = Path(__file__).parents[1] / "templates" / "adws"
sys.path.insert(0, str(MODULES))

from adw_modules import agent_codex, agents, permissions  # noqa: E402
from adw_modules.data_types import (CodexRequest, PlanOutput, SSSFConfig,
                                    UsageBreakdown)  # noqa: E402


def request(**overrides) -> CodexRequest:
    values = {
        "prompt": "inspect",
        "developer_instructions": "stay bounded",
        "model": "gpt-5.6-terra",
        "thinking": "medium",
        "raw_output_path": "/tmp/raw.jsonl",
        "output_schema_path": "/tmp/schema.json",
        "permission_profile": "sssf-scout",
        "cwd": "/repo",
    }
    values.update(overrides)
    return CodexRequest(**values)


def test_new_and_resumed_commands_keep_structured_contract() -> None:
    new = agent_codex._command(request())
    assert new[:2] == [agent_codex.CODEX_PATH, "exec"]
    assert "--output-schema" in new
    assert ["--cd", "/repo"] == new[-3:-1]
    assert 'default_permissions="sssf-scout"' in new

    resumed = agent_codex._command(request(thread_id="thr_123"))
    assert resumed[:3] == [agent_codex.CODEX_PATH, "exec", "resume"]
    assert "thr_123" in resumed
    assert "--cd" not in resumed


def test_codex_usage_maps_cached_input_without_fake_cost() -> None:
    usage = UsageBreakdown()
    total = usage.add_codex_turn({
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 25,
        "reasoning_output_tokens": 5,
    })
    assert total == 125
    assert usage.input_tokens == 40
    assert usage.cache_read_tokens == 60
    assert usage.output_tokens == 25
    assert usage.reasoning_tokens == 5
    assert usage.total_cost is None


def test_codex_tool_events_fold_into_one_span() -> None:
    tracker = agent_codex.ToolCallTracker()
    assert tracker.observe({
        "type": "item.started",
        "item": {"id": "item_1", "type": "command_execution", "command": "git status"},
    }) is None
    record = tracker.observe({
        "type": "item.completed",
        "item": {
            "id": "item_1", "type": "command_execution", "command": "git status",
            "status": "completed", "exit_code": 0, "aggregated_output": "clean",
        },
    })
    assert record is not None
    assert record["label"] == "bash: git status"
    assert record["ok"] is True
    assert record["result_snippet"] == "clean"
    assert record["started_at"] and record["ended_at"]


def test_output_schema_is_closed_and_requires_every_field() -> None:
    schema = agents._strict_schema(PlanOutput)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_native_permissions_expand_read_globs_and_defer_write_globs(tmp_path: Path) -> None:
    (tmp_path / "adws").mkdir()
    (tmp_path / "adws" / "adw_plan.py").write_text("# protected")
    (tmp_path / "README.md").write_text("# docs")
    cfg = SSSFConfig.model_validate({
        "defaults": {
            "data_dir": "adws/adw_data",
            "protected_files": ["adws/adw_*.py", ".codex/"],
        }
    })
    run = SimpleNamespace(repo_root=tmp_path, cfg=cfg)
    agent = SimpleNamespace(writes=["docs/", "**/*.md"])

    filesystem = agents._permission_filesystem(run, agent)
    rules = filesystem[":workspace_roots"]
    assert rules["."] == "write"  # post-run verifier narrows extension globs
    assert rules[".git/**"] == "read"
    assert rules["adws/adw_plan.py"] == "read"
    assert rules["README.md"] == "write"
    assert all("adw_*.py" not in path for path in rules)
    assert all(path != "**/*.md" for path in rules)


def test_permission_breach_restores_same_size_dirty_content(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "sssf@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "SSSF Test"], cwd=tmp_path, check=True)
    target = tmp_path / "owned.txt"
    target.write_text("alpha")
    subprocess.run(["git", "add", "owned.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    target.write_text("bravo")
    run = SimpleNamespace(repo_root=tmp_path, cfg=SSSFConfig())
    before = permissions.snapshot(run)
    target.write_text("charm")  # same byte length as the operator's dirty content

    agent = SimpleNamespace(name="scout", writes=[])
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert target.read_text() == "bravo"


def test_permission_breach_preserves_operator_deletion(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "sssf@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "SSSF Test"], cwd=tmp_path, check=True)
    target = tmp_path / "removed.txt"
    target.write_text("tracked")
    subprocess.run(["git", "add", "removed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    target.unlink()
    run = SimpleNamespace(repo_root=tmp_path, cfg=SSSFConfig())
    before = permissions.snapshot(run)
    target.write_text("agent recreation")

    agent = SimpleNamespace(name="scout", writes=[])
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert not target.exists()


def test_permission_breach_restores_dirty_symlink_without_touching_target(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "sssf@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "SSSF Test"], cwd=tmp_path, check=True)
    first, second, third = (tmp_path / name for name in ("first", "second", "third"))
    for target in (first, second, third):
        target.write_text(target.name)
    link = tmp_path / "current"
    link.symlink_to(first.name)
    subprocess.run(["git", "add", "current"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    link.unlink()
    link.symlink_to(second.name)
    run = SimpleNamespace(repo_root=tmp_path, cfg=SSSFConfig())
    before = permissions.snapshot(run)
    link.unlink()
    link.symlink_to(third.name)

    agent = SimpleNamespace(name="scout", writes=[])
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert link.readlink() == Path(second.name)
    assert second.read_text() == "second"
    assert third.read_text() == "third"

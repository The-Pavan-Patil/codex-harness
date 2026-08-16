"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


@dataclass(frozen=True)
class SnapshotEntry:
    fingerprint: str
    content: bytes | None
    mode: int | None
    untracked: bool
    symlink_target: str | None = None


TreeSnapshot = dict[str, SnapshotEntry]


def _paths(args: list[str], cwd) -> set[str]:
    result = subprocess.run(["git", *args, "-z"], cwd=cwd,
                            capture_output=True, check=False)
    if result.returncode != 0:
        return set()
    return {part.decode("utf-8", "surrogateescape")
            for part in result.stdout.split(b"\0") if part}


def _entry(root: Path, path: str, untracked: bool) -> SnapshotEntry:
    absolute = root / path
    if absolute.is_symlink():
        target = os.readlink(absolute)
        state = "untracked" if untracked else "tracked"
        return SnapshotEntry(f"{state}:symlink:{target}", None,
                             absolute.lstat().st_mode, untracked, target)
    try:
        content = absolute.read_bytes()
        mode = absolute.stat().st_mode
        digest = hashlib.sha256(content).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        content, mode, digest = None, None, "missing"
    except OSError as error:
        content, mode, digest = None, None, f"unreadable-{error.errno}"
    state = "untracked" if untracked else "tracked"
    return SnapshotEntry(f"{state}:{digest}:{mode}", content, mode, untracked)


def snapshot(run) -> TreeSnapshot:
    """Content-hash every dirty path and retain enough bytes to restore it.

    ``git diff --numstat`` misses a rewrite with the same insertion/deletion
    counts. Hashes do not. Keeping the operator's pre-agent bytes also lets a
    breach restore already-dirty or untracked work instead of merely reporting
    that the agent destroyed something the harness could no longer recover.
    """
    root = Path(run.repo_root)
    tracked = _paths(["diff", "HEAD", "--no-renames", "--name-only"], root)
    untracked = _paths(["ls-files", "--others", "--exclude-standard"], root)
    return {path: _entry(root, path, path in untracked)
            for path in tracked | untracked}


def changed_paths(before: TreeSnapshot, after: TreeSnapshot) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if (before.get(p).fingerprint if before.get(p) else None)
                   != (after.get(p).fingerprint if after.get(p) else None)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.
    """
    return [cfg.defaults.data_dir.rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in always_writable(cfg)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _roll_back(run, path: str, before: TreeSnapshot, after: TreeSnapshot) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.
    """
    absolute = Path(run.repo_root) / path
    original = before.get(path)
    if original:
        if original.symlink_target is not None:
            try:
                if absolute.is_dir() and not absolute.is_symlink():
                    return "could not restore symlink (agent replaced it with a directory)"
                if absolute.exists() or absolute.is_symlink():
                    absolute.unlink()
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.symlink_to(original.symlink_target)
                return "restored operator's pre-agent symlink"
            except OSError as error:
                return f"could not restore symlink ({error})"
        if original.content is None:
            # The operator had already deleted this tracked path. If the agent
            # recreated it, restoring the pre-agent state means deleting the
            # recreation—not checking HEAD out over the operator's deletion.
            if ":missing:" not in original.fingerprint:
                return "could not restore (original content unavailable)"
            try:
                if absolute.exists() or absolute.is_symlink():
                    absolute.unlink()
                return "restored operator's pre-agent deletion"
            except OSError as error:
                return f"could not restore deletion ({error})"
        try:
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(original.content)
            if original.mode is not None:
                os.chmod(absolute, original.mode)
            return "restored operator's pre-agent content"
        except OSError as error:
            return f"could not restore ({error})"
    current = after.get(path)
    if current and current.untracked:
        try:
            absolute.unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"
    result = subprocess.run(["git", "restore", "--source=HEAD", "--worktree", "--", path],
                            cwd=run.repo_root, capture_output=True, text=True)
    return "rolled back" if result.returncode == 0 else "could not roll back"


def enforce(run, phase, agent: AgentConfig, before: TreeSnapshot) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    after = snapshot(run)
    touched = changed_paths(before, after)
    breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")

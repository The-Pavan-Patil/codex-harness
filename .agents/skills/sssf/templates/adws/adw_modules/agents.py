"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from . import agent_codex, permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         CodexRequest, GateCheck, GateReport, Phase, SSSFConfig,
                         UsageBreakdown)

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        for key in ("permission_profile", "subagents"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
    return SSSFConfig(**raw)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent != "codex":
            problems.append(f"agent {name!r}: coding_agent must be 'codex'")
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        if not agent.model.strip():
            problems.append(f"agent {name!r}: model is required")
    environment_problem = agent_codex.environment_problem()
    if environment_problem:
        problems.append(environment_problem)
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> Codex -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    thread_id = _agent_thread_id(run, agent)
    permission_profile = agent.permission_profile or f"sssf-{agent.name}"
    output_schema_path = agent_dir / "output_schema.json"
    output_schema_path.write_text(json.dumps(_strict_schema(call.output_type), indent=2))
    hook_spool_path = agent_dir / "codex_hooks.jsonl"
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": thread_id or "(new Codex thread)",
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,
                                          "permission_profile": permission_profile,
                                          "subagents": agent.subagents}))
    run.console.agent_started(agent.name, agent.model, thread_id or "new")

    # Parse retries and gate corrections re-enter the SAME Codex thread, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: agent_codex.CodexResult | None = None
    spent = UsageBreakdown()
    active_thread_id = thread_id

    def send(prompt_text: str) -> agent_codex.CodexResult:
        nonlocal latest, active_thread_id
        request = CodexRequest(
            prompt=prompt_text,
            developer_instructions=system_text,
            model=agent.model,
            thinking=agent.thinking,
            thread_id=active_thread_id,
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            output_schema_path=str(output_schema_path.resolve()),
            permission_profile=permission_profile,
            permission_filesystem=_permission_filesystem(run, agent),
            subagents=agent.subagents,
            hook_spool_path=str(hook_spool_path.resolve()),
            cwd=str(run.repo_root),
        )
        result = agent_codex.run(
            request,
            on_event=_event_forwarder(run, phase, agent.name),
            on_spawn=lambda pid: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid,
                f"{agent.coding_agent} {agent.name} {agent.model}"),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        active_thread_id = result.thread_id
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, active_thread_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": active_thread_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent,
                                    "permission_profile": permission_profile})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": None,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_thread_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    profile = agent.permission_profile or f"sssf-{agent.name}"
    if (entry and entry.get("model") == agent.model
            and entry.get("coding_agent") == "codex"
            and entry.get("permission_profile") == profile):
        return entry["session_id"]           # rejoin the existing context window
    return ""


def _event_forwarder(run, phase: Phase, agent_name: str):
    """One tool_call event per real tool call, with its exact args and result."""
    tracker = agent_codex.ToolCallTracker()

    def forward(event: dict) -> None:
        if event.get("type") == "sssf.hook":
            hook = event.get("hook") or {}
            hook_name = str(hook.get("hook_event_name") or "CodexHook")
            if hook_name not in {"SubagentStart", "SubagentStop"}:
                return
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="subagent_start" if hook_name == "SubagentStart" else "subagent_end",
                name=str(hook.get("agent_type") or hook.get("agent_id") or "subagent"),
                payload={**hook, "agent": agent_name},
            ))
            return
        record = tracker.observe(event)
        if record is None:
            return
        # The call's span rides the columns; duration_ms stays in the payload as
        # Codex's completion event remains available in the payload as a convenience.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label"),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
    return forward


def _strict_schema(output_type) -> dict:
    """Make Pydantic's schema deterministic for Codex structured output."""
    schema = output_type.model_json_schema()

    def visit(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def _permission_filesystem(run, agent: AgentConfig) -> dict:
    """Build the role's complete profile for an inline Codex config override.

    Project config remains the human-readable durable policy. Passing the same
    rules inline makes unattended runs deterministic even before Codex has
    marked a newly stamped repository as trusted.
    """
    repo_root = Path(run.repo_root)

    def native_paths(pattern: str) -> list[str]:
        """Translate an SSSF allowlist pattern into portable Codex paths.

        Codex supports globs for ``deny`` rules, but portable ``read`` and
        ``write`` rules must name an exact path or a ``directory/**`` subtree.
        Existing wildcard matches can therefore be expanded to exact paths;
        creation through a wildcard is enforced by SSSF's post-run hash check.
        """
        clean = pattern.rstrip("/")
        if not clean:
            return []
        if not any(char in clean for char in "*?"):
            candidate = repo_root / clean
            return [f"{clean}/**" if candidate.is_dir() else clean]
        if clean.endswith("/**") and not any(char in clean[:-3] for char in "*?"):
            return [clean]
        matches: list[str] = []
        for candidate in repo_root.glob(clean):
            relative = candidate.relative_to(repo_root).as_posix()
            matches.append(f"{relative}/**" if candidate.is_dir() else relative)
        return sorted(set(matches))

    writes = agent.writes or []
    has_unportable_write_glob = any(
        any(char in pattern.rstrip("/") for char in "*?")
        and not (pattern.rstrip("/").endswith("/**")
                 and not any(char in pattern.rstrip("/")[:-3] for char in "*?"))
        for pattern in writes
    )
    root_rules: dict[str, str] = {
        # A wildcard write allowlist such as ``**/*.md`` cannot be expressed
        # by Codex's native profile. Let the command run, then apply the exact
        # SSSF allowlist through the byte-for-byte verifier below the sandbox.
        ".": "write" if agent.writes is None or has_unportable_write_glob else "read",
    }
    for path in native_paths(run.cfg.defaults.data_dir.rstrip("/")):
        root_rules[path] = "write"
    for pattern in run.cfg.defaults.protected_files:
        for path in native_paths(pattern):
            root_rules[path] = "read"
    for pattern in writes:
        for path in native_paths(pattern):
            root_rules[path] = "write"
    # Git mutations belong to explicit code phases. The content-hash verifier
    # cannot see a rewritten ref or index, so no role-level writes override
    # this native boundary—even if a broad SSSF glob happens to match it.
    root_rules[".git/**"] = "read"
    return {":minimal": "read", ":workspace_roots": root_rules}


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2))

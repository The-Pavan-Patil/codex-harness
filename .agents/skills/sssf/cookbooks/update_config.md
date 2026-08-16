# Update Config

Edit `adws/adw_sssf_config/sssf.config.yaml`; edit `.codex/config.toml` whenever
the role's operating boundary changes.

## Retune model or thinking

```yaml
agents:
  - name: reviewer
    model: gpt-5.6
    thinking: high
```

Use model ids available to the signed-in Codex subscription. Changing a role's
model starts a fresh Codex thread the next time that ADW id uses the role.

## Change permissions

Keep both layers aligned:

```yaml
permission_profile: sssf-documenter
writes: [app_docs/, docs/, "**/*.md", "*.md"]
```

The named `.codex` profile blocks writes during execution. `writes` and
`protected_files` verify the resulting Git tree afterward. Never broaden one
without reviewing the other.

## Native subagents

```yaml
subagents: true
```

Enable this for planner/scout-style roles only when parallel reconnaissance is
useful. Add or refine project agents in `.codex/agents/*.toml`, and instruct the
parent prompt to wait for every child before emitting its envelope.

## Add a role

1. Add its YAML entry and purpose.
2. Add `system.md` and `user.md`.
3. Add a concrete output type and keep the prompt example synchronized.
4. Add a named `.codex` permission profile.
5. Add its name to the appropriate ADW's `REQUIRED_AGENTS`.
6. Test config validation, structured output, permissions, and event tracing.

`tools` is documentation of intended capabilities, not a hard Codex builtin
allowlist. Permission profiles and post-run hashes are the enforcement boundary.

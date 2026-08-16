# Create Config

Generate the starter Codex roster:

```bash
uv run .agents/skills/sssf/scripts/make_config.py
```

The output is `adws/adw_sssf_config/sssf.config.yaml`. It defines five roles:
planner, builder, scout, reviewer, and documenter. Model and thinking defaults
merge into every role; the ADWs name roles, never models.

The installer separately stamps `.codex/config.toml`, whose permission profile
names must match each role's `permission_profile`. When adding a role, add both
the YAML identity and a least-privilege Codex profile.

After generating:

1. Confirm `codex login status` succeeds.
2. Confirm both prompt paths exist.
3. Confirm every required ADW role resolves through `agents.validate()`.
4. Use `writes: []` for read-only product roles and enable `subagents` only for
   independent read-heavy work.
5. Run the scout smoke test before a write-capable workflow.

See [the config reference](../references/config.md) for the complete contract.

# Install

Stamp the Codex-backed factory into the current repository.

## Prerequisites

- Codex CLI; run `codex login` and choose ChatGPT subscription authentication.
- `uv` and `sqlite3`.
- `bun` only for the visualizer.
- A Git repository with an initial commit for workflows that commit.

No API key or separate harness runtime is required. Treat `~/.codex/auth.json` as a
password: never copy it into the repository.

## Run it

```bash
uv run .agents/skills/sssf/scripts/install.py
```

Run from the target repository root. A user-scoped skill uses
`~/.agents/skills/sssf/scripts/install.py`.

## What gets stamped

| Stamped | Purpose |
|---|---|
| `adws/adw_*.py` | twelve starter workflows |
| `adws/adw_modules/` | Codex adapter, gates, permissions, quality, trace, and Git logic |
| `adws/adw_sssf_config/sssf.config.yaml` | role roster |
| `adws/adw_data/prompt_engineering/` | user-owned role prompts |
| `.codex/config.toml` | role permission profiles and multi-agent defaults |
| `.codex/agents/` | read-only native subagent definitions |
| `.codex/hooks.json` | subagent lifecycle trace hooks |
| `.env.sample`, `justfile` | optional local overrides and recipes |

Runtime sessions and SQLite data are gitignored.

## Idempotency

Existing files are skipped. `--force` overwrites every stamped file, including
the roster, prompts, and `.codex` policy, so commit user-owned edits first.

## Post-install checklist

1. Run `codex login status`.
2. Open `codex` in the repository, trust the project, then review and trust the
   SSSF hook definitions with `/hooks`.
3. Replace every unconfigured block in `adws/adw_modules/quality.py` with the
   project's real argv. Unconfigured checks fail closed with exit 78.
4. Ensure commit-producing workflows start from a clean working tree.
5. Run `just demo`, or:

```bash
uv run adws/adw_prompt.py "reply with a one-line summary of this repo" --agent scout
```

Green means configuration validated, Codex authenticated, a thread ran,
structured output parsed, and events reached `adws/adw_data/sssf.db`.

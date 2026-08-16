# Config Reference

`adws/adw_sssf_config/sssf.config.yaml` defines the SSSF roster. Project
`.codex/config.toml` defines the operating boundary Codex enforces while those
roles run. They are separate deliberately: YAML says who the role is; Codex
permissions say what its local commands may affect.

## Shape

```yaml
defaults:
  coding_agent: codex
  model: gpt-5.6-terra
  thinking: medium
  permission_profile: ""
  subagents: false
  tools: [read, search, bash, apply_patch]
  protected_files: [.git/, adws/adw_modules/, adws/adw_sssf_config/, adws/adw_*.py, .codex/]
  data_dir: adws/adw_data

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

agents:
  - name: scout
    purpose: Find and report where things live; change nothing.
    permission_profile: sssf-scout
    subagents: true
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/scout/system.md
      user: adws/adw_data/prompt_engineering/scout/user.md
    writes: []
```

## Fields

### `defaults`

| Field | Meaning |
|---|---|
| `coding_agent` | Must be `codex`. |
| `model` | Codex model id, passed to `codex exec --model`. |
| `thinking` | `model_reasoning_effort`; supported values depend on the model. |
| `permission_profile` | Named project Codex permission profile. Empty becomes `sssf-<agent>`. |
| `subagents` | Whether native Codex multi-agent tools are exposed for this role. |
| `tools` | Role intent shown in prompts/traces; not a Codex builtin allowlist. |
| `protected_files` | Paths the post-run checker rejects unless explicitly unlocked. |
| `data_dir` | Sessions, handoffs, raw events, schemas, and SQLite runtime. |

### `agents[]`

Every entry requires `name`, `purpose`, and `prompt_engineering`. Model,
thinking, permission profile, subagents, tools, color, and writes merge over
defaults.

`writes` is the second enforcement layer:

- omitted/`null`: product writes allowed except `protected_files`;
- `[]`: no product repository writes;
- paths/globs: only matching paths, plus runtime under `data_dir`.

The checker hashes content before and after every agent phase and restores
unauthorized changes, including modifications to already-dirty files. Codex
permission profiles should block the same change before it happens.

The generated native profile always keeps `.git/**` read-only for agent turns,
even when a role otherwise has full product writes. Ref/index mutations do not
appear in a working-tree content hash, so branch, stage, and commit operations
belong exclusively to explicit `kind="code"` phases.

## Models and authentication

Use model ids available to the signed-in Codex account. SSSF deliberately does
not accept provider prefixes or provider API keys. `agents.validate()` checks
that Codex is installed and logged in; server-side model availability is
reported by Codex when the turn starts.

Authentication comes from `codex login`. Do not put API keys or
`~/.codex/auth.json` in `.env`.

## Structured output and resume

For each call, the harness derives JSON Schema from its Pydantic output type,
writes it beside the run, and passes it through `--output-schema`. The returned
JSON is still validated with Pydantic. Parse and gate corrections call
`codex exec resume <thread-id>`, preserving the role's conversation.

`agent_map.json` is reused only when coding agent, model, and permission profile
all match. Changing any of those starts a fresh thread.

## Native subagents

Set `subagents: true` only on roles that benefit from parallel read-heavy work.
The starter planner and scout can use project agents from `.codex/agents/`.
Primary ADW sequencing never moves into native subagents; Python remains the
control plane.

## Tool boundaries

Codex does not expose a per-tool allowlist for every builtin tool. Enforce
effects with:

1. the selected `.codex` filesystem permission profile during execution;
2. project hooks for lifecycle/policy needs;
3. SSSF post-run `writes`/`protected_files` hashing.

MCP servers, web search, connectors, and browser tools have separate controls
and should remain disabled unless a role explicitly needs them.

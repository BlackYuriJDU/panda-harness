# Panda Harness — v0.1 Contract

**Version:** `0.1.0a1` (alpha)
**Status:** frozen for the v0.1 line
**Runtime:** OpenCode v2 + OpenRouter
**License:** MIT

This document is the contract for v0.1. Anything not listed here is out of scope.
Exit codes, MCP tool names, JSON schema IDs, and on-disk file names are public
interface: changing them requires a contract revision and a CHANGELOG entry.

---

## 1. Purpose

Panda turns evidence discipline into a tool surface. A collaborator (human or
agent) working on an evidence-heavy corpus cannot, through Panda:

- write outside the approved scopes;
- publish a release from a corpus whose validator fails;
- run a command classified as destructive;
- leave a provenance record that cannot be verified.

The guarantee does not depend on a prompt. It depends on the tool that performs
the action.

## 2. Supported environment

| Component | Requirement |
|---|---|
| Python | 3.12 or newer |
| Node.js | 20 or newer (validator runtime) |
| OpenCode | v2, `opencode` on `PATH` |
| OpenRouter | `OPENROUTER_API_KEY` or `opencode auth login` |
| Git | optional, used for provenance metadata |

Windows is supported. On Windows, Panda resolves npm `.cmd` shims to the native
executable before spawning, because `CreateProcess` cannot execute `.cmd`
directly. The MCP server is an optional extra (`panda-harness[mcp]`) because the
MCP SDK's `cryptography` dependency may lack a wheel for every platform.

## 3. Workspace contract

A **workspace root** is the nearest ancestor directory that contains either:

1. `.panda/config.json`, or
2. `corpus/validation/validate.mjs`.

If neither exists, commands that require a workspace fail with exit code `2`.
`panda init` may create a workspace in a directory that already has the
canonical validator. `panda doctor` and `panda status` are read-only.

Expected layout:

```text
<root>/
├── .panda/
│   ├── config.json          # typed config, schema_version = 1
│   ├── policy.json          # fail-closed rule set
│   ├── instructions.md      # agent instructions injected into OpenCode
│   ├── backups/             # pre-rewrite copies (gitignored)
│   └── sessions/            # JSONL ledgers + anchors (gitignored)
├── corpus/
│   ├── validation/validate.mjs   # canonical validator
│   └── studies/INDEX.md          # study registry
└── opencode.json            # OpenCode v2 config (never rewritten if JSONC)
```

### 3.1 Initialization

`panda init`:

- writes `.panda/config.json`, `.panda/policy.json`, `.panda/instructions.md`;
- creates `.panda/sessions/` (`.panda/backups/` appears when a file is backed up);
- adds a `.panda/.gitignore`;
- writes or merges `opencode.json` unless `--no-opencode` is given;
- backs up any file it replaces;
- never rewrites an existing `opencode.jsonc` (fails closed with exit code `2`);
- is idempotent: generated files that are already correct are left untouched;
- with `--dry-run`, writes nothing and reports planned changes.

If an existing OpenCode config has a `model`, that model is preserved unless
`--force-model` is passed. Existing permission rules are preserved. Restrictive
rules (`deny`/`ask`) are re-appended after Panda's rules so a user restriction
cannot be weakened by merging.

### 3.2 Configuration

`.panda/config.json` is validated with Pydantic (`extra="forbid"`). Defaults:

| Key | Default |
|---|---|
| `model` | `openrouter/anthropic/claude-sonnet-5.5` |
| `validator` | `corpus/validation/validate.mjs` |
| `node` | `node` |
| `node_min_major` | `20` |
| `opencode.binary` | `opencode` |
| `opencode.agent` | `panda` |
| `opencode.config_path` | `opencode.json` |
| `opencode.standalone` | `true` |
| `timeouts.validate_seconds` | `180` |
| `timeouts.run_seconds` | `null` (no limit) |
| `logging.redact` | `true` |
| `logging.log_tasks` | `true` |

`validator` and `instructions` must be relative paths that stay inside the
workspace. `model` must use the `provider/model` form.

## 4. Commands

| Command | Behavior |
|---|---|
| `panda` | Same as `panda status`. |
| `panda status` | Read-only workspace and last-session summary. |
| `panda init` | Initialize or update a workspace. |
| `panda doctor` | Read-only diagnostics. `--validate` also runs the validator. |
| `panda validate` | Run the canonical validator. |
| `panda run "<task>"` | Launch OpenCode with the Panda MCP server. |
| `panda session start` | Create a hash-chained session ledger. |
| `panda session list` | List sessions, newest first. |
| `panda session show <id>` | Show events and verification state. |
| `panda session verify [id]` | Verify one session or all sessions. |
| `panda session close <id>` | Append a terminal `session.end` event. |
| `panda policy check <action> <resource>` | Evaluate one action. |
| `panda policy explain <action> <resource>` | Show the matching rules. |
| `panda policy show` | Print the effective policy. |
| `panda study list` | List the study registry. |
| `panda study show <id>` | Show one study card. |
| `panda mcp serve` | Run the MCP stdio server. |
| `panda mcp tools` | Print the MCP tool/resource contract. |
| `panda version`, `panda brand` | Version and brand output. |

All commands accept `--root/-r` where meaningful. `--json` emits machine-readable
output on stdout for successful results. Diagnostics and errors are written to
stderr; a consumer must use the exit code, not string matching.

### 4.1 `panda run`

```text
panda run "<task>" [--model M] [--agent A] [--format default|json] [--title T]
                  [--standalone | --shared] [--dry-run] [--private]
                  [--session ID] [--timeout S] [--quiet]
```

- Requires an initialized workspace with a Panda MCP server in the OpenCode
  config. Otherwise exit code `2` with a `panda init` hint.
- Defaults to a private OpenCode server (`--standalone`); `--shared` opts out.
- Creates a Panda session unless `--session` continues one.
- Records task hash and length; records task text only when
  `logging.log_tasks` is true and `--private` is not set.
- `--dry-run` records `run.plan`, closes the session as `planned`, prints the
  command, and does not launch OpenCode.
- Propagates OpenCode's exit code. A timeout maps to exit code `4`.

## 5. Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Validation failed (validator ran and reported failure), or `doctor` has a failed required check. |
| `2` | Usage error or configuration error (bad flags, missing workspace, unsupported JSONC, invalid config). |
| `3` | Required dependency missing (Node.js, MCP SDK, OpenCode executable). |
| `4` | Protocol error, execution timeout, or session-integrity failure. |
| `5` | Policy denied, including `ask` outside an interactive approval. |
| `6` | Unexpected internal error. |
| `130` | Interrupted with Ctrl-C. |
| other | `panda run` propagates OpenCode's own exit code. |

`panda status --json` returns `0` even when the directory is not a workspace; the
payload carries `ok: false` and a stable `code`.

## 6. Policy

Policy is a JSON rule set (`schema_version: 1`) with ordered rules. The **last
matching rule wins**. If no rule matches, `default` applies (`ask` by default).

Effects:

- `allow` — permitted.
- `ask` — requires approval; non-interactive callers treat it as denied.
- `deny` — blocked.
- `hard_deny` — blocked even with `--approve`.

Hard denies include secret paths (`.env`, `*.pem`, `*.key`, `.ssh/`, etc.),
force-push, history rewrites, recursive deletion, and release publishing.
`panda policy check` exits `0` only for `allow`; every other effect exits `5`.

Path resources are resolved before matching. `corpus/../README.md` normalizes to
`README.md` and cannot borrow a `corpus/*` allow rule.

## 7. Session ledger

Each session is an append-only JSONL file plus an anchor file:

```text
.panda/sessions/s_20261025T120000Z_ab12cd.jsonl
.panda/sessions/s_20261025T120000Z_ab12cd.anchor.json
```

Every record contains `schema` (`panda.session.event.v1`), `seq`, `ts`,
`session_id`, `type`, `data`, `prev`, and `hash`. `hash` is SHA-256 over the
canonical record without `hash`; `prev` is the previous record's hash. The anchor
pins the tail `(seq, hash)`, so truncation is detectable. A missing anchor is a
warning for legacy logs; a mismatched anchor is an error.

Redaction runs before hashing. Keys such as `api_key`, `authorization`, `token`,
and values matching known secret patterns become `[redacted]`. Redaction can be
disabled in config, but the default is on.

`panda session verify` exits `1` if any session fails verification.

## 8. MCP surface

Server name: `panda`. Transport: stdio by default. `panda mcp serve
--transport streamable-http` exposes `/mcp` on a loopback host
(`127.0.0.1`, `localhost`, or `::1`). Non-loopback hosts are refused; the HTTP
transport has no authentication and must not be exposed to a network. Direct
tool names are unprefixed; OpenCode exposes them as `panda_<tool>`.

| Tool | Purpose |
|---|---|
| `workspace_info` | Root, model, validator, policy, OpenCode status, session state. |
| `validate` | Run the canonical validator (`release` optional). |
| `policy_check` | Evaluate an action/resource pair (`approved` optional). |
| `study_list` | List registered studies. |
| `study_show` | Show one study card. |
| `session_note` | Append a durable note to the session ledger. |

Resources: `panda://workspace`, `panda://studies` (JSON text).

The MCP server is read-only with respect to corpus content. It never writes
evidence or claims in v0.1. `session_note` writes only to the provenance ledger.

## 9. JSON Schemas

The repository ships four Draft 2020-12 schemas under `schemas/`:

| Schema ID | Purpose |
|---|---|
| `panda.session.event.v1` | Session ledger event. |
| `panda.metric.v1` | Evidence metric with status and provenance. |
| `panda.harness.proposal.v1` | Proposed harness change with required evidence. |
| `panda.fanout.plan.v1` | Bounded parallel work plan. |

Schemas are validated in CI against valid and invalid fixtures in
`fixtures/schemas/`.

## 10. Acceptance criteria for v0.1

- `panda init` is idempotent, backs up before rewriting, and fails closed on
  JSONC.
- `panda doctor` is read-only and reports required failures with exit `1`.
- `panda validate` runs the corpus's own validator; Panda does not duplicate
  validation logic. A real corpus run reports `10/10` checks and exit `0`.
- `panda run --dry-run` prints a launch command containing `--standalone` by
  default and records a closed `planned` session.
- Policy decisions are last-match-wins; traversal cannot escape scopes; `ask` is
  denied non-interactively.
- Session edits, reordering, truncation, and tail rewrites are detected.
- MCP exposes exactly six tools and two resources; the same policy and
  provenance rules apply as in the CLI.
- `ruff check .` passes and `pytest` passes on the supported Python versions.

## 11. Out of scope for v0.1

- `evidence_add`, `claim_add`, `pipeline_run`, `release_build`.
- Literature/biomedical lookup tools (BioMCP integration).
- Provider/model fallback, token accounting, and cost dashboards.
- Multi-user server mode, remote transport, authentication.
- GitHub App, automatic commits, pushes, or releases.
- Rewriting arbitrary OpenCode JSONC.
- Clinical interpretation or medical advice.

These are candidates for later revisions, not implicit promises.

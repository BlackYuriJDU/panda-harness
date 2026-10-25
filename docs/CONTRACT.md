# Panda Harness — v0.2 Contract

**Version:** `0.2.0a1` (unreleased)
**Status:** frozen for the v0.2 line once tagged
**Runtime:** Panda cockpit + REPL + headless run, over the OpenCode Console provider
**License:** MIT

This document is the contract for v0.2. Anything not listed here is out of scope.
Exit codes, tool names, JSON schema IDs, and on-disk file names are public
interface: changing them requires a contract revision and a CHANGELOG entry.

v0.1 shipped an OpenCode v2 launcher. v0.2 removes OpenCode from the runtime
path: Panda talks to the provider directly and owns the agent loop, the tools,
the approvals, and the ledger. The legacy OpenCode export still exists as an
opt-in for other clients (`panda init --opencode`), never as a dependency.

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
| Node.js | 20 or newer, only for the corpus validator |
| OpenCode Console | `https://opencode.ai/inference` by default |
| API key | `OPENCODE_API_KEY` (name configurable via `provider.api_key_env`) |
| Git | optional, used for provenance metadata |
| OpenCode CLI | **not required and not used** |

Windows is supported. The MCP server is an optional extra
(`panda-harness[mcp]`) because the MCP SDK's `cryptography` dependency may lack a
wheel for every platform. The cockpit needs `textual`, which is a core
dependency; without a TTY, use `--plain`.

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
│   ├── config.json          # typed config, schema_version = 2
│   ├── policy.json          # fail-closed rule set
│   ├── instructions.md      # corpus rules injected into the system prompt
│   ├── backups/             # pre-rewrite copies (gitignored)
│   └── sessions/            # JSONL ledgers + anchors (gitignored)
├── corpus/
│   ├── validation/validate.mjs   # canonical validator
│   ├── studies/INDEX.md          # study registry
│   ├── evidence/evidence.jsonl   # optional evidence records
│   ├── claims/*.md               # optional claim frontmatter
│   └── numbers.json              # optional number records
└── opencode.json            # legacy, only if `init --opencode` was used
```

### 3.1 Initialization

`panda init`:

- writes `.panda/config.json`, `.panda/policy.json`, `.panda/instructions.md`;
- creates `.panda/sessions/` (`.panda/backups/` appears when a file is backed up);
- adds a `.panda/.gitignore`;
- does **not** write `opencode.json` by default;
- with `--opencode`, also writes or merges `opencode.json` and wires the Panda
  MCP server (legacy export for OpenCode clients);
- backs up any file it replaces;
- never rewrites an existing `opencode.jsonc`; with `--opencode` that fails
  closed with exit code `2`;
- is idempotent: generated files that are already correct are left untouched;
- with `--dry-run`, writes nothing and reports planned changes.

Warnings are reported, not enforced: a missing `OPENCODE_API_KEY` and an
enabled legacy export both produce a warning line.

### 3.2 Configuration

`.panda/config.json` is validated with Pydantic (`extra="forbid"`). Defaults:

| Key | Default |
|---|---|
| `schema_version` | `2` (v1 files still load and are rewritten as v2) |
| `model` | `opencode/deepseek-v4.1-flash` |
| `provider.name` | `opencode` |
| `provider.base_url` | `https://opencode.ai/inference` |
| `provider.api_key_env` | `OPENCODE_API_KEY` |
| `provider.timeout_seconds` | `120` |
| `provider.context_window` | `131072` |
| `provider.max_output_tokens` | `8192` |
| `provider.temperature` | `0.2` |
| `validator` | `corpus/validation/validate.mjs` |
| `node` | `node` |
| `node_min_major` | `20` |
| `timeouts.validate_seconds` | `180` |
| `timeouts.run_seconds` | `null` (no limit) |
| `logging.redact` | `true` |
| `logging.log_tasks` | `true` |

`validator` and `instructions` must be relative paths that stay inside the
workspace. `model` must use the `provider/model` form and its provider must
match `provider.name`. The API key itself is never stored in the workspace; only
the environment variable name is.

## 4. Commands

| Command | Behavior |
|---|---|
| `panda` / `panda chat` | Open the cockpit; `--plain` opens the line REPL. |
| `panda status` | Read-only workspace, provider, model, and last-session summary. |
| `panda init` | Initialize or update a workspace (legacy OpenCode export is opt-in). |
| `panda doctor` | Read-only diagnostics. `--validate` also runs the validator. |
| `panda validate` | Run the canonical validator. |
| `panda models` | List the provider catalogue with protocol, callable state, and key state. |
| `panda run "<task>"` | Run one task headless through the agent loop. |
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
| `panda mcp serve` | Run the optional MCP server (stdio or loopback HTTP). |
| `panda mcp tools` | Print the MCP tool/resource contract. |
| `panda version`, `panda brand` | Version and brand output. |

All commands accept `--root/-r` where meaningful. `--json` emits machine-readable
output on stdout for successful results. Diagnostics and errors are written to
stderr; a consumer must use the exit code, not string matching.

### 4.1 `panda run` and `panda chat`

```text
panda run "<task>" [--model M] [--max-steps N] [--timeout S] [--session ID]
                   [--auto] [--private] [--dry-run] [--json] [--quiet]
panda chat [--model M] [--max-steps N] [--session ID] [--auto] [--private]
           [--plain]
```

- Both require an initialized workspace (exit `2` otherwise) and an API key for
  the model call (exit `4` with a hint naming the environment variable).
- A **turn** is one user message plus up to `--max-steps` model steps
  (default `24`) with tool calls in between.
- `--auto` approves every `ask` decision for the process. Without a human at a
  prompt, `ask` is denied.
- `--dry-run` records `run.plan`, prints model/provider/tools/session, closes the
  session as `planned`, and never calls the provider.
- `--private` persists only the task hash and length; the text is not written.
- `--json` suppresses streaming and prints one result object
  (`status`, `text`, `steps`, `tool_calls`, `usage`, `context_tokens`,
  `context_window`, `context_ratio`, `session_id`).
- A timeout maps to exit code `4`.

### 4.2 `panda models`

The catalogue has two sources: the live provider (`GET /v1/models`) and a
captured snapshot shipped with Panda. `--refresh` refuses the snapshot and exits
non-zero when the live call fails.

Each row reports:

- `protocol` — `openai-chat`, `anthropic`, `responses`, or `google`;
- `callable` — the protocol Panda implements (only `openai-chat`);
- `ready` — callable **and** the API key env is present in this process.

The human table shows `callable` in green only when the key is present; otherwise
it shows `callable, needs key` in amber. A supported protocol is a statement
about the adapter, not a promise that a call will succeed.

## 5. Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Validation failed, or `doctor` has a failed required check. |
| `2` | Usage or configuration error (bad flags, missing workspace, unsupported JSONC, unsupported model protocol, invalid config). |
| `3` | Required dependency missing (Node.js, MCP SDK). |
| `4` | Provider error (auth, rate limit, transport, protocol), execution timeout, or session-integrity failure. |
| `5` | Policy denied, including `ask` outside an interactive approval. |
| `6` | Unexpected internal error. |
| `130` | Interrupted with Ctrl-C. |

`panda status --json` returns `0` even when the directory is not a workspace; the
payload carries `ok: false` and a stable `code`.

## 6. Provider and model routing

The provider speaks the OpenAI-compatible chat completions protocol
(`POST {base_url}/openai/v1/chat/completions`, streaming by default). The model
id decides the wire protocol:

| Prefix | Protocol | Supported |
|---|---|---|
| everything not listed below | `openai-chat` | yes |
| `claude-*` | `anthropic` | no |
| `gpt-*`, `o1*`, `o3*` | `responses` | no |
| `gemini-*` | `google` | no |

Selecting an unsupported model is a configuration error (exit `2`) and the
message names the protocol. Panda never calls a model with the wrong adapter and
never silently falls back to another model.

The error surface is stable: auth failures say the key env name, rate limits say
the retry hint, and no message contains the key or request headers.

## 7. Tools

The agent loop can call these tools; the same registry backs the cockpit, the
REPL, headless `run`, and the MCP export.

| Group | Tools |
|---|---|
| Workspace | `workspace_info`, `read_file`, `list_files`, `grep` |
| Validation | `validate`, `policy_check` |
| Studies | `study_list`, `study_show` |
| Evidence | `evidence_list`, `evidence_show` |
| Claims | `claim_list`, `claim_show` |
| Numbers | `number_list`, `number_show` |
| Writes | `write_file`, `edit_file` (create/replace, exact-string edit) |
| Process | `run_shell` (argv array, no shell, bounded timeout, `cwd` inside root) |
| Provenance | `session_note` |

Every path is resolved inside the workspace before use. Reads outside the
workspace, secret paths, and traversal are refused by policy or by path
resolution, not by prompt text. `write_file`, `edit_file`, and `run_shell` are
the non-read-only tools and require a policy `allow` (or an approved `ask`).

## 8. Policy and approvals

Policy is a JSON rule set (`schema_version: 1`) with ordered rules. The **last
matching rule wins**. If no rule matches, `default` applies (`ask` by default).

Effects:

- `allow` — permitted.
- `ask` — requires approval; non-interactive callers treat it as denied.
- `deny` — blocked.
- `hard_deny` — blocked even with `--approve`.

Hard denies include secret paths (`.env`, `*.pem`, `*.key`, `.ssh/`, etc.),
force-push, history rewrites, recursive deletion, and release publishing.

An `ask` becomes an approval request with a single decision:
**allow once** in the cockpit modal or the REPL prompt. There is no "remember"
and no global allow. In `panda run` without `--auto`, an `ask` is denied and the
tool returns a blocked result, not an exception loop. Every request and decision
is written to the ledger as `approval.request` / `approval.decision`.

`panda policy check` exits `0` only for `allow`; every other effect exits `5`.
Path resources are resolved before matching, so `corpus/../README.md` normalizes
to `README.md` and cannot borrow a `corpus/*` allow rule.

## 9. Session ledger

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

A chat session records `session.start`, `user.message`, `assistant.message`,
`tool.call`, `tool.result`, `approval.*`, and a terminal `session.end` with the
model and turn count. Redaction runs before hashing and persistence. `--private`
keeps task text out of every record.

`panda session verify` exits `1` if any session fails verification.

## 10. MCP export (optional)

MCP is not part of the Panda agent loop. It exists so other clients can reuse the
read-only surface.

Server name: `panda`. Transport: stdio by default. `panda mcp serve
--transport streamable-http` exposes `/mcp` on a loopback host (`127.0.0.1`,
`localhost`, or `::1`). Non-loopback hosts are refused; the HTTP transport has no
authentication and must not be exposed to a network.

| Tool | Purpose |
|---|---|
| `workspace_info` | Root, model, validator, policy, studies, session state. |
| `validate` | Run the canonical validator (`release` optional). |
| `policy_check` | Evaluate an action/resource pair. |
| `study_list` | List registered studies. |
| `study_show` | Show one study card. |
| `session_note` | Append a durable note to the session ledger. |

Resources: `panda://workspace`, `panda://studies` (JSON text). The MCP surface
never writes corpus content, evidence, or claims; `session_note` writes only to
the ledger. Installing the extra is opt-in: without it, `panda mcp serve` fails
with exit `3` and the rest of the CLI works.

## 11. JSON Schemas

The repository ships four Draft 2020-12 schemas under `schemas/`:

| Schema ID | Purpose |
|---|---|
| `panda.session.event.v1` | Session ledger event. |
| `panda.metric.v1` | Evidence metric with status and provenance. |
| `panda.harness.proposal.v1` | Proposed harness change with required evidence. |
| `panda.fanout.plan.v1` | Bounded parallel work plan. |

Schemas are validated in CI against valid and invalid fixtures in
`fixtures/schemas/`.

## 12. Acceptance criteria for v0.2

- `panda init` is idempotent, backs up before rewriting, writes no
  `opencode.json` unless `--opencode` is passed, and fails closed on JSONC in
  legacy mode.
- `panda doctor` is read-only and reports required failures with exit `1`.
- `panda validate` runs the corpus's own validator; Panda does not duplicate
  validation logic. A real corpus run reports `10/10` checks and exit `0`.
- `panda run --dry-run` prints model, provider, tools, and session, records a
  closed `planned` session, and makes no network call.
- A real provider stream produces text deltas and a `run.end` ledger event; a
  missing key exits `4` naming `OPENCODE_API_KEY` and never prints a value.
- `panda models` reports protocol, callable, and key state honestly; `ready` is
  false without the key and the catalogue counts match the snapshot (85 models,
  38 OpenAI-compatible at capture time).
- Policy decisions are last-match-wins; traversal cannot escape scopes; `ask` is
  denied non-interactively and approved explicitly interactively.
- Session edits, reordering, truncation, and tail rewrites are detected.
- The tool registry exposes exactly the tools in section 7; read-only tools stay
  read-only and writes go through policy.
- `ruff check src tests` passes and `pytest` passes on the supported Python
  versions, with and without the MCP extra.
- Unsupported protocols (`claude-*`, `gpt-*`, `gemini-*`) fail with a clear
  configuration error before any request is made.

## 13. Out of scope for v0.2

- Anthropic, Responses, and Google adapters. The routing is classified and
  refused, not emulated.
- Provider/model fallback, token accounting dashboards, cost optimisation.
- Multi-user server mode, remote MCP transport, authentication, tenancy.
- GitHub App, automatic commits, pushes, or releases.
- Sandboxing beyond policy and workspace path resolution.
- Literature/biomedical lookup tools (BioMCP integration).
- Clinical interpretation or medical advice.

These are candidates for later revisions, not implicit promises.

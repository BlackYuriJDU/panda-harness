# Panda Harness

<img src="assets/icon.svg" alt="Panda" width="72" align="right">

> A fail-closed terminal harness for evidence-heavy research corpora.

**Status:** alpha. `0.2.0a2` is the last tagged release; the line in `main` is
the unreleased `0.2.0a3`, described in [`docs/CONTRACT.md`](docs/CONTRACT.md).

Panda is a terminal agent and optional MCP server that keeps an AI model inside
an evidence discipline: approved write scopes, canonical validation, a verifiable
provenance ledger, and explicit approvals. The guarantee lives in the tool, not
in the prompt.

```text
If it does not resolve to a line, a script, a run, and a license, it does not ship.
```

## Why

Projects like [`dcj-study`](https://github.com/BlackYuriJDU/dcj-study) reanalyze
public data under hard rules:

- no number without evidence;
- no claim without a `study_id` and a `track`;
- no release containing restricted source payloads.

Those rules usually live in convention, a validator, and human review. Panda turns
them into a tool surface that a collaborator — human or agent — cannot silently
bypass.

## What it is

- An autonomous terminal agent (cockpit, line REPL, or headless `panda run`) for a
  corpus with a canonical validator and a study registry.
- A provider client for OpenCode Go: all four wire protocols the Go catalogue
  uses — OpenAI-compatible chat completions, Anthropic Messages for Claude, the
  OpenAI Responses API for GPT/Grok/Muse, and Google generateContent for Gemini
  — normalized into one event stream so the agent loop is protocol-agnostic.
- A policy engine: ordered rules, last-match-wins, hard denies, `ask` treated as
  deny when nobody can answer.
- A hash-chained session ledger with a tail anchor and redaction.
- An optional MCP server for other clients; the Panda runtime does not need it.

## What it is not

- Not an autonomous "AI scientist".
- Not a literature-review SaaS.
- Not a generic coding agent.
- Not an electronic lab notebook.
- Not a replacement for the corpus validator. Panda calls it.
- Not an OpenCode wrapper. The OpenCode CLI is not required.

## Requirements

| Component | Version |
|---|---|
| Python | 3.12+ |
| Node.js | 20+, only for `panda validate` |
| OpenCode Go key | `OPENCODE_API_KEY` in the environment |

Windows is supported. The MCP extra is optional. There is no OpenCode CLI
dependency and no OpenRouter account requirement.

## Install

Panda is not on PyPI yet. Install from source:

```bash
git clone https://github.com/BlackYuriJDU/panda-harness.git
cd panda-harness
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"
```

Omit `[mcp]` if you do not need `panda mcp serve`; the CLI, cockpit, and agent
work without it. The MCP extra is separate because the SDK's native dependencies
may not have a wheel on every platform.

## Quickstart

Run inside a corpus root — a directory containing
`corpus/validation/validate.mjs`:

```bash
export OPENCODE_API_KEY=...      # Windows: $env:OPENCODE_API_KEY = "..."
panda init
panda models
panda
```

`panda` opens the cockpit. `panda --plain` opens the line REPL, for terminals
without full-screen support or for scripting. Headless tasks:

```bash
panda run "List the registered studies and validate the corpus."
```

`panda init` writes `.panda/config.json`, `.panda/policy.json`,
`.panda/instructions.md`, and the session directory. It does **not** write
`opencode.json`; pass `--opencode` only if you want the legacy export for an
OpenCode client. It backs up anything it replaces and refuses to rewrite JSONC in
legacy mode. Existing model choices are preserved.

`panda models` lists the catalogue (38 models on OpenCode Go at capture time,
live when the network is available). Each row shows the protocol and whether it
is callable; `callable, needs key` means the protocol is implemented but the API
key is not exported in this shell.

The provider is OpenCode Go: `provider.name = "opencode-go"`,
`provider.base_url = "https://opencode.ai/inference/go"`, key in
`OPENCODE_API_KEY`. The default model is
`opencode-go/deepseek-v4.1-flash`. Every catalogue model is callable: OpenAI-chat
models use `/openai/v1/chat/completions`, Claude uses the Anthropic Messages
adapter (`/anthropic/v1/messages`), GPT/Grok/Muse use the Responses API
(`/openai/v1/responses`), and Gemini uses Google generateContent
(`/google/v1beta/models/<id>:streamGenerateContent`). Protocol classification is
explicit; there is no silent fallback. The legacy `opencode/` model prefix is
accepted as an alias for `opencode-go/` when the base URL is Go.

Go requires two request headers that generic OpenAI clients do not send: a
stable `x-opencode-session` id per conversation and a non-generic
`User-Agent`. Panda sends `User-Agent: panda-harness/<version>` and the session
ledger id as `x-opencode-session`; without them Go answers
`400 MissingSessionID`. The Console/Zen base URL (`/inference`) is not used by
default: the Go key is rejected there with `402 Insufficient funds`.

## Commands

| Command | Purpose |
|---|---|
| `panda` / `panda chat` | Cockpit (`--plain` for the line REPL). |
| `panda status` | Workspace, provider, model, and last-session summary. |
| `panda init` | Initialize or update the workspace (`--opencode` for the legacy export). |
| `panda models` | Catalogue with protocol, callable state, and key state. |
| `panda doctor` | Read-only diagnostics (`--validate` to also validate). |
| `panda validate` | Run the canonical validator (`--release`, `--record`). |
| `panda run "<task>"` | One headless agent turn (`--json`, `--dry-run`, `--max-steps`). |
| `panda session start/list/show/verify/close` | Manage the provenance ledger. |
| `panda policy check/explain/show` | Inspect and evaluate policy. |
| `panda study list/show` | Read the study registry. |
| `panda mcp serve/tools` | Run the optional MCP server or print its contract. |

Add `--json` for machine-readable output. Exit codes are stable and documented in
[`docs/CONTRACT.md`](docs/CONTRACT.md#5-exit-codes). A provider failure exits `4`;
a policy denial exits `5`.

### Example: policy

```bash
panda policy check write corpus/studies/gse160208.md   # allow, exit 0
panda policy check write README.md                     # deny, exit 5
panda policy explain shell "git push origin main"      # ask; shows matching rules
```

### Example: sessions

```bash
panda session start --label "review gse160208"
panda session list
panda session verify latest
```

A session is a JSONL file plus an anchor. Every event commits to the previous
hash; the anchor pins the tail. Edits, reordering, truncation, and coordinated
tail rewrites are detected.

## Tools

The agent can call: `workspace_info`, `read_file`, `list_files`, `grep`,
`validate`, `policy_check`, `study_list`, `study_show`, `evidence_list`,
`evidence_show`, `claim_list`, `claim_show`, `number_list`, `number_show`,
`write_file`, `edit_file`, `run_shell`, and `session_note`.

Reads resolve inside the workspace. Writes and shell commands go through policy
and, when the rule is `ask`, through an explicit approval in the cockpit or REPL.
`run_shell` takes an argv array, never a shell string. In headless `run`, an
unanswered `ask` is a denial.

## MCP

MCP is optional and not part of the runtime path. With the extra installed,
`panda mcp serve` exposes six read-only tools (`workspace_info`, `validate`,
`policy_check`, `study_list`, `study_show`, `session_note`) and two resources
(`panda://workspace`, `panda://studies`). Without the extra, `panda mcp serve`
exits `3` and everything else still works.

For a client that connects to a URL:

```bash
panda mcp serve --root <corpus-root> --transport streamable-http --port 8765
# endpoint: http://127.0.0.1:8765/mcp
```

The HTTP transport is loopback-only and has no authentication. Non-loopback hosts
are refused on purpose.

## Workspace layout

```text
<root>/
├── .panda/
│   ├── config.json
│   ├── policy.json
│   ├── instructions.md
│   ├── backups/
│   └── sessions/
├── corpus/
│   ├── validation/validate.mjs
│   └── studies/INDEX.md
└── opencode.json        # only with `init --opencode`
```

## Design choices

- **Delegate validation.** The corpus owns its checks; Panda maps the report to
  exit codes.
- **Fail closed.** Missing dependency, unanswerable approval, unsupported model
  protocol, and missing key are errors — not silent workarounds.
- **No shell.** `run_shell` takes argv arrays; Windows npm shims resolve to native
  executables instead of `cmd.exe`.
- **Preserve user config.** Legacy export merges, backs up, and keeps restrictive
  rules last.
- **Provenance before convenience.** Redaction happens before hashing; task text
  can be omitted with `--private`.
- **Boring core.** Python 3.12, Typer, Rich, Pydantic, Textual; Node only for the
  corpus validator.

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/DESIGN.md`](docs/DESIGN.md), and [`SECURITY.md`](SECURITY.md).

## Brand

The mark is [Panda Spark](assets/spark.svg), a 13×13 pixel glyph. The mascot has
eight poses (`idle`, `thinking`, `working`, `verifying`, `success`, `blocked`,
`alert`, `sleeping`) and a telemetry panel for effort, model, cache, context, and
tokens. It is rendered from one deterministic grid in
[`src/panda_cli/mascot.py`](src/panda_cli/mascot.py); the committed SVGs under
[`assets/`](assets) are generated by
[`tools/render_brand_assets.py`](tools/render_brand_assets.py).

The **Panda Spark** mark and mascot are project identifiers, not a claim of
trademark registration.

## Development

```bash
pip install -e ".[dev,mcp]"
ruff check src tests
pytest -q
```

The suite uses a fixture corpus and a real Node fake validator. It does not touch
the network or a real corpus. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT © 2026 Arthur Araújo da Silva. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

# Panda Harness

<img src="assets/icon.svg" alt="Panda" width="72" align="right">

> A fail-closed terminal harness for evidence-heavy research corpora.

**Status:** alpha (`0.1.0a1`). The v0.1 surface is frozen in
[`docs/CONTRACT.md`](docs/CONTRACT.md).

Panda is a CLI and MCP server that keeps an AI agent inside an evidence
discipline: approved write scopes, canonical validation, a verifiable provenance
ledger, and an OpenCode + OpenRouter runtime. The guarantee lives in the tool,
not in the prompt.

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

- A terminal-first harness for a corpus with a canonical validator and a study
  registry.
- A policy engine: ordered rules, last-match-wins, hard denies, `ask` treated as
  deny when nobody can answer.
- A hash-chained session ledger with a tail anchor and redaction.
- An OpenCode v2 launcher with the Panda MCP server wired in.
- A read-only study registry and validation surface for agents.

## What it is not

- Not an autonomous "AI scientist".
- Not a literature-review SaaS.
- Not a generic coding agent.
- Not an electronic lab notebook.
- Not a replacement for the corpus validator. Panda calls it.

## Requirements

| Component | Version |
|---|---|
| Python | 3.12+ |
| Node.js | 20+ |
| OpenCode | v2, `opencode` on `PATH` |
| OpenRouter | `OPENROUTER_API_KEY` or `opencode auth login` |

## Install

Panda is not on PyPI yet. Install from source:

```bash
git clone https://github.com/BlackYuriJDU/panda-harness.git
cd panda-harness
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"
```

The MCP extra is optional so the core can install on platforms where the SDK's
native dependencies have no wheel. Without it, `panda run` and `panda mcp serve`
cannot start; the rest of the CLI works.

## Quickstart

Run inside a corpus root — a directory containing
`corpus/validation/validate.mjs`:

```bash
panda init
panda doctor
panda validate
panda run "Review corpus/studies/INDEX.md and list unresolved evidence gaps"
```

`panda init` writes `.panda/config.json`, `.panda/policy.json`,
`.panda/instructions.md`, the session directory, and merges the Panda MCP server
into `opencode.json`. It backs up anything it replaces and refuses to rewrite
JSONC. Existing model and permission choices are preserved.

Start with `--dry-run` if you want to see the writes:

```bash
panda init --dry-run
```

## Commands

| Command | Purpose |
|---|---|
| `panda` / `panda status` | Workspace and last-session summary. |
| `panda init` | Initialize or update the workspace. |
| `panda doctor` | Read-only diagnostics (`--validate` to also validate). |
| `panda validate` | Run the canonical validator (`--release`, `--record`). |
| `panda run "<task>"` | Launch OpenCode with the Panda MCP server. |
| `panda session start/list/show/verify/close` | Manage the provenance ledger. |
| `panda policy check/explain/show` | Inspect and evaluate policy. |
| `panda study list/show` | Read the study registry. |
| `panda mcp serve/tools` | Run the MCP server or print its contract. |

Add `--json` for machine-readable output. Exit codes are stable and documented in
[`docs/CONTRACT.md`](docs/CONTRACT.md#5-exit-codes).

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

## MCP

`panda mcp serve` exposes exactly six read-only tools to OpenCode:

| Tool | Purpose |
|---|---|
| `panda_workspace_info` | Workspace, model, policy, and session state. |
| `panda_validate` | Run the canonical validator. |
| `panda_policy_check` | Evaluate an action/resource pair. |
| `panda_study_list` | List registered studies. |
| `panda_study_show` | Show one study card. |
| `panda_session_note` | Append a durable note to the ledger. |

Resources: `panda://workspace` and `panda://studies`.

The MCP layer never writes evidence or claims in v0.1. Notes go to the provenance
ledger only.

For a console that connects to a URL instead of launching a command:

```bash
panda mcp serve --root <corpus-root> --transport streamable-http --port 8765
# endpoint: http://127.0.0.1:8765/mcp
```

The HTTP transport is loopback-only and has no authentication. Non-loopback
hosts are refused on purpose. A remote MCP client entry looks like:

```json
{
  "mcp": {
    "servers": {
      "panda": {
        "type": "remote",
        "url": "http://127.0.0.1:8765/mcp",
        "oauth": false
      }
    }
  }
}
```

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
└── opencode.json
```

## Design choices

- **Delegate validation.** The corpus owns its checks; Panda maps the report to
  exit codes.
- **Fail closed.** Missing dependency, unanswerable approval, unsupported JSONC,
  and missing MCP wiring are errors.
- **No shell.** Commands are argv arrays; Windows npm shims resolve to native
  executables instead of `cmd.exe`.
- **Preserve user config.** Merge, back up, and keep restrictive rules last.
- **Provenance before convenience.** Redaction happens before hashing; task text
  can be omitted with `--private`.
- **Boring core.** Python 3.12, Typer, Rich, Pydantic; Node only for the corpus
  validator.

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`SECURITY.md`](SECURITY.md).

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
ruff check .
pytest -q
```

The suite uses a fixture corpus and a real Node fake validator. It does not touch
the network or a real corpus. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT © 2026 Arthur Araújo da Silva. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

# Architecture

Panda has two layers with a deliberate boundary:

1. **`panda-core`** — a Python library and MCP server. It owns policy, validation,
   provenance, workspace discovery, and the OpenCode integration. It is the only
   place where enforcement lives.
2. **`panda`** — the CLI/TUI reference client. It parses intent, renders results,
   and delegates every decision to the core.

The boundary matters: an agent calling an MCP tool gets the same policy and the
same ledger as a human running the CLI. Nothing is enforced only in the prompt.

```text
┌──────────────┐        ┌────────────────────┐
│  panda CLI   │        │  OpenCode + agent  │
│  (Typer)     │        │  (OpenRouter)      │
└──────┬───────┘        └─────────┬──────────┘
       │                          │ MCP stdio
       │                          ▼
       │               ┌────────────────────┐
       └──────────────▶│   panda-core       │
                       │ service / policy   │
                       │ validator / session│
                       │ registry / workspace│
                       └─────────┬──────────┘
                                 │ read-only
                                 ▼
                    corpus/validation/validate.mjs
                    corpus/studies/INDEX.md
                    .panda/sessions/*.jsonl
```

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Typed `.panda/config.json`, path restrictions, defaults. |
| `workspace.py` | Root discovery, path normalization, git/tool probes. |
| `policy.py` | Ordered rules, last-match-wins, hard denies, `ask` handling. |
| `validator.py` | Adapter for the corpus's canonical Node validator. |
| `registry.py` | Parse `corpus/studies/INDEX.md` and study frontmatter. |
| `session.py` | Append-only JSONL ledger, hash chain, anchor, verification. |
| `redact.py` | Remove secrets before persistence. |
| `atomic.py` | Atomic writes and file locks. |
| `process.py` | Subprocess helper, version parsing, Windows shim resolution. |
| `opencode.py` | OpenCode v2 config generation/merge, launch argv, status. |
| `init.py` | Idempotent scaffolding, backups, dry-run. |
| `doctor.py` | Read-only diagnostics. |
| `service.py` | Shared operations used by CLI and MCP. |
| `mcp_server.py` | FastMCP server, six tools, two resources, stdio transport. |
| `panda_cli/main.py` | Commands, exit codes, orchestration. |
| `panda_cli/render.py` | Rich rendering; no decisions. |
| `panda_cli/brand.py` | Panda Spark mark, palette, banner. |

## Why validation is delegated

The corpus already has a validator (`corpus/validation/validate.mjs`) that encodes
its own schema, track, evidence, number, claim, findings, license, public-face,
and secret checks. Reimplementing those checks in Python would create two sources
of truth and allow drift.

Panda therefore:

- resolves the validator path from config;
- checks the Node version (`node_min_major`);
- runs the script with a bounded timeout;
- parses the JSON report;
- maps the result to exit codes;
- never edits the corpus.

The validator's report is the authoritative result. Panda's `ValidatorReport` is a
typed view over it, including warnings when the check set is missing or extended.

## Fail-closed decisions

| Decision | Rationale |
|---|---|
| `ask` is denied to non-interactive callers | An unanswered approval must not become consent. |
| Hard denies ignore `--approve` | Secrets and destructive history operations are not delegable. |
| Paths are resolved before policy matching | Prevents `corpus/../README.md` from borrowing a corpus allow rule. |
| JSONC is refused, never rewritten | Rewriting would destroy comments and formatting the user owns. |
| Existing model/default_agent are preserved | `init` must not silently change a user's runtime. |
| Restrictive user rules are re-appended last | A merge must never weaken an explicit restriction. |
| Missing OpenCode config fails `run` | Launching without the Panda MCP server would bypass policy. |
| Missing anchor is a warning; mismatch is an error | Legacy logs stay readable; tampering is not tolerated. |
| `doctor` never creates directories | Diagnostics must not mutate the corpus. |
| MCP is optional | The core remains installable where the SDK's native deps are unavailable. |

## Session ledger

The ledger is the provenance primitive. Each event commits to the previous event:

```text
hash = sha256(canonical_json(record without "hash"))
prev = previous record.hash
```

The anchor file stores the tail `(seq, hash)`. Verification reads the whole log,
recomputes every hash, checks `prev`, checks sequence continuity, rejects blank
lines, rejects events after `session.end`, and compares the tail with the anchor.

This detects:

- editing an event (hash mismatch);
- reordering (prev/seq mismatch);
- deleting the tail (anchor seq > tail seq);
- replacing the tail and recomputing its hash (anchor hash mismatch);
- appending a forged event after close (event-after-end).

It does not make the log immutable. An attacker with write access can rewrite the
whole log **and** the anchor. The threat model is accidental corruption and
tampering by an agent that does not control both files; it is not a remote
adversary with root. For stronger guarantees, sign or ship anchors to an external
store — out of scope for v0.1.

## OpenCode integration

`panda init` writes or merges an OpenCode v2 config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "openrouter/anthropic/claude-sonnet-5.5",
  "default_agent": "panda",
  "instructions": [".panda/instructions.md"],
  "mcp": {
    "servers": {
      "panda": {
        "type": "local",
        "command": ["panda", "mcp", "serve", "--root", "<root>"],
        "cwd": "<root>",
        "environment": { "PANDA_ROOT": "<root>", "PANDA_CONFIG": "..." }
      }
    }
  },
  "permissions": [
    { "action": "read", "resource": "*", "effect": "allow" },
    { "action": "panda_validate", "resource": "*", "effect": "allow" }
  ],
  "agents": {
    "panda": {
      "mode": "primary",
      "description": "Fail-closed evidence harness agent.",
      "system": "..."
    }
  }
}
```

Permissions are explicit per Panda tool. The OpenCode schema types `action` as a
plain string, so Panda does not assume wildcard semantics; six explicit rules are
safer than one unverified pattern.

`panda run` launches:

```text
<opencode> run --model <model> --agent panda --standalone -- <task>
```

`--standalone` asks OpenCode for a private server, so concurrent Panda runs do not
share mutable state. `--shared` is the explicit opt-out.

The MCP adapter supports stdio (the default, launched by OpenCode) and a
loopback-only streamable HTTP transport for consoles that connect to a URL. The
HTTP transport is refused on non-loopback hosts and has no authentication; it is
a local convenience, not a network service.

### Windows

`opencode` from npm is a `.cmd` shim. `process.resolve_command_prefix()` reads the
shim, extracts the native `.exe` it delegates to, and returns that executable
prefix. If a shim cannot be resolved, Panda reports a dependency error instead of
routing task text through `cmd.exe`, which would reintroduce shell parsing.

## Provenance and privacy

- Session metadata records environment **names** (`PANDA_*`, `OPENROUTER_*`,
  `OPENCODE_*`), never values.
- Redaction runs before hashing and persistence.
- `--private` records only the task hash and length.
- `logging.log_tasks = false` suppresses task text globally.
- The ledger is gitignored by default.

## Testing strategy

- Unit tests for policy, sessions, registry, config, workspace, and the validator
  adapter.
- Schema tests validate every schema and fixture with `jsonschema`.
- CLI tests use Typer's `CliRunner` and assert exit codes and JSON shapes.
- MCP tests run against a real FastMCP server in an environment where the optional
  extra is installed; otherwise they skip.
- A fixture corpus with a real Node fake validator exercises the subprocess path,
  including failure and timeout behavior.

The suite does not require network access and does not modify the real corpus.

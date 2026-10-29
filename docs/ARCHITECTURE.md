# Architecture

Panda has two layers with a deliberate boundary:

1. **`panda-core`** — a Python library. It owns the provider client, the agent
   loop, the tool registry, policy, validation, provenance, and workspace
   discovery. It is the only place where enforcement lives.
2. **`panda`** — the CLI/TUI reference client. It parses intent, renders events,
   answers approvals, and delegates every decision to the core.

The boundary matters: a tool call from the cockpit, the REPL, headless `run`, or
the optional MCP server reaches the same policy engine and the same ledger.
Nothing is enforced only in the prompt.

```text
┌────────────┐  ┌────────────┐  ┌──────────────┐
│ cockpit    │  │ REPL       │  │ panda run    │
│ (Textual)  │  │ (--plain)  │  │ (headless)   │
└─────┬──────┘  └─────┬──────┘  └──────┬───────┘
      │               │                │
      └───────────────┴────────────────┘
                      │
                ┌─────▼──────────┐
                │ ChatRuntime    │  one workspace, one provider,
                │ runtime.py     │  one policy, one session
                └───┬────────┬───┘
                    │        │
          ┌─────────▼──┐  ┌──▼─────────────┐
          │ Agent      │  │ ToolRegistry   │
          │ agent.py   │◀─┤ tools.py       │
          └─────┬──────┘  └──┬───────┬─────┘
                │            │       │
      ┌─────────▼─────┐      │       │
      │ Provider      │      │       │
      │ provider.py   │      │       │
      └─────────┬─────┘      │       │
                │            │       │
                ▼            ▼       ▼
     OpenCode Go        PolicyEngine  SessionLog
     /openai /anthropic policy.py    session.py
     v1 (streaming)           │            │
                              ▼            ▼
                    corpus/validation  .panda/sessions
                    corpus/studies     *.jsonl + anchor
```

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Typed `.panda/config.json` (schema 2), provider settings, path restrictions. |
| `workspace.py` | Root discovery, path normalization, git/tool probes. |
| `provider.py` | OpenCode Go client: catalogue, protocol classification, streaming adapters for OpenAI chat, Anthropic Messages, Responses, and Google generateContent, usage. |
| `agent.py` | System prompt, turn loop, tool dispatch, context trimming, approvals, usage accounting. |
| `tools.py` | JSON-schema tool specs, path resolution, read/write/shell/validate/study/evidence/claim/number/session tools. |
| `policy.py` | Ordered rules, last-match-wins, hard denies, `ask` handling. |
| `validator.py` | Adapter for the corpus's canonical Node validator. |
| `registry.py` | Parse `corpus/studies/INDEX.md` and study frontmatter. |
| `session.py` | Append-only JSONL ledger, hash chain, anchor, verification. |
| `redact.py` | Remove secrets before persistence. |
| `atomic.py` | Atomic writes and file locks. |
| `process.py` | Subprocess helper, version parsing, Windows shim resolution. |
| `opencode.py` | **Legacy** OpenCode config generation/merge and launcher argv. Opt-in only. |
| `init.py` | Idempotent scaffolding, backups, dry-run, legacy export switch. |
| `doctor.py` | Read-only diagnostics. |
| `service.py` | Shared operations used by CLI and MCP. |
| `mcp_server.py` | Optional FastMCP server: six tools, two resources, stdio or loopback HTTP. |
| `panda_cli/main.py` | Commands, exit codes, orchestration. |
| `panda_cli/runtime.py` | `ChatRuntime`: builds provider/policy/tools/session/agent for every UI. |
| `panda_cli/repl.py` | Line REPL (`panda chat --plain`). |
| `panda_cli/cockpit.py` | Textual cockpit: layout, streaming transcript, tool cards, approvals, mascot. |
| `panda_cli/mascot.py` | Deterministic 28×24 mascot grid, poses, animation frames, telemetry strip. |
| `panda_cli/render.py` | Rich rendering; no decisions. |
| `panda_cli/brand.py` | Panda Spark mark, palette, banner. |

## Provider routing

The provider implements the four wire protocols OpenCode Go exposes under one key
and nothing else. All four normalize into the same event stream, so the agent
loop never branches on protocol.

```text
POST {base_url}/openai/v1/chat/completions                 (stream, OpenAI chat)
POST {base_url}/anthropic/v1/messages                      (stream, Anthropic Messages)
POST {base_url}/openai/v1/responses                        (stream, Responses)
POST {base_url}/google/v1beta/models/<id>:streamGenerateContent?alt=sse
                                                           (stream, Google)
GET  {base_url}/v1/models                                  (catalogue, 38 models)
```

Every request sends `User-Agent: panda-harness/<version>` and a stable
`x-opencode-session` id; Go answers `400 MissingSessionID` without them. The
provider owns a UUID per conversation, and `stream(..., session_id=...)` lets the
agent pass the Panda session id so the upstream trace matches the ledger.

`classify_protocol()` maps a model id to its wire protocol: `claude-*` ->
Anthropic Messages, `gpt-*`/`o1*`/`o3*`/`grok-*`/`muse-*` -> Responses,
`gemini-*` -> Google generateContent, everything else -> OpenAI chat. All four
protocols are implemented, so every id in the captured catalogue is callable.
An unknown protocol still fails closed (exit `2` when selected) instead of being
sent through the wrong adapter. There is no silent model fallback and no
provider auto-switch.

The Anthropic adapter converts the OpenAI-shaped history the agent owns: system
messages become the `system` prompt, assistant tool calls become `tool_use`
blocks, tool results become `tool_result` blocks grouped into a user message, and
adjacent same-role messages merge so roles alternate. Text deltas,
`input_json_delta` tool arguments, stop reasons, and usage normalize into the
same `TextDelta`/`ToolCall`/`Usage` shape as OpenAI chat; cost comes from
`usage.opencode.list_cost` when present.

The Responses adapter sends `input` items instead of messages: system text
becomes `instructions`, assistant tool calls become `function_call` items, and
tool results become `function_call_output` items. It consumes
`response.output_text.delta`, `response.function_call_arguments.delta`, and the
terminal `response.completed`/`response.incomplete` frames, so partial argument
JSON streams like OpenAI chat. Usage maps from `input_tokens`/`output_tokens`
plus `input_tokens_details`.

The Google adapter sends `contents` with `systemInstruction`, and converts tools
into `functionDeclarations` after stripping schema keys Gemini rejects
(`additionalProperties`, `$schema`, `default`, …) while preserving caller-chosen
property names — a tool argument called `pattern` must not disappear. Gemini
requires the `thoughtSignature` returned with a `functionCall` to be echoed on
the next turn; the adapter stores it in `ToolCall.metadata`, the agent persists
it as `provider_meta` on the assistant message, and the converter restores it.
It is stripped before any other protocol sees the history. Gemini often reports
`finishReason: STOP` alongside function calls, so the adapter maps that to
`tool_calls` when calls are present.

The key is read from the environment by name (`OPENCODE_API_KEY` by default) and
only used in the `Authorization` header. Error messages name the variable, never
its value.

## Agent loop

One turn is:

1. append `user.message` to the ledger (subject to `--private`);
2. build the system prompt from the corpus rules, validator state, provider,
   model, and active study;
3. stream a completion; emit `text_delta` events to the UI;
4. if the model returns tool calls, run each through `ToolRegistry` (which asks
   `PolicyEngine` first), append `tool.call` / `tool.result`, and feed the result
   back;
5. repeat until the model answers without tool calls or `max_steps` is reached;
6. append `assistant.message` and return a `TurnResult` with usage and context
   estimate.

Context is trimmed by character budget derived from `provider.context_window`,
never by dropping the system prompt. `max_steps` bounds a turn, so a model that
keeps calling tools cannot run forever.

Tool results are data. A file, log, or web page that contains instructions is not
an order; the system prompt says so and the tool layer enforces scopes anyway.

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
The agent is told to run `validate` after changing corpus content and to treat a
failing check as a stop condition.

## Fail-closed decisions

| Decision | Rationale |
|---|---|
| `ask` is denied to non-interactive callers | An unanswered approval must not become consent. |
| Hard denies ignore `--approve` | Secrets and destructive history operations are not delegable. |
| Paths are resolved before policy matching | Prevents `corpus/../README.md` from borrowing a corpus allow rule. |
| Unsupported protocols are refused | Calling a model with the wrong adapter corrupts the transcript and hides errors. |
| A missing API key is a hard failure for a call, a warning for `init` | Diagnostics must not block scaffolding; the call must not pretend to work. |
| `run_shell` takes argv, never a shell string | Removes shell parsing as an injection surface. |
| JSONC is refused in legacy mode, never rewritten | Rewriting would destroy comments and formatting the user owns. |
| Existing model/default_agent are preserved by legacy export | `init` must not silently change a user's runtime. |
| Restrictive user rules are re-appended last | A merge must never weaken an explicit restriction. |
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
store — out of scope for v0.2.

## Legacy OpenCode export

`opencode.py` still exists because some users want to drive the Panda MCP surface
from an OpenCode client. It is not imported by the runtime and is only reachable
through `panda init --opencode` and `panda mcp serve`. `panda doctor` reports a
present `opencode.json` as an informational warning: "present but unused by the
Panda runtime".

The export writes a config shaped like this (permissions and the agent block
are added from `build_permissions()` and the generated agent entry):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": [".panda/instructions.md"],
  "mcp": {
    "servers": {
      "panda": {
        "type": "local",
        "command": ["panda", "mcp", "serve", "--root", "<root>"],
        "cwd": "<root>"
      }
    }
  }
}
```

Permissions are explicit per Panda tool. Existing `model`, `default_agent`, and
restrictive permission rules are preserved; restrictive rules are re-appended
after Panda's rules so a merge cannot weaken them.

## Windows

The validator and `run_shell` spawn argv arrays. npm `.cmd` shims are resolved to
the native executable before spawning, because `CreateProcess` cannot execute a
`.cmd` directly. If a shim cannot be resolved, Panda reports a dependency error
instead of routing command text through `cmd.exe`, which would reintroduce shell
parsing. Console scripts emit UTF-8 on legacy code pages, and the cockpit uses
the same rendering path when the terminal supports it.

## Provenance and privacy

- Session metadata records environment **names** (`PANDA_*`, `OPENCODE_*`),
  never values.
- Redaction runs before hashing and persistence.
- `--private` records only the task hash and length.
- `logging.log_tasks = false` suppresses task text globally.
- The ledger is gitignored by default.
- Tool arguments and results are truncated before being written to the ledger
  (`agent.py` caps strings and list lengths), so a large read cannot bloat the
  chain.

## Cockpit as a renderer

`cockpit.py` owns no policy. It submits a message to `ChatRuntime`, renders
events, and answers approval requests. Input is never blocked by a running turn;
the composer stays available while the model streams, and a second submit is
queued behind the active turn. The mascot is a state machine driven by runtime
events (`idle`, `thinking`, `working`, `verifying`, `success`, `blocked`,
`alert`, `sleeping`), animated at a fixed timestep with a reduced-motion gate.
Visual rules live in [`DESIGN.md`](DESIGN.md).

## Testing strategy

- Unit tests for policy, sessions, registry, config, workspace, and the validator
  adapter.
- Provider tests use a mock transport: streaming deltas, tool-call assembly,
  auth failure, rate limit, and no-secret-in-error.
- Agent tests use a scripted provider: tool loops, step limits, approvals,
  context trimming, and ledger events.
- Tool tests cover path traversal, secret refusal, write scopes, shell argv, and
  the read-only contract.
- Schema tests validate every schema and fixture with `jsonschema`.
- CLI tests use Typer's `CliRunner` and assert exit codes and JSON shapes.
- Cockpit tests drive the Textual app in headless mode.
- MCP tests run against a real FastMCP server where the optional extra is
  installed; otherwise they skip.
- A fixture corpus with a real Node fake validator exercises the subprocess path,
  including failure and timeout behavior.

The suite does not require network access and does not modify the real corpus.

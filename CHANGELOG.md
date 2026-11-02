# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 0.2.0a5 hardening

### Security

- Validator routes (SEC-002) and MCP policy (MCP-001, MCP-002): the `validate` tool
  and the MCP `validate` and `study_show` ran with no policy check, so a rewritten
  validator was executable without approval through three routes. `validate` is now
  authorized under a `validate` action (baseline allow beneath any workspace policy,
  and the validator itself is `hard_deny` for writes) and records
  `validator_sha256`; the MCP service reuses `ToolRegistry`, so `ask` is denied and a
  denial is written to the ledger. The `tool` rules now name the tools FastMCP
  actually registers.
- Shell policy by effect (SEC-001, SEC-007, shell route of SEC-002): commands are
  classified from argv tokens (`panda_core.shell_policy`) after the glob rules, and
  can only escalate. `git show HEAD:.env`, `git diff --output=README.md`,
  `git diff --no-index`, `git -C . reset --hard`, `git -c ... push --force`,
  `git push origin +main`, `git branch -D`, `rm -r -f`, `rm -Rf` and
  `find -delete` are now refused, and `git diff -- .env` is refused by the `read`
  rules. `git push origin fix-foo` is `ask` instead of a false-positive
  `hard_deny`. `node corpus/validation/validate.mjs` lost its pre-approval.
- Study cards and claims (SEC-010, SEC-011): `study_show` followed whatever link
  `corpus/studies/INDEX.md` held, which the agent can rewrite, and `claim_*`
  followed symlinks out of the workspace. An INDEX link that is absolute or
  contains `..` is now a configuration error, the card path goes through the
  `read` policy, and symlinked cards and claims are refused or skipped.
- Search tools (SEC-003): `grep` and `list_files` authorized only the workspace
  root, so they returned the contents and names of `.env`, `*.pem` and other files
  that `read_file` refuses. Each hit is now checked with the same `read` rule, under
  the file's own name and its symlink target; refused files are omitted and counted
  in `omitted_by_policy`. A file that would need approval is omitted, never asked
  about per file.
- Credential files (SEC-005): `.git/config`, `.netrc`, `.npmrc`, `.pypirc`,
  `.git-credentials`, `credentials.json`, `.aws/credentials`, `.docker/config.json`,
  `*.p12`, `*.pfx`, `*.kdbx`, `*.keystore`, `*.jks` and `secrets.*` are `hard_deny`
  for reads, at the root as well as nested.
- Policy effects (POL-001): an unknown `effect` such as `"hard-deny"` is now a
  configuration error instead of silently becoming an approvable `ask`.
- Workspace trust boundary (TRUST-001): `.panda/config.json` can no longer choose
  where the provider key is sent. `provider.base_url` must be `https` on
  `opencode.ai` and `provider.api_key_env` must be `OPENCODE_API_KEY`; any other
  value is a configuration error (exit `2`) raised before any request. This
  closes the key-exfiltration route for `panda run`, `panda chat` and
  `panda models` when opening a corpus received from a third party.

- Symlinks in workspace files (TRUST-005): `.panda/instructions.md`,
  `.panda/config.json`, `.panda/policy.json` and the validator script are read
  through a single helper (`panda_core.safe_read`) that refuses a symbolic link
  anywhere below the root and opens with `O_NOFOLLOW`. A link such as
  `.panda/instructions.md -> ~/.ssh/id_ed25519` no longer puts a local file in
  the system prompt, and a linked validator is not executed. Instructions are now
  redacted before they reach the prompt.

- `panda status` no longer executes the corpus-chosen `node` (TRUST-002).
  `config.node` must be a plain command name resolved through `PATH`, and
  `status` plus the session metadata do not probe the binary at all; the node
  version is reported from the user-scoped cache or as `null`. `doctor`,
  `validate` and the validator tools are where node runs.

- Policy floor (TRUST-003): a workspace `policy.json` with `default: "allow"` is
  refused, and the engine always applies a built-in floor of `hard_deny` rules
  (secret reads, force-push and destructive shell, release publishing, and
  writes under `.panda/` and `corpus/validation/`). A hostile policy that allows
  everything can no longer let the agent rewrite the policy, the ledger or the
  validator. Writes to `corpus/validation/**` are now `hard_deny` for the agent.

- `panda doctor` records the Node.js version it measured in the user-scoped cache,
  so `panda status` can report it without running the binary.

### Changed

- **Breaking for custom endpoints:** a workspace file with a non-official
  `provider.base_url` or `provider.api_key_env` is now rejected. `panda init
  --force` resets it to the defaults.
- **Breaking for policy authors:** `default: "allow"` is rejected, and the agent
  can no longer write `corpus/validation/**` or `.panda/**` even if a rule allows
  it; edit those files yourself.
- **Breaking for a path in `node`:** put the binary on `PATH` instead of naming
  its path in `.panda/config.json`.
- **Breaking for symlinked workspace files:** copy the target into the
  workspace instead of linking it.

## [0.2.0a4] — 2026-10-30

### Fixed

- POSIX CI: `mypy` on Linux type-checked the Windows-only `msvcrt` lock branch
  because it was guarded by `os.name`. Both Ubuntu jobs failed on
  `msvcrt.locking`/`LK_NBLCK`/`LK_UNLCK`. The guard is now
  `sys.platform == "win32"`, which mypy narrows per platform; runtime behaviour
  is unchanged.

### Verified

- `mypy --platform linux` and `mypy --platform win32` are clean, `ruff` is
  clean, and the full suite passes on Windows (340 passed, 4 skipped).

## [0.2.0a3] — 2026-10-29

Every model in the captured Go catalogue is callable. The agent loop stays
protocol-agnostic: all four adapters normalize into the same events.

### Added

- OpenAI Responses adapter (`POST /openai/v1/responses`) for GPT/Grok/Muse:
  `instructions` from system messages, `function_call`/`function_call_output`
  input items, streamed text and argument deltas, `response.completed` and
  `response.incomplete` handling, and usage from `input_tokens`/`output_tokens`
  plus cache details.
- Google generateContent adapter
  (`POST /google/v1beta/models/<id>:streamGenerateContent?alt=sse`) for Gemini:
  `systemInstruction`, cleaned `functionDeclarations`, `functionCall` and
  `functionResponse` history conversion, streamed text, finish-reason mapping,
  and `usageMetadata` normalization.
- Gemini thought signatures: the `thoughtSignature` of a function call is kept
  in `ToolCall.metadata`, persisted as `provider_meta` on the assistant message,
  and echoed on the next turn. It is stripped before any other protocol sees the
  history.
- Tests for both adapters: URL/payload shaping, text and tool streams, usage
  parsers, history conversion, signature round-trip, fail-closed errors, and
  schema-property preservation.

### Changed

- `SUPPORTED_PROTOCOLS` now covers `openai-chat`, `anthropic`, `responses`, and
  `google`; `panda models`, `panda doctor`, the cockpit, and model switching stop
  warning that the latter two are unsupported.
- `panda run --dry-run` accepts every catalogued model instead of rejecting
  Responses/Google ids.

### Fixed

- The Google schema cleaner no longer drops a tool argument whose *name* is
  `pattern` (or `default`); property names are data, only schema keywords are
  removed. This caused a real `400 required fields ['pattern']` failure for
  `grep`.
- Gemini reporting `finishReason: STOP` alongside function calls is mapped to
  `tool_calls`, so the agent executes them.

### Verified

- Real two-turn tool calls: GPT-5.6, GPT-6, Grok-4.7, and Gemini-4 returned a
  tool call and then an answer; Muse-1.3 returned a tool call on both turns.
- Real agent loops: Gemini (2 steps, 1 tool call, cost `0.009864`) and GPT-6
  (2 steps, 1 tool call, cost `0.00027337`), exit 0.
- `ruff`, `compileall`, and `mypy` clean; full suite green on native and x64
  virtualenvs.

## [0.2.0a2] — 2026-10-28

The autonomous runtime pivot. OpenCode is no longer in the runtime path; the
provider is OpenCode Go, with OpenAI-chat and Anthropic Messages adapters.

### Added

- Direct OpenCode Go provider (`provider.py`): OpenAI-compatible streaming chat
  completions, model catalogue, protocol classification, usage accounting, and
  no-secret-in-error guarantees.
- Anthropic Messages adapter for Claude (`POST /anthropic/v1/messages`): system
  prompt, `tool_use`/`tool_result` conversion, SSE text/JSON deltas, stop
  reasons, and usage normalized to the OpenAI-chat event shape. `claude-haiku-5-5`
  is callable on Go.
- Go request identity: every call sends `User-Agent: panda-harness/<version>` and
  a stable `x-opencode-session` id (the Panda session id when available), which
  Go requires to avoid `400 MissingSessionID`.
- Agent loop (`agent.py`): system prompt built from corpus rules, tool dispatch,
  step limit, context trimming, approval events, and per-turn usage.
- Tool registry (`tools.py`): `workspace_info`, `read_file`, `list_files`,
  `grep`, `validate`, `policy_check`, `study_list`, `study_show`,
  `evidence_list`, `evidence_show`, `claim_list`, `claim_show`, `number_list`,
  `number_show`, `write_file`, `edit_file`, `run_shell`, `session_note`.
- `ChatRuntime` (`panda_cli/runtime.py`) shared by the cockpit, the REPL, and
  headless `run`.
- Textual cockpit (`panda_cli/cockpit.py`): responsive layout, streaming
  transcript, tool cards, approval modal, mascot, telemetry sidebar.
- Line REPL (`panda_cli/repl.py`) via `panda chat --plain`.
- `panda chat`, `panda models`, and a rewritten `panda run` with `--json`,
  `--dry-run`, `--max-steps`, and `--timeout`.
- Offline model snapshot (`model_catalog.py`, 38 Go models) with `--refresh` to
  require the live catalogue.
- `docs/DESIGN.md`: visual system, cockpit layout, mascot state machine,
  animation gates, telemetry semantics, and approval UX.

### Changed

- `panda init` no longer writes `opencode.json`; the legacy export is opt-in via
  `--opencode`.
- Default provider is `opencode-go` at `https://opencode.ai/inference/go`; the
  default model is `opencode-go/deepseek-v4.1-flash` and the runtime
  authenticates with `OPENCODE_API_KEY`. The Console/Zen base
  (`https://opencode.ai/inference`) is no longer the default: it answers `402
  Insufficient funds` for Go keys.
- The legacy `opencode/<id>` model prefix is accepted as an alias for
  `opencode-go/<id>` when the base URL is Go.
- Config schema is v2 (v1 files still load). Provider settings replace the
  OpenRouter/OpenCode runtime assumptions.
- `panda doctor` reports the provider key and model protocol; a present
  `opencode.json` is informational only.
- `panda models` marks a model `callable` for a supported protocol and only
  `ready` when the key is present; the JSON adds `callable`, `ready`,
  `api_key_env`, and `api_key_present`.
- Exit code `4` now explicitly covers provider errors (auth, rate limit,
  transport, protocol).

### Removed

- OpenCode CLI and OpenRouter as runtime dependencies. `opencode.py` remains as
  an opt-in export and is not imported by the runtime.

### Fixed

- Supported-protocol models are no longer advertised as `ready` without an API
  key.
- Unsupported protocols (`gpt-*`/`grok-*`/`muse-*` = Responses, `gemini-*` =
  Google) fail with a clear configuration error instead of a wrong-adapter
  request.

### Verified

- `ruff check src tests` passes.
- `pytest` passes with and without the MCP extra; the suite does not touch the
  network or a real corpus.

## [0.1.0a1] — 2026-10-25

Initial alpha. The contract is frozen in [`docs/CONTRACT.md`](docs/CONTRACT.md).

### Added

- `panda-core` library with typed errors and stable exit codes.
- Workspace discovery from `.panda/config.json` or
  `corpus/validation/validate.mjs`.
- Idempotent `panda init` with backups, dry-run, JSONC refusal, and OpenCode v2
  config merge that preserves the user's model, instructions, and restrictions.
- Fail-closed policy engine with ordered rules, last-match-wins, hard denies for
  secrets and destructive git/shell operations, and non-interactive `ask` denial.
- Adapter for the corpus's canonical Node validator, with Node version checks,
  bounded timeouts, and structured reports.
- Append-only session ledger: SHA-256 hash chain, tail anchor, redaction before
  hashing, truncation and tail-rewrite detection.
- Study registry parser for `corpus/studies/INDEX.md` and study frontmatter.
- CLI: `init`, `doctor`, `validate`, `run`, `status`, `session`, `policy`,
  `study`, `mcp`, `version`, `brand`.
- OpenCode v2 launcher: private server by default, OpenRouter model selection,
  six explicit Panda tool permissions, provenance around each run.
- MCP stdio server with six tools and two resources.
- Loopback-only `streamable-http` MCP transport (`--transport
  streamable-http`), so a console can connect to `http://127.0.0.1:8765/mcp`
  without launching a stdio process.
- Four Draft 2020-12 JSON Schemas with valid/invalid fixtures.
- Read-only `panda doctor` with Python, Node, OpenCode, validator, policy,
  session, and writability checks.
- Test suite covering policy, sessions, registry, config, init, OpenCode config,
  workspace discovery, schemas, CLI contracts, service layer, and MCP.
- Brand palette, Panda Spark mark, and a deterministic 8-pose mascot with an
  effort/model/cache/context/tokens telemetry strip.
- Reproducible SVG assets (icon, spark, head, splash, eight mascot poses) generated
  from the terminal grids.

### Fixed

- Windows console scripts emit UTF-8 and no longer crash on legacy code pages.
- npm `.cmd` shims are resolved to native executables before spawning.
- Policy traversal bypass (`corpus/../README.md`) normalized before matching.
- `doctor` no longer creates `.panda/sessions` in a read-only corpus.
- Permission deduplication keeps the last occurrence, so restrictive user rules
  remain effective after a merge.
- Workspace path validation rejects Windows drive and UNC paths on every
  platform and normalizes both path separators before matching.
- Quoted Windows commands with spaces (for example an absolute `panda.exe` path)
  are parsed without leaving quote characters in the executable name.

### Verified

- `panda validate --root "DCJ - Lito"` reports 10/10 checks, exit 0, with no
  writes to the corpus.
- `panda init --root "DCJ - Lito" --dry-run` reports no writes.
- `ruff check .` passes.
- `pytest` passes (207 passed, 4 skipped without the MCP extra; 211 passed with
  the extra installed).

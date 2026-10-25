# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — target 0.2.0a1

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

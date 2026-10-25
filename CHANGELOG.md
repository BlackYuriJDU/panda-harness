# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

### Verified

- `panda validate --root "DCJ - Lito"` reports 10/10 checks, exit 0, with no
  writes to the corpus.
- `panda init --root "DCJ - Lito" --dry-run` reports no writes.
- `ruff check .` passes.
- `pytest` passes (200 passed, 3 skipped without the MCP extra; MCP tests pass in
  an environment with the extra installed).

# Contributing

Panda is an alpha project with a narrow contract. Contributions that strengthen
the existing scope are more likely to land than contributions that expand it.

Read [`docs/CONTRACT.md`](docs/CONTRACT.md) before changing behavior. Exit codes,
MCP tool names, schema IDs, and file names are public interface.

## Setup

Requirements:

- Python 3.12+
- Node.js 20+ (the test suite runs a real fake validator)
- Git

```bash
git clone https://github.com/BlackYuriJDU/panda-harness.git
cd panda-harness
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp]"
```

On platforms where the MCP SDK cannot install (for example, Windows ARM64 without
a `cryptography` wheel), install without the extra and run the non-MCP tests:

```bash
pip install -e ".[dev]"
pytest
```

The MCP tests skip automatically when `mcp` is unavailable.

## Checks

```bash
ruff check .
pytest -q
```

Both must pass. If you change a schema, update its fixtures. If you change the CLI
surface, update `docs/CONTRACT.md` and `README.md`.

## Rules for changes

- **Smallest change that solves the problem.** No drive-by refactors.
- **Fail closed.** An unanswered approval is a denial. A missing dependency is an
  error, not a fallback that silently skips enforcement.
- **One source of truth.** Do not reimplement corpus validation in Python; call
  the corpus validator.
- **No shell.** Spawn argv arrays. On Windows, resolve npm shims instead of
  routing through `cmd.exe`.
- **No secrets in logs.** Redaction happens before persistence; keep it that way.
- **No writes to a real corpus in tests.** Use `tmp_path` and the fixture corpus.
- **Preserve user config.** Never rewrite `opencode.jsonc`; merge JSON configs and
  back up before replacing.
- **Do not weaken a restriction.** Restrictive permission rules stay last.

## Pull requests

Include:

1. What problem this solves and why now.
2. The smallest reproduction or acceptance test.
3. What you verified (commands and output, not adjectives).
4. Risk and rollback, if the change touches policy, sessions, or OpenCode config.
5. Contract/doc updates when the public surface changes.

Security-sensitive changes (policy, redaction, session verification, subprocess
execution, MCP) get an independent review before merge. A builder does not
self-approve a security change.

## Commit style

Conventional-ish, imperative, scoped:

```text
feat(cli): add session verify --json
fix(policy): normalize traversal before matching
docs(contract): freeze v0.1 exit codes
test(session): cover coordinated tail rewrite
```

## Reporting bugs

Use the issue template. Include the exact command, expected behavior, observed
behavior, exit code, and `panda doctor --json` output with secrets removed. For
security issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public
issue.

## License

By contributing, you agree that your contribution is licensed under the MIT
License, the same terms as the project.

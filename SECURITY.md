# Security Policy

Panda is an alpha project. Security reports are welcome and taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` (alpha) | Best effort |

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting:

<https://github.com/BlackYuriJDU/panda-harness/security/advisories/new>

Do **not** open a public issue for a vulnerability that could expose secrets,
allow policy bypass, or enable code execution. Include:

- affected version and platform;
- a minimal reproduction;
- the expected and observed behavior;
- impact (what an attacker could read, write, or execute).

You will get an acknowledgement as soon as possible. This is a volunteer alpha
project, so no fixed SLA is promised. Please allow time for a fix before public
disclosure.

## Threat model

Panda is a local developer tool. It assumes:

- the user controls the machine and the workspace;
- an agent may be untrusted or may hallucinate;
- the corpus is valuable and may contain restricted material;
- the provenance ledger is local.

Panda does **not** protect against:

- an attacker with root or equivalent access to the machine;
- an attacker who can rewrite both a session log and its anchor;
- malicious code in the corpus validator, OpenCode, OpenRouter, or any dependency;
- prompt injection that convinces a user to approve a destructive action;
- exfiltration through a tool that is not managed by Panda.

## Controls in v0.1

### Policy

- Ordered rules, last match wins.
- `hard_deny` cannot be overridden by `--approve`.
- `ask` is denied to non-interactive callers.
- Path resources are resolved before matching, so `..` cannot borrow a scope.
- Default policy denies writes outside `corpus/`, `memory/`, and release metadata
  requires approval; secrets are hard-denied; force-push and recursive deletion
  are hard-denied.

### Provenance

- Append-only JSONL with a SHA-256 hash chain and tail anchor.
- Redaction before hashing and persistence.
- Environment variable **names** are recorded, never values.
- `--private` avoids storing task text; `logging.log_tasks = false` disables it
  globally.
- `.panda/sessions/` and `.panda/backups/` are gitignored by default.

### Secrets

- Panda never prints `.env` contents, tokens, or API keys.
- Redaction covers common key names and token patterns.
- `panda doctor` checks presence, not values.
- The policy hard-denies reads of `.env`, `*.env.*`, `*.pem`, `*.key`, and `.ssh/`.

### Subprocess execution

- Commands are spawned without a shell.
- On Windows, npm `.cmd` shims are resolved to their native executable; task text
  is never routed through `cmd.exe`.
- The validator is executed with a bounded timeout and a checked Node version.
- `panda run` requires the Panda MCP server to be present in the OpenCode config.

## Known limitations

- The session anchor is stored beside the log. A local attacker who can rewrite
  both is not detected. External anchoring is future work.
- Policy applies to Panda tools and to the OpenCode permission envelope. It
  cannot constrain arbitrary tools that the user configures outside Panda.
- OpenCode and OpenRouter are external runtimes. Their security posture, data
  handling, and model behavior are outside Panda's control.
- `panda run` propagates OpenCode's exit code; a non-zero code is not proof that
  no side effect occurred.
- The alpha CLI emits human-readable errors on stderr even with `--json`; scripts
  must rely on documented exit codes.

## Safe defaults for contributors

- Never commit `.env`, credentials, session logs, or backups.
- Do not use real restricted data in tests; use the fixture corpus.
- Do not add network calls to the test suite.
- Treat a policy change as a security change: add a regression test for both the
  allow and the deny path.

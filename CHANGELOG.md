# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 0.2.0a6 (hardening, round 2)

Follows the round-2 audit of `0.2.0a5` (99 findings, 25 root causes). Fixes close root
causes, not single routes, and each bypass becomes a permanent adversarial case.

### Security

- Boot probe (R2-106, P0): opening `panda chat`/`panda run` in a corpus delivered with
  its `.git` directory ran the corpus's clean filter and `post-index-change` hook,
  with the whole environment (including `OPENCODE_API_KEY`); the round-1 fix only
  neutralised `core.fsmonitor`. `Workspace.git_state()` now starts no process: it
  reads `.git/HEAD`, the branch ref and `packed-refs` as plain files (no symlinks,
  bounded size, strict ref names). A `.git` file (worktree, submodule) is not followed.
- `--auto` (R2-500 hotfix): the never-auto list matched rule ids, so `env git push`,
  `git -C . push`, `/usr/bin/git push` and any push under a corpus policy were
  auto-approved, as were `twine upload` and `gh release`. Until shell effects are
  classified, `--auto` approves no shell command; edits inside write scopes are
  still approved.

- Read-only git (R2-101, R2-201, R2-102): `git status`/`diff`/`show`/`log`, which need
  no approval, ran the corpus's `core.fsmonitor`, `diff.external` and textconv drivers,
  including from a nested `corpus/sub/.git/config` the agent could write, and printed a
  committed `.env`. Allowed git now runs through `panda_core.gitsafe` (explicit git dir,
  empty attribute source, program-naming keys overridden, filters from
  `.git/info/attributes` neutralised by name, no inherited `GIT_*`, exclude pathspecs
  for read-denied files); what Panda cannot isolate needs an approval. `--attr-source`
  alone does not cover `.git/info/attributes`: verified on git 2.43 and pinned by a case.
- Writing into any `.git` directory is now a floor `hard_deny` (R2-201).
- Workspace policy (R2-113, R2-207, R2-116): an `allow` rule in `.panda/policy.json`
  opened arbitrary shell and writes, and a workspace rule could relabel a push as risk
  `low`, "pre-approved". Unless the user trusts the file, decisions are now the stricter
  of the workspace and built-in policies; trust is bound to the file's SHA-256 and kept
  outside the workspace. Rule files are validated strictly (lists of strings, risk enum,
  no unknown fields).
- Shell policy (R2-100, R2-104, R2-103, R2-202, R2-203, R2-317): `allow` globs matched
  joined text, so `git status/../../usr/bin/touch` and `panda validate --root
  corpus/x` ran without approval, and wrappers or spellings lowered `hard_deny` to
  `ask` (`timeout`, `stdbuf`, `sudo -u`, `env -u/-C`, `rm --r`, `push --prune`,
  `git --glob-pathspecs clean -fdx`, `git.exe.`, `GIT~1.EXE`). Shell `allow` now comes
  only from the argv classifier (plain `git` read-only forms) or a trusted rule
  matched by tokens; the classifier unwraps wrappers by option arity, splits `sh -c`
  and `env -S` strings, and escalates an unknown wrapper around git/rm/find. The
  decision keeps the classifier's rule and risk on a tie, which is the root cause of
  the `env git push` auto-approval (R2-500).
- `--auto` by effect class (R2-500, R2-204, R2-501, DRIFT-501): the never-auto list matched
  rule ids, so any push not spelled `git push ...` and every other publishing command were
  approved. The argv classifier now gives each command an effect class carried through the
  decision and the approval request; `--auto` approves only edits, `validate` and
  `exec_local` checks. `NEVER_AUTO_RULES` is gone and the `--auto` help says what it approves.
- Approval provenance (R2-405, R2-406): `approval.decision` records `decided_by` and
  `effect_class`; `approval_mode` is recorded at session and run start. MCP
  `policy_check(approved=True)` no longer turns an `ask` into a logged allow.
- Operator commands (R2-111): `!cmd` in the REPL and cockpit is recorded in the ledger like
  the model's tool calls and stops when the ledger cannot record it.
- Session end (R2-312): a cancelled or failed last turn is no longer recorded as
  `completed`, and a failure to write `session.end` is reported.
- Secrets toward the provider (R2-218, R2-112, R2-219, R2-220): tool results were redacted
  in the ledger but sent raw to the provider, and the redactor missed JSON keys
  (`"password": "x"`), URL passwords, `Cookie:`, `Authorization: Basic`, `pwd`/`passphrase`,
  `sk_live_`, `glpat-`, `hf_`, JWTs and encrypted/PGP/cut PEM keys. Tool output is now
  redacted before it joins the conversation and before the UI sees it; the patterns cover
  those formats; ordinary words (`Secretion:`, `Tokenization:`) are no longer redacted; the
  assignment scan is linear (a hostile 240 KB input went from 5 s to 0.5 s).
- Children's environment (R2-114): an allowlist (system, locale, toolchain, CA and proxy
  variables) replaces the denylist that kept `PGPASSWORD`, `MYSQL_PWD`, `DATABASE_URL` and
  `SSH_AUTH_SOCK`; a proxy URL with a password is dropped; the user may add names with
  `PANDA_CHILD_ENV`, never a credential-looking one.
- Credential files (R2-108): `.envrc`, `id_ecdsa`/`id_dsa`, `.kube/config`, Google ADC json,
  `.vault-token`, `*.tfstate`, `.s3cfg`, `.boto`, `.htpasswd` join the read floor.
- Workspace instructions (R2-303, R2-400, R2-415): `config.instructions` must be a `.md` file in
  `.panda/` or `corpus/` that the read policy allows; the text is placed in a
  `<workspace_instructions untrusted="true">` block that cannot be closed from inside, and
  a new rule 10 says text from tools and the workspace is data. "The corpus wins" is gone;
  the prompt names only the workspace folder and no longer mentions `--auto`.
- Previews and the redaction marker (new, from the plan review): `write_file` showed the
  current content of a read-denied file in the approval diff and the ledger; it now shows
  only its hash, and `edit_file` refuses such a file. Writing `[redacted]` into a file that
  holds a secret is refused, so redaction cannot destroy the real value.
- Terminal output (R2-221, R2-222, R2-223, R2-306, R2-310): sanitizing was done per call
  site and the cockpit transcript, tool cards, CLI tables, `print_error`, `panda models` and
  validator output were missed. Every CLI console now writes through a sink that keeps only
  colour sequences, with Rich markup off, so `[link=...]` stays literal; the cockpit builds
  every block with `SafeText`, which sanitizes on construction and on every append; model
  Markdown is sanitized and its links are never clickable (N-03); `--json` output escapes
  bidi, invisible and C1 characters. The sanitizer also covers U+2028/2029, U+061C,
  U+180E, U+00AD, Hangul fillers, variation selectors and tag characters.
- Approval card (R2-229, R2-117, R2-125): the card shows the exact argv (quoted token by
  token, with the token count), the working directory, the effect class, where the rule
  comes from (a workspace rule's reason is labelled as workspace text) and the payload's
  SHA-256, which is also written to `approval.request`. A diff is shown whole up to 200 000
  characters; a truncated preview cannot be approved in the cockpit or the REPL.
- Workspace discovery (R2-300): the search for `.panda/config.json` climbed every
  ancestor, so a `.panda/` planted in `/tmp` or `C:\` became the workspace of anyone below
  it (the git CVE-2022-24765 class). It now stops at the user's home, never adopts a
  filesystem root above the start directory, and on POSIX never climbs into a directory
  owned by another user.
- `panda init` (R2-305): it followed links shipped by the corpus, creating files outside
  the workspace and, with `--force`, overwriting a link's target and copying it into
  `.panda/backups/`. Init now refuses when any path it touches is a link.
- Ledger (R2-224, R2-225, R2-227, R2-502): a long-lived writer (cockpit, MCP) trusted a
  tail cached by file size, so a same-size rewrite of the last line went unnoticed; the
  tail is now re-read under the lock on every append and its hash recomputed. Nothing is
  appended after `session.end`. `panda session recover` refuses an unanchored approval or
  policy event and one dated before its predecessor or in the future. A new test kills
  the surviving mutant that skipped the anchor's hash comparison.
- Reads that follow links (R2-209, R2-215, R2-307, R2-308, R2-109, R2-118): workspace
  files are now opened one directory at a time with `O_NOFOLLOW` (a parent swapped for a
  link after the check is refused); the grep worker opens only the inode the parent
  checked; the cockpit crash log and lock files refuse links and other users' files;
  `corpus/studies/INDEX.md` is read through `safe_read` and `study_list` (tool and MCP)
  obeys the read policy.
- User state and config boundary (R2-301, R2-309, R2-110, R2-304, R2-302): atomic writes
  create the temporary file with its final mode (a key was briefly world-readable), a
  world-readable credential store is refused, and the OpenCode export backup is counted
  like `init`'s. A workspace file can no longer pick which stored key is sent
  (`provider.name`) or raise budgets above 1 048 576 context tokens, 65 536 output tokens,
  3 retries and 600 s. Evidence and number registries above 16 MB and claim cards above
  1 MB are refused instead of read whole.
- Tree hash and packaging (R2-226, R2-313): `tree_sha256` no longer skips
  `corpus/node_modules`, `.venv` or `__pycache__` (code the validator can load), and its
  cache keys on ctime and inode, which a writer cannot restore like mtime; on Windows, where
  `st_ctime` is the creation time, the cache is not used. `panda_cli` is
  a regular package, and the MCP command Panda writes runs `python -P -m panda_cli`, so a
  `panda_cli/` folder in a corpus cannot be imported.
- Tool arguments (R2-401): only `required` was checked, so `replace_all: "false"` replaced
  every occurrence and unknown keys were ignored. Arguments are now validated against the
  tool's JSON Schema (`jsonschema`, now a runtime dependency), unknown keys are refused, and
  the error names the field and the expected type.
- Validator closure (R2-105, R2-210): the configured validator, its directory,
  `node_modules/`, `package.json` and `.npmrc` (root and `corpus/`), and nested
  `*/.panda/*` and `*/corpus/validation/*` are floor `hard_deny` for writes.
- Windows (first CI run of the branch): non-UTF-8 child output in the ANSI code page
  was decoded as U+FFFD; it is now decoded with the system code page.

### Added

- `tests/evals/policy/`: adversarial policy cases as data, run through the real
  `ChatRuntime` -> `ToolRegistry` route with a fake approver and effect markers. Open
  holes are strict xfails, so fixing one without flipping its case fails the suite.

### Added

- `panda policy trust [--show] [--yes] [--revoke]`: read what a workspace policy would allow beyond the built-in one, then trust it explicitly (typed confirmation); the cockpit, REPL and `panda run` say how many allow rules are ignored until then.

### Changed

- **Breaking, `panda run` exit codes (R2-404):** a turn that stops at `max_steps`, a budget or
  an incomplete stream now exits `7` (it exited `1`, which the CONTRACT reserves for a
  failed validation), and a turn that completes with any blocked or denied action exits
  `5` (it exited `0`). `--json` always prints one object, including on errors, with
  `exit_code`, `blocked_calls` and an `error` object. Scripts that treated `1` as "did not
  finish" must accept `7`.
- `panda validate` through `run_shell` now needs an approval; the `validate` tool is the
  unattended route (owner approved the test change).
- CI runs the Ubuntu and Windows matrix on every branch push.
- `git_state()["dirty"]` is now `None` (unknown) everywhere: only git can compute it,
  and running git on a corpus is what R2-106 was about. `panda status`, `doctor` and
  the cockpit header show the branch and commit without a dirty marker.

## [0.2.0a5] — 2026-11-03 (hardening)

A security-hardening release that follows an independent audit of `0.2.0a4` (53 findings).
`0.2.0a4` should not be published. Nothing is added to the product surface except
`panda session recover`; most entries below make existing behavior stricter.

### Security

- Release pipeline (SUP-001): `release.yml` had `contents: write` for the whole workflow and
  built and published without running the tests (the a4 release job was green on a commit
  whose CI was red). It is now read-only by default and runs `test` -> `build` -> `publish`,
  where only `publish` has `contents: write`, only for `v*` tags, and still as a draft. CI
  gains a `pip-audit` job and `.github/dependabot.yml` proposes weekly updates for actions
  and Python dependencies. Actions are still pinned by tag, not by commit SHA.
- `--private` (PRIV-001): it hid the task but the model's prose, which often repeats it, was
  stored. The assistant's text is now stored as SHA-256 and length too. Tool arguments stay
  recorded, because they are the provenance; the CONTRACT now says exactly that.
- Legacy OpenCode export (LEG-001): the generated envelope no longer pre-approves
  `git diff`, `git log`, `git show`, `git branch` or the validator script (the same prefix
  hole as SEC-001); merging no longer lets a user's `ask` override a generated `deny`;
  `launch_opencode` no longer passes the whole environment (credentials removed); and
  `environment_status` reports `OPENCODE_API_KEY`, not the obsolete `OPENROUTER_API_KEY`.
  The functions that have no caller remain, with their tests, and are listed for removal.
- Git boot probe (TRUST-004): `git status` at startup ran whatever program `core.fsmonitor`
  in the corpus's `.git/config` named. The probe now forces `core.fsmonitor=false`.
- Locks and writes (SEC-009): the POSIX `file_lock` could block forever (now a timeout, as on
  Windows); `write_file`/`edit_file` reset a file's mode to 0644 (an executable lost its bit,
  a 0600 file became world-readable; the mode is now kept); and a file changed while waiting
  for approval was overwritten from the stale read (now refused: "the file changed while
  waiting for approval").
- Crash log (PRIV-002): the cockpit crash log wrote tracebacks unredacted with default
  permissions into a folder that was not git-ignored. It is redacted, created `0600`, and
  `logs/` is in `.panda/.gitignore`.
- Dependency floor (DEP-001): the `mcp` extra is `>=1.30,<2`; older versions in the previous
  range carry published advisories (DNS rebinding, session binding, DoS).

### Changed

- Documentation matches the code (DOC-001): event names are `message.user` /
  `message.assistant`, `panda auth` is in the command table, the CONTRACT has a reference
  of every event the code writes, `SECURITY.md` supports `0.2.x`, and the agent's rule 7
  now says what the policy does (a person approves a push; `--auto` never does).
  `tests/test_contract.py` compares the CONTRACT with the registered commands, the ledger
  events, the hard denies and the version, so the two cannot drift again.
- Dead policy actions (DEAD-001) are now explicit: `ENFORCED_ACTIONS` and `PLANNED_ACTIONS`
  in `panda_core.policy`, documented in the CONTRACT and guarded by tests.

### Fixed

- `panda init` rewrote `config.json` and made a backup on every run (INIT-001). It now touches
  the file, and backs it up, only when the content changes.
- Redaction (SEC-004): the `NAME=value` rule used `\b`, which does not occur after `_`, so
  `DB_PASSWORD=`, `AWS_SECRET_ACCESS_KEY=` and `OPENCODE_API_KEY=` leaked into the ledger
  and into tool output sent to the provider. It now matches any name that contains a
  credential word (`NAME=v`, `NAME: v`, `NAME = "v"`), adds `AKIA…`, `AIza…`, `xox?-…` and
  `gh?_…`, and removes the exact key the provider loaded (`register_secret`). The pattern
  is length-bounded so hostile input stays linear.
- Approval prompt in the cockpit (APR-002, APR-001, UI-001): the modal cut the preview at
  16 lines with no sign of it, so the tail of a diff or command was never seen, and
  `y` approved instantly, including a `y` typed into the composer as the modal opened.
  The preview now scrolls with a `+N lines hidden` counter and approval stays locked
  until the end has been seen, a truncated preview is flagged, approval is armed after
  0.4 s, and `high`/`critical` risk needs a second `y`. Deny stays immediate.
- Approval display (APR-003, UI-002): the REPL passed model text to Rich with markup on,
  so `[conceal]` hid part of a command, `[link=...]` made a hyperlink and `[/x]` raised
  an error, and the REPL and `panda run` wrote raw terminal control sequences (clipboard,
  title, clear screen) from the model. All of it is printed literally and sanitized
  (`panda_core.display.sanitize_display`); bidi and zero-width characters are shown as
  `<U+XXXX>`.
- `--auto` (APR-004): it approved every `ask`, including `git push`, against the agent's
  own rule "never push". Publishing asks (`shell-push`, `shell-git-push`, `git-push`)
  are now `never_auto`: `--auto` does not approve them.
- Validator (VAL-001, VAL-002): `parse_report` accepted `{"ok": true, "checks": []}`,
  a report with every check skipped, and reports missing required checks, so a broken
  or rewritten validator could pass. The nine unconditional checks must now be present
  and run, or it is a protocol error (exit `4`). The validator also inherited the whole
  environment, including `OPENCODE_API_KEY`; it now gets the scrubbed environment (the
  same one `run_shell` uses, keeping `PANDA_*` settings).
- Adapters (ADP-001, ADP-002): a `tool_use` or `function_call` left without a result by
  a cancelled or failed turn made Anthropic and Responses reject the replayed history,
  and trimming could leave a Gemini history starting with a `model` turn. The history is
  now repaired before it is sent: missing results are synthesized and marked as errors,
  orphan outputs are dropped, and a Google history starts with `user`.
- Provider streams (PROV-002, PROV-003, PROV-004): a stream cut off before its terminal
  event was accepted as a complete answer in all four protocols; it is now a protocol
  error (exit `4`). SSE events are read per event (several `data:` lines are joined
  with a newline), and generated tool-call ids (`call_<stream>_<index>`) no longer
  repeat between steps, which made Anthropic reject the replayed history with
  `tool_use ids must be unique`.
- ReDoS in `grep` (SEC-006): the model's regular expression ran synchronously on the
  event loop, so 26 characters could freeze the cockpit and the approval prompt for
  seconds. The search now runs in a child process (`panda_core.grep_worker`) that is
  killed after 10 seconds, while the policy filter stays in the parent.
- Byte-exact edits (EDIT-001): `edit_file` rewrote CRLF as LF and replaced non-UTF-8
  bytes with U+FFFD, which is irreversible for a file not tracked by git. It now reads
  and writes bytes, keeps the line ending, BOM and non-ASCII text, and refuses (leaving
  the file untouched) a file that is not UTF-8 or has mixed line endings.
- Content hashes in the ledger (LED-001): writes and edits report `sha256_before` and
  `sha256_after`, copied into `tool.result`, and `turn.end` carries a `tree_sha256` of
  `corpus/`, so the ledger proves the state left behind and not only the sequence.
- Process control (SEC-008): a timeout killed only the direct child, Ctrl+C cancelled
  the turn but the command kept running, `asyncio.wait_for` could not bound a running
  tool, and output was captured without a limit. `run_command` now runs the command
  in its own process group, kills the group on timeout, cancellation or an output cap
  (4 MB per stream, `output_truncated`), closes stdin, and `run_shell` propagates the
  cancellation to the process.
- Fail-closed ledger (PROV-001): `Agent._log` swallowed ledger failures, so with an
  invalid anchor the turn ended `completed`, `write_file` wrote and nothing was
  recorded. A ledger failure now raises a session-integrity error (exit `4`) before
  any tool runs, including `approval.*` records, and the MCP `_record` raises too.
  `turn.end` uses status `ledger_failed` when it can still be written.
- Context (AGT-002): `_trim` removed the user's task first, without a summary and
  without a trace in the ledger. The task of the current turn, the system prompt and
  the last six messages are now pinned, removed messages are replaced by a visible
  marker, tool exchanges are removed whole, and a `context.trim` event records the
  count and the SHA-256 of every removed message.
- Turn isolation (AGT-004): `/clear`, `Ctrl+L`, `/model`, `/study use` and `!cmd` ran
  beside an active turn and could leave `[system, tool]` in the history. They are now
  refused while a turn runs, and `Agent.reset_conversation`, `set_model` and
  `set_active_study` raise a usage error mid-turn.
- Agent loop (AGT-001, AGT-003): an unexpected tool exception is a failed result, any
  other exception still closes the turn, unanswered tool calls are answered on
  cancel or error, and a provider `finish_reason` of `length`, `content_filter`,
  `incomplete` or similar gives status `incomplete` instead of `completed`.
- Ledger (SES-001, SES-002, SES-003, PERF-001): an event forged into the log with a
  valid chain was legitimized by the next legitimate append, and a crash between the
  write and the anchor was absorbed silently. `append` now requires the anchor to
  equal the tail and raises a session-integrity error otherwise; the new
  `panda session recover <id>` adopts a single unanchored event and records
  `session.recovered`. `os.write` is looped until the line is complete, the tail is
  read backwards so a last event larger than 128 KiB no longer breaks `open`,
  `run.plan` is shrunk like every other event, and `is_closed()` is O(1).
  Known limit, now documented: a writer of both log and anchor can still rebuild
  the chain.
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

- **Breaking for `--auto`:** `git push` is no longer approved by it; answer the prompt.
- **New command:** `panda session recover <id>`. After a crash the writer stops
  until it is run; this is deliberate.
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

### Verified

- Baseline `231e48f` (0.2.0a4): `341 passed, 3 skipped`. This release: `771 passed, 3 skipped`
  on Linux (Python 3.13), 406 of them in `tests/security`, each written to fail on `231e48f`
  first. `ruff`, `compileall` and `mypy --platform linux` / `--platform win32` are clean;
  `pip-audit` reports no known vulnerabilities for the resolved dependencies; `python -m
  build` and `twine check` pass.
- 4 existing test assertions changed, each with the owner's approval and its source in the
  commit message: a policy default for `node ...validate.mjs`, a provider error now writing
  `turn.end`, a missing required validator check, and the approval modal's arming delay.

### Not verified

- Windows (process-tree kill with `taskkill`, junctions, `O_NOFOLLOW` absence, path case).
- The approval modal and `y` typed ahead in a real terminal; only Textual's pilot was used.
- Any call to a real provider; adapters are covered by fixtures and `MockTransport`.
- The `release.yml`, `ci.yml` `audit` job and Dependabot run on GitHub; only their syntax and
  structure were checked locally.
- Actions are pinned by tag, not by commit SHA.

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

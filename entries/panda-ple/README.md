# panda-ple

The [Panda harness](https://github.com/BlackYuriJDU/panda-harness) as an entry for
Google's **Physical-Engineering's Last Exam** harness competition. It speaks the
evaluator's [serve protocol](https://github.com/google-research/ple/blob/main/docs/protocol.md)
through Google's `taskbridge.py` (copied unmodified, Apache-2.0, see `NOTICE`) and
runs the Panda agent loop (`panda_core`) inside each task.

## What happens in one task

1. **Probe** (no model): one `exec` lists Python, the libraries that are present
   (`openpyxl`, `docx`, `ezdxf`, `ifcopenshell`, ...), converters, and the working
   directory's files. The container has no network, so this is fixed for the task.
2. **MCP discovery** (no model): each MCP application is initialized and its tools
   listed once. Up to 128 tools and 40 kB become direct function tools
   (`mcp__<server>__<tool>`); a larger catalog is reached through `mcp_find` and
   `mcp_call`. The tool list is frozen for the session.
3. **Agent loop** (`panda_core.agent.Agent`, no workspace): tools `bash`,
   `read_file`, `write_file`, `edit_file`, optional `view_image`, plus MCP. The
   loop stops by wall clock, `PANDA_PLE_WRAP_UP_SEC` before the task limit.
4. **Finish**: the final message is the model's last summary plus the list of files
   written. A fallback summary is kept current, so a `cancel` at the time limit
   still sends `done` with it within the 10 s the evaluator allows.

## Guarantees

| Rule | How |
|---|---|
| No task text on disk | The agent ledger is `panda_core.ledger.MemoryLedger`; nothing is written in the serve container |
| No task text in evaluator logs | `PANDA_PLE_LOG=metadata` (default) logs counts, tool names, and times only |
| Inputs never lost | Before the first overwrite of an existing file outside `/tmp`, a copy goes to `/tmp/panda-originals/` in the task container |
| Retry safety | `400` is never retried; rate limits and outages are retried until the deadline; a cut stream is discarded and repeated |
| A failure before any model output is reported as `error` | The evaluator then starts the task again (up to 3 attempts) |
| Usage is reported per model call | `usage` messages with total input (cache included), output, and cache-read tokens |
| No task-specific logic | Prompts and tools are generic engineering practice |

## Settings (`env:` in `submission.yaml`)

| Variable | Default | Meaning |
|---|---|---|
| `PANDA_PLE_EFFORT` | profile default (`high` for Claude, Gemini, gpt-oss, GLM) | Reasoning effort override |
| `PANDA_PLE_VISION` | `0` | `1` enables `view_image` |
| `PANDA_PLE_LOG` | `metadata` | `verbose` adds tool arguments; local dev runs only |
| `PANDA_PLE_MAX_STEPS` | `1000` | Model steps per task |
| `PANDA_PLE_WRAP_UP_SEC` | `240` | Seconds kept for the final summary |

## Layout

```text
submission.yaml   install, serve, model_api
serve.py          taskbridge.run(harness.solve)
harness.py        solve(session): probe, MCP, agent, usage, final message
taskbridge.py     Google's protocol implementation (unmodified)
panda_ple/        remote tools, MCP catalog, probe, system prompt
tests/            unit tests and end-to-end tests over the real protocol
dev/              local development with your own model (not part of the harness)
```

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

The end-to-end tests start the real `taskbridge` server, connect a WebSocket
evaluator stand-in that executes requests in a temporary directory, and use a
scripted model. They cover the `kit check` stages: smoke, MCP, and cancel.

## License

Apache-2.0 (`LICENSE`). `taskbridge.py` is © Google LLC, Apache-2.0. The
`panda-harness` dependency is MIT.

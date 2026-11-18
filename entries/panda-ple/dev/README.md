# Local development

Nothing in this folder is part of the harness. It holds the settings for running
the entry on your own machine.

## Dev runs on OpenCode Go (no Google Cloud billing)

The competition allows dev-set `artifacts/` from a model of your own, declared in
the Writeup. OpenCode Go needs two extra headers (`User-Agent` and
`x-opencode-session`), which the kit's request path does not send, so a local
LiteLLM proxy adds them. `litellm-go.yaml` serves the harness model and a fixed
judge from the same subscription.

```bash
pip install 'litellm[proxy]'
export OPENCODE_API_KEY=...            # never commit it
litellm --config dev/litellm-go.yaml --port 4000 &
```

Then, from a clone of `google-research/ple`:

```bash
# submission.yaml: model_api: openai-compatible/glm-5.2
uv run python -m kit check ../panda-ple --model-base-url http://host.docker.internal:4000/v1
```

`kit check` never prints `ready to submit` for an `openai-compatible` model; that
is expected. For the final commit, restore `model_api: google-cloud/claude-opus-5-5`
and run `kit check` once with a proxy that serves the real Claude Opus 5.5 under
that name (never another model under that name).

The judge for local `eval/run.py` runs is `glm-5.2-judge` in the same proxy
(`JUDGE_BASE_URL=http://localhost:4000/v1`). Use the same judge for every
comparison and declare it in the Writeup.

## Status

`litellm-go.yaml` has not been exercised yet: it needs your Go key. Check the
first run's `~/.cache/plebench/<name>/egress-model.log`.

"""Panda CLI. UI strings are English in v1."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from panda_cli import render
from panda_cli.runtime import ChatRuntime
from panda_core.agent import DEFAULT_MAX_STEPS
from panda_core.config import split_model
from panda_core.doctor import run_doctor
from panda_core.errors import ConfigError, ExecutionTimeout, PandaError, UsageError
from panda_core.init import initialize_workspace
from panda_core.model_catalog import CAPTURED_AT, snapshot_models
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.process import split_command
from panda_core.provider import (
    is_supported_model,
    provider_for_config,
    status_snapshot,
)
from panda_core.registry import find_study, load_registry, load_study_card
from panda_core.session import SessionLog, list_sessions
from panda_core.validator import Validator
from panda_core.version import __version__
from panda_core.workspace import Workspace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help=(
        "Fail-closed autonomous harness for evidence-heavy research corpora. "
        "Runtime: Panda cockpit + OpenCode Console models."
    ),
)
session_app = typer.Typer(help="Hash-chained session ledger", no_args_is_help=True)
policy_app = typer.Typer(help="Policy evaluation and explanation", no_args_is_help=True)
study_app = typer.Typer(help="Read-only study registry", no_args_is_help=True)
mcp_app = typer.Typer(help="Optional MCP export for other clients", no_args_is_help=True)

app.add_typer(session_app, name="session")
app.add_typer(policy_app, name="policy")
app.add_typer(study_app, name="study")
app.add_typer(mcp_app, name="mcp")

def handle[F: Callable[..., Any]](fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except PandaError as exc:
            render.print_error(exc)
            raise typer.Exit(exc.exit_code) from None
        except KeyboardInterrupt:
            raise typer.Exit(130) from None

    return wrapper  # type: ignore[return-value]


def _root_value(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _load_workspace(root: Path | None, *, require_init: bool = False) -> Workspace:
    return Workspace.load(_root_value(root), require_init=require_init)


def _session_metadata(workspace: Workspace) -> dict[str, Any]:
    env_names = sorted(
        name
        for name in os.environ
        if name.startswith(("PANDA_", "OPENROUTER_", "OPENCODE_"))
    )
    return {
        "cwd": str(Path.cwd()),
        "panda_version": __version__,
        "git": workspace.git_state(),
        "tools": workspace.tool_versions(),
        "env_names": env_names,
    }


def _create_session(
    workspace: Workspace,
    label: str,
    *,
    private: bool = False,
) -> SessionLog:
    workspace.ensure_runtime_dirs()
    return SessionLog.create(
        workspace.sessions_dir,
        label=label,
        metadata=_session_metadata(workspace),
        private=private,
        redact_enabled=workspace.config.logging.redact,
    )


def _active_session(
    workspace: Workspace, identifier: str | None = None
) -> SessionLog | None:
    if identifier:
        return SessionLog.resolve(
            workspace.sessions_dir,
            identifier,
            redact_enabled=workspace.config.logging.redact,
        )
    env_path = os.environ.get("PANDA_SESSION_FILE")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            return SessionLog.open(path, redact_enabled=workspace.config.logging.redact)
    return None


def _close_quietly(session: SessionLog, status: str, data: dict[str, Any] | None = None) -> None:
    try:
        if not session.is_closed():
            session.close(status, data)
    except (PandaError, OSError):
        pass


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", help="Print the Panda version and exit.", is_eager=True
    ),
    plain: bool = typer.Option(
        False, "--plain", help="Use the line REPL instead of the full-screen cockpit.",
        is_eager=True,
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model id for the cockpit (opencode/<model>)."
    ),
    auto: bool = typer.Option(False, "--auto", help="Approve every `ask` action."),
    max_steps: int = typer.Option(
        DEFAULT_MAX_STEPS, "--max-steps", min=1, max=200, help="Maximum model steps per turn."
    ),
    session: str | None = typer.Option(None, "--session", help="Continue a Panda session."),
    private: bool = typer.Option(False, "--private", help="Never persist task text."),
) -> None:
    if plain:
        os.environ["NO_COLOR"] = "1"
    if version:
        typer.echo(f"panda {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _start_chat(
            root=None,
            plain=plain,
            model=model,
            auto=auto,
            max_steps=max_steps,
            session=session,
            private=private,
        )


@app.command("init")
@handle
def init_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    model: str | None = typer.Option(
        None, "--model", help="Provider model id (opencode/<model>)."
    ),
    validator: str | None = typer.Option(
        None, "--validator", help="Validator path relative to the root."
    ),
    allow_missing_validator: bool = typer.Option(
        False, "--allow-missing-validator", help="Initialize even if the validator is absent."
    ),
    force: bool = typer.Option(False, "--force", help="Rewrite generated files after a backup."),
    force_model: bool = typer.Option(False, "--force-model", help="Overwrite an existing model."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned writes without writing."),
    opencode: bool = typer.Option(
        False,
        "--opencode",
        help="Legacy: also write/merge opencode.json and wire the Panda MCP server.",
    ),
    panda_command: str | None = typer.Option(
        None,
        "--panda-command",
        help="Legacy OpenCode launcher command (only with --opencode).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    command = split_command(panda_command) if panda_command else None
    result = initialize_workspace(
        _root_value(root),
        model=model,
        validator=validator,
        allow_missing_validator=allow_missing_validator,
        force=force,
        dry_run=dry_run,
        opencode=opencode,
        panda_command=command,
        force_model=force_model,
    )
    if json_output:
        render.print_json(result.to_dict())
    else:
        render.banner(__version__)
        render.init_panel(result)


@app.command("doctor")
@handle
def doctor_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    validate: bool = typer.Option(False, "--validate", help="Also run the corpus validator."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    try:
        workspace = _load_workspace(root)
    except ConfigError:
        workspace = Workspace.from_root(_root_value(root))
    report = run_doctor(workspace, run_validation=validate)
    if json_output:
        render.print_json(report.to_dict())
    else:
        render.banner(__version__)
        render.doctor_report(report)
    raise typer.Exit(0 if report.ok else 1)


@app.command("validate")
@handle
def validate_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    release: bool = typer.Option(False, "--release", help="Enforce public-face release checks."),
    timeout: int | None = typer.Option(None, "--timeout", help="Validator timeout in seconds."),
    session: str | None = typer.Option(
        None, "--session", help="Append provenance to an existing session id."
    ),
    record: bool = typer.Option(
        False, "--record", help="Create a session for this validation if none is active."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the validator JSON report."),
) -> None:
    workspace = _load_workspace(root)
    log = _active_session(workspace, session)
    created = False
    if record and log is None:
        log = _create_session(workspace, "validate")
        created = True
    if log is not None:
        log.append("validate.start", {"release": release, "root": str(workspace.root)})
    try:
        report = Validator(
            workspace.root,
            script=workspace.validator_path,
            node=workspace.config.node,
            min_major=workspace.config.node_min_major,
            timeout=timeout or workspace.config.timeouts.validate_seconds,
        ).run(release=release)
    except PandaError as exc:
        if log is not None:
            log.append("validate.error", {"code": exc.code, "message": exc.message})
            if created:
                _close_quietly(log, "error")
        raise
    if log is not None:
        log.append(
            "validate.result",
            {
                "ok": report.ok,
                "release": release,
                "passed": report.passed,
                "failed": report.failed,
                "skipped": report.skipped,
                "failed_checks": [
                    check.name for check in report.checks if not check.ok and not check.skipped
                ],
            },
        )
        if created:
            _close_quietly(log, "ok" if report.ok else "failed")
    payload = report.to_dict()
    payload["session_id"] = log.session_id if log is not None else None
    if json_output:
        render.print_json(payload)
    else:
        render.banner(__version__)
        render.validation_report(report)
        if log is not None:
            typer.echo(f"session: {log.session_id}")
    raise typer.Exit(0 if report.ok else 1)


@app.command("run")
@handle
def run_command(
    task: list[str] | None = typer.Argument(None, help="Task for the autonomous Panda agent."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    model: str | None = typer.Option(None, "--model", help="Model id (opencode/<model>)."),
    auto: bool = typer.Option(False, "--auto", help="Approve every `ask` action."),
    json_output: bool = typer.Option(False, "--json", help="Emit a JSON result."),
    session: str | None = typer.Option(None, "--session", help="Continue an existing session."),
    private: bool = typer.Option(
        False, "--private", help="Persist only a hash of the task, never its text."
    ),
    max_steps: int = typer.Option(
        DEFAULT_MAX_STEPS, "--max-steps", min=1, max=200, help="Maximum model steps."
    ),
    timeout: int | None = typer.Option(None, "--timeout", help="Run timeout in seconds."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; do not call the model."),
    quiet: bool = typer.Option(False, "--quiet", help="Do not print the session id."),
) -> None:
    task_text = " ".join(task or []).strip()
    if not task_text:
        raise UsageError("a task is required", hint='example: panda run "validate the corpus"')
    workspace = _load_workspace(root, require_init=True)
    selected_model = _validate_model(workspace, model)
    log = _active_session(workspace, session)
    if log is not None and log.is_closed():
        raise ConfigError(f"session already closed: {log.session_id}")
    if log is None:
        label = "run: " + (task_text[:60] if not private else "[private task]")
        log = _create_session(workspace, label, private=private)

    run_data = {
        "task": None if private or not workspace.config.logging.log_tasks else task_text,
        "task_sha256": hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
        "task_chars": len(task_text),
        "private": private,
        "model": selected_model,
        "provider": workspace.config.provider.name,
        "max_steps": max_steps,
        "cwd": str(workspace.root),
        "git": workspace.git_state(),
    }

    if dry_run:
        runtime = _runtime(workspace, log, selected_model, max_steps, private, auto)
        log.append("run.plan", {**run_data, "tools": runtime.tools.names()})
        _close_quietly(log, "planned")
        typer.echo(f"model: {selected_model}")
        provider = workspace.config.provider
        typer.echo(f"provider: {provider.name} ({provider.base_url})")
        typer.echo(f"tools: {', '.join(runtime.tools.names())}")
        typer.echo(f"session: {log.session_id}")
        runtime.close("planned")
        return

    log.append("run.start", run_data)
    if not quiet:
        typer.echo(f"session: {log.session_id}", err=True)
    streamed = {"any": False}

    def on_event(event: Any) -> None:
        if json_output:
            return
        if event.kind == "text_delta":
            typer.echo(event.data.get("text", ""), nl=False)
            streamed["any"] = True
        elif event.kind == "tool_call":
            typer.echo(f"\n[tool] {event.data.get('name')}", err=True)
        elif event.kind == "tool_result":
            state = "blocked" if event.data.get("blocked") else "ok"
            typer.echo(f"[tool] {event.data.get('name')}: {state}", err=True)

    async def _execute() -> Any:
        runtime = _runtime(
            workspace, log, selected_model, max_steps, private, auto, on_event=on_event
        )
        try:
            return await runtime.ask(task_text)
        finally:
            await runtime.provider.aclose()

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _execute(),
                timeout=timeout or workspace.config.timeouts.run_seconds,
            )
        )
    except TimeoutError as exc:
        log.append("run.error", {"code": "execution_timeout", "message": "run timed out"})
        _close_quietly(log, "error")
        raise ExecutionTimeout(
            "run exceeded the timeout",
            hint="raise --timeout or narrow the task",
        ) from exc
    except PandaError as exc:
        log.append("run.error", {"code": exc.code, "message": exc.message})
        _close_quietly(log, "error")
        raise

    log.append(
        "run.end",
        {
            "status": result.status,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
            "usage": result.usage.to_dict(),
        },
    )
    _close_quietly(log, "ok" if result.status == "completed" else result.status)
    if not json_output and streamed["any"]:
        typer.echo()
    payload = {
        "ok": result.status == "completed",
        "model": selected_model,
        "provider": workspace.config.provider.name,
        **result.to_dict(),
    }
    if json_output:
        render.print_json(payload)
    if result.status == "cancelled":
        raise typer.Exit(130)
    if result.status != "completed":
        raise typer.Exit(1)


def _runtime(
    workspace: Workspace,
    log: SessionLog,
    model: str,
    max_steps: int,
    private: bool,
    auto: bool,
    *,
    on_event: Any = None,
    approver: Any = None,
) -> ChatRuntime:
    return ChatRuntime(
        workspace,
        model=model,
        max_steps=max_steps,
        private=private,
        auto=auto,
        session=log,
        approver=approver,
        on_event=on_event,
    )


def _validate_model(workspace: Workspace, model: str | None) -> str:
    selected = model or workspace.config.model
    provider, name = split_model(selected)
    if not name:
        raise ConfigError("model must use the provider/model form")
    if provider != workspace.config.provider.name:
        raise ConfigError(
            f"model provider {provider!r} is not configured",
            hint=(
                f"the workspace provider is {workspace.config.provider.name!r}; "
                "run `panda models` and pass --model opencode/<id>"
            ),
        )
    if not is_supported_model(selected):
        raise ConfigError(
            f"model {selected!r} uses a protocol this build does not implement",
            hint="choose an OpenAI-compatible model; `panda models` marks the rest unsupported",
        )
    return selected


@app.command("chat")
@handle
def chat_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    plain: bool = typer.Option(False, "--plain", help="Line REPL instead of the cockpit."),
    model: str | None = typer.Option(None, "--model", help="Model id (opencode/<model>)."),
    auto: bool = typer.Option(False, "--auto", help="Approve every `ask` action."),
    max_steps: int = typer.Option(
        DEFAULT_MAX_STEPS, "--max-steps", min=1, max=200, help="Maximum model steps."
    ),
    session: str | None = typer.Option(None, "--session", help="Continue an existing session."),
    private: bool = typer.Option(False, "--private", help="Never persist task text."),
) -> None:
    _start_chat(
        root=root,
        plain=plain,
        model=model,
        auto=auto,
        max_steps=max_steps,
        session=session,
        private=private,
    )


def _start_chat(
    *,
    root: Path | None,
    plain: bool,
    model: str | None,
    auto: bool,
    max_steps: int,
    session: str | None,
    private: bool,
) -> None:
    if plain:
        os.environ["NO_COLOR"] = "1"
    try:
        workspace = _load_workspace(root, require_init=True)
    except ConfigError as exc:
        if not sys.stdout.isatty():
            _status(root=root, json_output=False)
            return
        render.print_error(exc)
        raise typer.Exit(exc.exit_code) from None
    selected = _validate_model(workspace, model)
    if not plain and not sys.stdout.isatty():
        _status(root=root, json_output=False)
        return
    log = _active_session(workspace, session)
    if log is not None and log.is_closed():
        raise ConfigError(f"session already closed: {log.session_id}")
    if log is None:
        log = _create_session(workspace, "cockpit", private=private)
    if plain:
        from panda_cli.repl import run_plain_chat

        run_plain_chat(
            workspace,
            log,
            model=selected,
            max_steps=max_steps,
            private=private,
            auto=auto,
        )
        return
    try:
        from panda_cli.cockpit import PandaCockpit
    except ImportError as exc:
        raise PandaError(
            "the cockpit dependency (textual) is not installed",
            hint="install textual or run `panda --plain`",
        ) from exc
    PandaCockpit(
        workspace,
        session=log,
        model=selected,
        max_steps=max_steps,
        private=private,
        auto=auto,
    ).run()


@app.command("models")
@handle
def models_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Require the live catalogue; do not fall back to the snapshot."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    try:
        workspace = _load_workspace(root)
    except ConfigError:
        workspace = Workspace.from_root(_root_value(root))
    try:
        models, source, error, captured_at = asyncio.run(
            _collect_models(workspace, refresh=refresh)
        )
    except PandaError as exc:
        if not json_output:
            render.print_error(exc)
        raise typer.Exit(exc.exit_code) from None
    rows = [
        {
            "id": model.qualified(workspace.config.provider.name),
            "protocol": model.protocol,
            "supported": model.supported,
            "free": model.free,
        }
        for model in models
    ]
    if json_output:
        render.print_json(
            {
                "ok": True,
                "provider": workspace.config.provider.name,
                "source": source,
                "captured_at": captured_at,
                "count": len(rows),
                "models": rows,
                "error": error,
            }
        )
        return
    render.models_table(rows, source=source, captured_at=captured_at, error=error)


async def _collect_models(
    workspace: Workspace, *, refresh: bool
) -> tuple[list[Any], str, str | None, str]:
    provider = provider_for_config(workspace.config)
    try:
        models = await provider.list_models()
        return models, "live", None, ""
    except PandaError as exc:
        if refresh:
            raise
        return snapshot_models(), "snapshot", exc.message, CAPTURED_AT
    finally:
        await provider.aclose()


@app.command("status")
@handle
def status_command(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    _status(root=root, json_output=json_output)


def _status(root: Path | None, *, json_output: bool) -> None:
    try:
        workspace = _load_workspace(root)
    except ConfigError as exc:
        if json_output:
            render.print_json(
                {
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                    "hint": exc.hint,
                }
            )
            raise typer.Exit(0) from None
        render.banner(__version__)
        typer.echo("not a Panda workspace")
        typer.echo(f"hint: {exc.hint}")
        raise typer.Exit(0) from None
    last = SessionLog.latest(
        workspace.sessions_dir, redact_enabled=workspace.config.logging.redact
    )
    last_summary = last.summary() if last else None
    info = workspace.describe()
    info["provider"] = status_snapshot()
    info["provider"]["model"] = workspace.config.model
    if json_output:
        render.print_json(
            {
                "ok": True,
                "workspace": info,
                "last_session": last_summary.to_dict() if last_summary else None,
            }
        )
        return
    render.banner(__version__)
    render.status_panel(info, last_summary)


@app.command("version")
def version_command() -> None:
    typer.echo(f"panda {__version__}")


@app.command("brand")
def brand_command() -> None:
    render.banner(__version__)
    typer.echo(f"panda {__version__} · evidence harness · fail-closed by construction")


# ---- session -----------------------------------------------------------------
@session_app.command("start")
@handle
def session_start(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    label: str = typer.Option("", "--label", help="Human-readable session label."),
    private: bool = typer.Option(False, "--private", help="Mark this session as private."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root, require_init=True)
    log = _create_session(workspace, label, private=private)
    payload = {"ok": True, "session_id": log.session_id, "path": str(log.path)}
    if json_output:
        render.print_json(payload)
    else:
        typer.echo(f"session {log.session_id}")
        typer.echo(str(log.path))


@session_app.command("list")
@handle
def session_list(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum sessions."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    summaries = list_sessions(
        workspace.sessions_dir, redact_enabled=workspace.config.logging.redact
    )[:limit]
    if json_output:
        render.print_json({"ok": True, "sessions": [item.to_dict() for item in summaries]})
    else:
        render.sessions_table(summaries)


@session_app.command("show")
@handle
def session_show(
    session_id: str = typer.Argument(..., help="Session id or 'latest'."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum events to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    log = SessionLog.resolve(
        workspace.sessions_dir, session_id, redact_enabled=workspace.config.logging.redact
    )
    summary = log.summary()
    verification = log.verify()
    events = list(log.events())[-limit:]
    if json_output:
        render.print_json(
            {
                "ok": verification.ok,
                "summary": summary.to_dict(),
                "verification": verification.to_dict(),
                "events": [event.to_record() for event in events],
            }
        )
        return
    render.session_detail(summary, verification, events)


@session_app.command("verify")
@handle
def session_verify(
    session_id: str | None = typer.Argument(None, help="Session id or 'latest'; default: all."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    if session_id:
        logs = [
            SessionLog.resolve(
                workspace.sessions_dir, session_id, redact_enabled=workspace.config.logging.redact
            )
        ]
    else:
        logs = [
            SessionLog.open(path, redact_enabled=workspace.config.logging.redact)
            for path in sorted(workspace.sessions_dir.glob("s_*.jsonl"))
        ]
    verifications = [log.verify() for log in logs]
    if json_output:
        render.print_json(
            {
                "ok": all(item.ok for item in verifications),
                "sessions": [item.to_dict() for item in verifications],
            }
        )
    elif verifications:
        render.verify_table(verifications)
    else:
        typer.echo("no sessions found")
    raise typer.Exit(0 if all(item.ok for item in verifications) else 1)


@session_app.command("close")
@handle
def session_close(
    session_id: str = typer.Argument(..., help="Session id or 'latest'."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    status: str = typer.Option("completed", "--status", help="Completion status."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    log = SessionLog.resolve(
        workspace.sessions_dir, session_id, redact_enabled=workspace.config.logging.redact
    )
    event = log.close(status)
    payload = {"ok": True, "session_id": log.session_id, "closed_at": event.ts, "status": status}
    if json_output:
        render.print_json(payload)
    else:
        typer.echo(f"closed {log.session_id} ({status})")


# ---- policy ------------------------------------------------------------------
def _policy_engine(workspace: Workspace) -> tuple[PolicyEngine, str]:
    policy, from_file = load_policy_or_default(workspace.policy_path)
    return PolicyEngine(policy, root=workspace.root), "file" if from_file else "built-in default"


@policy_app.command("check")
@handle
def policy_check(
    action: str = typer.Argument(
        ..., help="read, write, edit, delete, shell, git, release, spend, tool."
    ),
    resource: str = typer.Argument(..., help="Path, shell command, or tool name."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    approve: bool = typer.Option(False, "--approve", help="Allow an ask decision once."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    engine, source = _policy_engine(workspace)
    normalized = (
        workspace.relative_resource(resource)
        if action in {"read", "write", "edit", "delete"} and resource
        else resource
    )
    decision = engine.decide(action, normalized)
    if approve and decision.effect == "ask":
        decision = engine.authorize(action, normalized, approved=True)
    payload = decision.to_dict()
    payload["policy_source"] = source
    payload["approved"] = approve
    if json_output:
        render.print_json(payload)
    else:
        render.decision_panel(decision)
        if decision.effect == "ask":
            typer.echo("hint: rerun with --approve to allow this action once")
        elif decision.effect in {"deny", "hard_deny"}:
            typer.echo("hint: this action is blocked; do not retry it through another tool")
    raise typer.Exit(0 if decision.effect == "allow" else 5)


@policy_app.command("explain")
@handle
def policy_explain(
    action: str = typer.Argument(..., help="Policy action."),
    resource: str = typer.Argument(..., help="Path, shell command, or tool name."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    engine, source = _policy_engine(workspace)
    normalized = (
        workspace.relative_resource(resource)
        if action in {"read", "write", "edit", "delete"} and resource
        else resource
    )
    decision = engine.decide(action, normalized)
    matched = engine.matching_rules(action, normalized)
    if json_output:
        render.print_json(
            {
                "ok": True,
                "policy_source": source,
                "decision": decision.to_dict(),
                "matching_rules": [rule.to_dict() for rule in matched],
            }
        )
    else:
        render.rules_table(matched, decision)


@policy_app.command("show")
@handle
def policy_show(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    policy, from_file = load_policy_or_default(workspace.policy_path)
    if json_output:
        render.print_json(
            {
                "ok": True,
                "source": "file" if from_file else "built-in default",
                "path": str(workspace.policy_path),
                "policy": policy.to_dict(),
            }
        )
        return
    typer.echo(f"policy: {'file' if from_file else 'built-in default'} ({workspace.policy_path})")
    render.rules_table(policy.rules)


# ---- study -------------------------------------------------------------------
@study_app.command("list")
@handle
def study_list(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    track: str | None = typer.Option(None, "--track", help="public, simulated, or real."),
    status: str | None = typer.Option(None, "--status", help="planned, active, or done."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    studies = load_registry(workspace.root)
    if track:
        studies = [study for study in studies if study.track == track]
    if status:
        studies = [study for study in studies if study.status == status]
    if json_output:
        render.print_json({"ok": True, "studies": [study.to_dict() for study in studies]})
    else:
        render.study_table(studies)


@study_app.command("show")
@handle
def study_show(
    study_id: str = typer.Argument(..., help="Registered study id."),
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    workspace = _load_workspace(root)
    study = find_study(workspace.root, study_id)
    card = load_study_card(study)
    if json_output:
        payload = study.to_dict()
        payload["frontmatter"] = card["frontmatter"]
        payload["body"] = card["body"]
        render.print_json({"ok": True, "study": payload})
    else:
        render.study_detail(study, card)


# ---- MCP ---------------------------------------------------------------------
@mcp_app.command("serve")
@handle
def mcp_serve(
    root: Path | None = typer.Option(None, "--root", "-r", help="Corpus root (default: cwd)."),
    session: Path | None = typer.Option(
        None, "--session", help="Append MCP tool calls to this session file."
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="stdio or streamable-http (loopback only).",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Loopback host for streamable-http."
    ),
    port: int = typer.Option(
        8765, "--port", min=1, max=65535, help="Port for streamable-http."
    ),
) -> None:
    from panda_core.mcp_server import serve

    serve(
        _root_value(root),
        session_file=session,
        transport=transport,
        host=host,
        port=port,
    )


@mcp_app.command("tools")
def mcp_tools() -> None:
    render.print_json(
        {
            "server": "panda",
            "direct_tools": [
                "workspace_info",
                "validate",
                "policy_check",
                "study_list",
                "study_show",
                "session_note",
            ],
            "opencode_tools": [
                "panda_workspace_info",
                "panda_validate",
                "panda_policy_check",
                "panda_study_list",
                "panda_study_show",
                "panda_session_note",
            ],
            "transports": ["stdio", "streamable-http"],
            "http_path": "/mcp",
            "resources": ["panda://workspace", "panda://studies"],
        }
    )


def main() -> None:
    _configure_stdio()
    app()


def _configure_stdio() -> None:
    """Emit UTF-8 even on legacy Windows code pages and redirected pipes."""

    import sys

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()

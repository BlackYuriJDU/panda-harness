"""Wave 5: redaction, privacy, idempotent init, git hardening, dead code, contract tests.

SEC-004/009, TRUST-004, PRIV-001/002, INIT-001, DEAD-001, LEG-001, DOC-001, DEP-001.
Synthetic data only.
"""

from __future__ import annotations

import pytest

from panda_core.redact import REDACTED, redact, redact_text, register_secret

# --------------------------------------------------------------------------- SEC-004
LEAKS = [
    "DB_PASSWORD=hunter2hunter2",
    "AWS_SECRET_ACCESS_KEY=abcd1234efgh5678",
    "OPENCODE_API_KEY=oc_fakefakefakefakefake",
    "GITHUB_TOKEN=ghx_fakefakefake",
    "export STRIPE_SECRET=fakefakefake1",
    "password: hunter2hunter2",
    'client_secret = "fakefakefake1"',
    "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE123456",
    "AKIAFAKEFAKEFAKE1234",
    "xoxb-1111-2222-fakefakefake",
    "ghs_FAKEFAKEFAKEFAKEFAKEFAKE",
]


@pytest.mark.parametrize("leak", LEAKS)
def test_known_secret_shapes_are_redacted(leak: str) -> None:
    value = leak.split("=")[-1].split(": ")[-1].strip(" \"'")
    cleaned = redact_text(f"log line: {leak} end")
    assert value not in cleaned, cleaned
    assert REDACTED in cleaned
    assert "log line:" in cleaned and "end" in cleaned


def test_the_name_of_the_variable_stays_readable() -> None:
    assert redact_text("DB_PASSWORD=hunter2hunter2").startswith("DB_PASSWORD=")


def test_an_exact_loaded_key_is_redacted_wherever_it_appears() -> None:
    register_secret("zz-custom-key-with-no-known-shape-99")
    cleaned = redact_text("the model echoed zz-custom-key-with-no-known-shape-99 back")
    assert "zz-custom-key-with-no-known-shape-99" not in cleaned
    assert "zz-custom" not in str(redact({"note": "zz-custom-key-with-no-known-shape-99"}))


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096",
        "the token count is 4096 and passwords are hashed",
        "total_tokens: 123456",
        "see docs/credentials-howto.md for secret handling",
        "sk-short",
        "key=1",
    ],
)
def test_neg_ordinary_text_is_not_touched(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    ["sk-abcdefghijklmnopqrstuvwxyz012345", "Authorization: Bearer abcdefghijklmnop12345678"],
)
def test_neg_previously_covered_formats_stay_covered(text: str) -> None:
    assert redact_text(text) != text


def test_the_provider_registers_the_key_it_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    from panda_core.provider import OpenCodeProvider

    monkeypatch.setenv("OPENCODE_API_KEY", "oc-exact-value-from-env-123456")
    provider = OpenCodeProvider()
    assert provider._require_api_key() == "oc-exact-value-from-env-123456"
    leaked = "x oc-exact-value-from-env-123456 y"
    assert "oc-exact-value-from-env-123456" not in redact_text(leaked)


@pytest.mark.parametrize(
    "hostile",
    ["a" * 300_000, "token" * 60_000, "=" * 100_000, ("password " * 40_000), "a=" * 100_000],
)
def test_redaction_stays_linear_on_hostile_input(hostile: str) -> None:
    import time

    started = time.perf_counter()
    redact_text(hostile)
    assert time.perf_counter() - started < 3.0


# --------------------------------------------------------------------------- TRUST-004
import asyncio  # noqa: E402
import json  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX permissions and flock")


@POSIX
def test_git_status_at_boot_does_not_run_a_configured_fsmonitor(workspace, tmp_path: Path) -> None:
    marker = tmp_path / "fsmonitor-ran.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=workspace.root, check=True)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=workspace.root, check=True)
    state = workspace.git_state()
    assert state["repo"] is True
    assert not marker.exists(), "git status executed the fsmonitor named by .git/config"


def test_neg_git_state_still_reports_the_repository(workspace) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace.root, check=True)
    state = workspace.git_state()
    assert state["repo"] is True and state["branch"] == "main"


# --------------------------------------------------------------------------- SEC-009
@POSIX
def test_a_held_lock_times_out_instead_of_blocking_forever(tmp_path: Path) -> None:
    import time

    from panda_core.atomic import file_lock

    lock = tmp_path / "x.lock"
    release = threading.Event()
    ready = threading.Event()

    def holder() -> None:
        with file_lock(lock):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert ready.wait(5)
    started = time.monotonic()
    with pytest.raises(TimeoutError), file_lock(lock, timeout=0.5):
        pass
    assert time.monotonic() - started < 3
    release.set()
    thread.join(5)


@POSIX
def test_edit_keeps_the_file_mode(workspace) -> None:
    from test_w2b import call, make_registry

    registry, _ = make_registry(workspace)
    script = workspace.root / "corpus" / "run.sh"
    script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    script.chmod(0o755)
    private = workspace.root / "corpus" / "private.txt"
    private.write_text("secret old\n", encoding="utf-8")
    private.chmod(0o600)
    call(registry, "edit_file", path="corpus/run.sh", old_string="old", new_string="new")
    call(registry, "edit_file", path="corpus/private.txt", old_string="old", new_string="new")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


@POSIX
def test_write_keeps_the_mode_of_an_existing_file_and_new_files_are_0644(workspace) -> None:
    from test_w2b import call, make_registry

    registry, _ = make_registry(workspace)
    existing = workspace.root / "corpus" / "keep.txt"
    existing.write_text("a", encoding="utf-8")
    existing.chmod(0o600)
    call(registry, "write_file", path="corpus/keep.txt", content="b")
    call(registry, "write_file", path="corpus/fresh.txt", content="c")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    mode = stat.S_IMODE((workspace.root / "corpus" / "fresh.txt").stat().st_mode)
    assert mode == 0o644


@pytest.mark.parametrize("tool", ["edit_file", "write_file"])
def test_a_file_changed_while_waiting_for_approval_is_not_overwritten(
    workspace, tool: str
) -> None:
    from test_w2b import make_registry

    target = workspace.root / "pipeline" / "a.py"  # `ask` by the default policy
    target.parent.mkdir(exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")

    async def approver(request) -> bool:
        target.write_text("value = 1\n# someone else edited this meanwhile\n", encoding="utf-8")
        return True

    registry, _ = make_registry(workspace, approver=approver)
    args = (
        {"path": "pipeline/a.py", "old_string": "1", "new_string": "2"}
        if tool == "edit_file"
        else {"path": "pipeline/a.py", "content": "value = 2\n"}
    )
    result = asyncio.run(registry.call(tool, args))
    assert not result.ok and "changed" in result.content
    assert "someone else" in target.read_text(encoding="utf-8")


def test_neg_an_approved_edit_of_an_unchanged_file_still_applies(workspace) -> None:
    from test_w2b import approve_all, make_registry

    target = workspace.root / "pipeline" / "b.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")
    registry, _ = make_registry(workspace, approver=approve_all)
    args = {"path": "pipeline/b.py", "old_string": "1", "new_string": "2"}
    assert asyncio.run(registry.call("edit_file", args)).ok
    assert target.read_text(encoding="utf-8") == "value = 2\n"


# --------------------------------------------------------------------------- INIT-001
def test_init_does_not_rewrite_or_back_up_an_unchanged_config(fake_corpus: Path) -> None:
    from panda_core.init import initialize_workspace

    initialize_workspace(fake_corpus)
    config = fake_corpus / ".panda" / "config.json"
    before = config.read_bytes()
    mtime = config.stat().st_mtime_ns
    second = initialize_workspace(fake_corpus)
    assert config.read_bytes() == before and config.stat().st_mtime_ns == mtime
    assert not (fake_corpus / ".panda" / "backups").exists()
    assert str(config) in second.unchanged


def test_init_backs_up_a_config_that_really_changes(fake_corpus: Path) -> None:
    from panda_core.init import initialize_workspace

    initialize_workspace(fake_corpus)
    config = fake_corpus / ".panda" / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["created_with"] = "0.1.0"  # an older Panda wrote this; init will update it
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    initialize_workspace(fake_corpus)
    assert list((fake_corpus / ".panda" / "backups").rglob("config.json"))
    assert json.loads(config.read_text(encoding="utf-8"))["created_with"] != "0.1.0"


# --------------------------------------------------------------------------- PRIV-002
def test_the_crash_log_is_redacted_private_and_never_committed(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panda_cli.cockpit import PandaCockpit

    app = PandaCockpit(workspace)
    exc = RuntimeError(
        "boom OPENCODE_API_KEY=oc_fakefakefakefakefake and DB_PASSWORD=hunter2hunter2"
    )
    path = app._write_crash_log(exc, "test")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "oc_fakefakefakefakefake" not in text and "hunter2hunter2" not in text
    assert "boom" in text
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_init_git_ignores_the_logs_directory(fake_corpus: Path) -> None:
    from panda_core.init import initialize_workspace

    initialize_workspace(fake_corpus)
    ignore = (fake_corpus / ".panda" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "logs/" in ignore


# --------------------------------------------------------------------------- DEP-001
def test_the_mcp_floor_excludes_the_versions_with_advisories() -> None:
    import re

    text = Path(__file__).resolve().parents[2].joinpath("pyproject.toml").read_text("utf-8")
    match = re.search(r'"mcp>=(\d+)\.(\d+)(?:\.\d+)?,<2"', text)
    assert match, "the mcp extra must declare a floor and a ceiling"
    assert (int(match.group(1)), int(match.group(2))) >= (1, 30)

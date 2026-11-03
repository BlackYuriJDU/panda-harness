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

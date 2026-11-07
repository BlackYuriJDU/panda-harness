"""Ledger schema validation: real session events must conform to the published schema (R2-503)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from panda_core.session import SessionLog

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "panda.session.event.v1.schema.json"


def _validator() -> jsonschema.Draft202012Validator:
    with SCHEMA_PATH.open() as f:
        schema = json.load(f)
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def test_real_session_events_validate_against_schema(workspace) -> None:
    """Every event emitted by a real SessionLog must satisfy the schema."""
    validator = _validator()
    session = SessionLog.create(workspace.sessions_dir, label="schema-test")
    # Write a few events through the normal API
    session.append("system.hello", {"msg": "test"})
    session.append("tool.call", {"name": "read_file", "path": "x.txt"})
    session.append("tool.result", {"name": "read_file", "ok": True})

    for event in session.events():
        validator.validate(event.to_record())


def test_fixture_examples_validate_against_schema() -> None:
    """The committed valid fixtures must still satisfy the schema."""
    validator = _validator()
    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "schemas" / "panda.session.event"
    for path in fixtures_dir.glob("valid-*.json"):
        with path.open() as f:
            data = json.load(f)
        validator.validate(data)


def test_fixture_invalid_examples_fail_validation() -> None:
    """The committed invalid fixtures must fail validation."""
    validator = _validator()
    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "schemas" / "panda.session.event"
    for path in fixtures_dir.glob("invalid-*.json"):
        with path.open() as f:
            data = json.load(f)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(data)

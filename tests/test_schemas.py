"""JSON Schemas and their valid/invalid fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
FIXTURES = ROOT / "fixtures" / "schemas"

CASES = {
    "panda.session.event.v1.schema.json": "panda.session.event",
    "panda.metric.v1.schema.json": "panda.metric",
    "panda.harness.proposal.v1.schema.json": "panda.harness.proposal",
    "panda.fanout.plan.v1.schema.json": "panda.fanout.plan",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema_name", sorted(CASES))
def test_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    schema = load(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(schema_name)


@pytest.mark.parametrize("schema_name,fixture_dir", sorted(CASES.items()))
def test_valid_fixtures(schema_name: str, fixture_dir: str) -> None:
    validator = Draft202012Validator(load(SCHEMAS / schema_name))
    paths = sorted((FIXTURES / fixture_dir).glob("valid-*.json"))
    assert paths, f"no valid fixtures for {fixture_dir}"
    for path in paths:
        errors = list(validator.iter_errors(load(path)))
        assert not errors, f"{path.name}: {[error.message for error in errors]}"


@pytest.mark.parametrize("schema_name,fixture_dir", sorted(CASES.items()))
def test_invalid_fixtures(schema_name: str, fixture_dir: str) -> None:
    validator = Draft202012Validator(load(SCHEMAS / schema_name))
    paths = sorted((FIXTURES / fixture_dir).glob("invalid-*.json"))
    assert paths, f"no invalid fixtures for {fixture_dir}"
    for path in paths:
        assert list(validator.iter_errors(load(path))), f"{path.name} should be invalid"


def test_fixture_directories_match_schemas() -> None:
    assert {path.name for path in FIXTURES.iterdir() if path.is_dir()} == set(CASES.values())

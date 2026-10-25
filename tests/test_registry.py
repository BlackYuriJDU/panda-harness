"""Study registry: table parsing, isolation ids, and study cards."""

from __future__ import annotations

from pathlib import Path

import pytest

from panda_core.errors import ConfigError
from panda_core.registry import (
    find_study,
    load_registry,
    load_study_card,
    parse_frontmatter,
    parse_index,
)


def test_load_registry_from_fixture(workspace) -> None:
    studies = load_registry(workspace.root)
    assert [study.id for study in studies] == ["gse160208", "caso-referencia"]
    assert studies[0].track == "public"
    assert studies[0].status == "done"
    assert studies[0].license == "GEO / pública"
    assert studies[0].path.endswith("gse160208.md")


def test_find_study_by_id(workspace) -> None:
    study = find_study(workspace.root, "caso-referencia")
    assert study.track == "simulated"
    with pytest.raises(ConfigError):
        find_study(workspace.root, "missing")


def test_parse_index_rejects_duplicate_ids() -> None:
    text = """| ID | Trilha | Status |
|---|---|---|
| aa | public | done |
| aa | public | done |
"""
    with pytest.raises(ConfigError):
        parse_index(text)


def test_parse_index_rejects_invalid_track() -> None:
    text = """| ID | Trilha | Status |
|---|---|---|
| aa | private | done |
"""
    with pytest.raises(ConfigError):
        parse_index(text)


def test_parse_index_rejects_missing_header() -> None:
    with pytest.raises(ConfigError):
        parse_index("no table here\n")


def test_parse_index_rejects_empty_table() -> None:
    text = """| ID | Trilha | Status |
|---|---|---|
"""
    with pytest.raises(ConfigError):
        parse_index(text)


def test_parse_index_accepts_english_headers() -> None:
    text = """| ID | Track | Status | Question | Data | License | Next |
|---|---|---|---|---|---|---|
| `abc` | public | active | Why? | GEO | CC-BY | Next |
"""
    studies = parse_index(text)
    assert studies[0].id == "abc"
    assert studies[0].question == "Why?"


def test_frontmatter_parsing() -> None:
    data, body = parse_frontmatter(
        "---\nstudy_id: x\ntrack: public\ntags:\n  - a\n  - b\n---\n\nBody\n"
    )
    assert data["study_id"] == "x"
    assert data["tags"] == ["a", "b"]
    assert body == "Body"


def test_frontmatter_without_block() -> None:
    data, body = parse_frontmatter("# Just markdown\n")
    assert data == {}
    assert body == "# Just markdown\n"


def test_frontmatter_unterminated() -> None:
    with pytest.raises(ConfigError):
        parse_frontmatter("---\nstudy_id: x\n")


def test_load_study_card(workspace) -> None:
    study = find_study(workspace.root, "gse160208")
    card = load_study_card(study)
    assert card["frontmatter"]["study_id"] == "gse160208"
    assert "Body text" in card["body"]


def test_registry_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_registry(tmp_path)

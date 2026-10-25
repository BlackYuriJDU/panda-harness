"""Read-only study registry (`corpus/studies/INDEX.md`).

The registry table is the routing source of truth. Missing or malformed cards
fail closed: the harness will not invent a study to satisfy a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panda_core.errors import ConfigError

STUDY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_ID_CELL_RE = re.compile(r"\[`?([^`\]]+)`?\]")
_LINK_RE = re.compile(r"\((.*?)\)")
TRACKS = {"public", "simulated", "real"}
STATUSES = {"planned", "active", "done"}


@dataclass(frozen=True)
class Study:
    id: str
    track: str
    status: str
    question: str = ""
    data: str = ""
    license: str = ""
    next: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "track": self.track,
            "status": self.status,
            "question": self.question,
            "data": self.data,
            "license": self.license,
            "next": self.next,
            "path": self.path,
        }


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _header_map(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        value = cell.lower()
        if "id" in value:
            mapping.setdefault("id", index)
        elif "trilha" in value or "track" in value:
            mapping.setdefault("track", index)
        elif "status" in value:
            mapping.setdefault("status", index)
        elif "pergunta" in value or "question" in value:
            mapping.setdefault("question", index)
        elif "dados" in value or value == "data":
            mapping.setdefault("data", index)
        elif "licen" in value or "license" in value:
            mapping.setdefault("license", index)
        elif "pr" in value and ("ximo" in value or "next" in value):
            mapping.setdefault("next", index)
    return mapping


def _cell(row: list[str], mapping: dict[str, int], key: str, fallback: int | None = None) -> str:
    index = mapping.get(key, fallback)
    if index is None or index >= len(row):
        return ""
    return row[index]


def parse_index(text: str, *, source: Path | None = None) -> list[Study]:
    lines = text.splitlines()
    header_index = None
    header: list[str] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_row(line)
        lowered = " ".join(cell.lower() for cell in cells)
        if "id" in lowered and ("trilha" in lowered or "track" in lowered or "status" in lowered):
            header_index = index
            header = cells
            break
    if header_index is None:
        raise ConfigError("study registry has no recognizable table header")

    mapping = _header_map(header)
    if "id" not in mapping or "track" not in mapping or "status" not in mapping:
        raise ConfigError(
            "study registry table is missing id/track/status columns",
            details={"header": header},
        )

    studies: list[Study] = []
    seen: set[str] = set()
    for line in lines[header_index + 1 :]:
        if not line.lstrip().startswith("|"):
            if studies:
                break
            continue
        cells = _split_row(line)
        if all(set(cell) <= {"-", ":", " "} for cell in cells if cell):
            continue
        id_cell = _cell(cells, mapping, "id")
        match = _ID_CELL_RE.search(id_cell)
        study_id = (match.group(1) if match else id_cell).strip().strip("`")
        if not study_id or study_id.lower() == "id":
            continue
        if not STUDY_ID_RE.match(study_id):
            raise ConfigError(f"invalid study id in registry: {study_id!r}")
        if study_id in seen:
            raise ConfigError(f"duplicate study id in registry: {study_id}")
        track = _cell(cells, mapping, "track").strip("`").lower()
        status = _cell(cells, mapping, "status").strip("`").lower()
        if track not in TRACKS:
            raise ConfigError(f"study {study_id} has invalid track: {track!r}")
        if status not in STATUSES:
            raise ConfigError(f"study {study_id} has invalid status: {status!r}")
        link = _LINK_RE.search(id_cell)
        relative = link.group(1).strip() if link else ""
        if relative and source is not None:
            path = str((source.parent / relative).resolve())
        elif relative:
            path = relative
        else:
            path = ""
        studies.append(
            Study(
                id=study_id,
                track=track,
                status=status,
                question=_cell(cells, mapping, "question", 3),
                data=_cell(cells, mapping, "data", 4),
                license=_cell(cells, mapping, "license", 5).strip("*"),
                next=_cell(cells, mapping, "next", 6),
                path=path,
            )
        )
        seen.add(study_id)

    if not studies:
        raise ConfigError("study registry table has no rows")
    return studies


def load_registry(root: Path) -> list[Study]:
    path = root / "corpus/studies/INDEX.md"
    if not path.is_file():
        raise ConfigError(
            f"study registry not found: {path}",
            hint="the corpus root must contain corpus/studies/INDEX.md",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read study registry: {path}") from exc
    return parse_index(text, source=path)


def find_study(root: Path, study_id: str) -> Study:
    for study in load_registry(root):
        if study.id == study_id:
            return study
    raise ConfigError(
        f"study not found: {study_id}",
        hint="run `panda study list` to see registered studies",
    )


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the small YAML subset used by study cards, without a YAML dependency."""

    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        raise ConfigError("study card has an unterminated frontmatter block")
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if current_key and line.strip().startswith("- "):
                value = line.strip()[2:].strip().strip('"').strip("'")
                existing = data.get(current_key)
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    data[current_key] = [value]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value:
            data[key] = value.strip('"').strip("'")
        else:
            data[key] = []
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return data, body


def load_study_card(study: Study) -> dict[str, Any]:
    if not study.path:
        return {"frontmatter": {}, "body": ""}
    path = Path(study.path)
    if not path.is_file():
        raise ConfigError(f"study card not found: {path}")
    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return {"frontmatter": frontmatter, "body": body}

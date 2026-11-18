"""Environment probe: one shell command before the first model call.

The task container has no network, so nothing can be installed mid-task.
Knowing up front which Python libraries and converters exist saves the model
turns spent discovering them, and the file index tells it where inputs live.
"""

from __future__ import annotations

import taskbridge

MODULES = (
    "numpy scipy pandas sympy matplotlib openpyxl docx pptx reportlab fpdf pypdf "
    "pdfplumber fitz PIL ezdxf shapely ifcopenshell networkx"
)
COMMANDS = "libreoffice soffice pdftotext pdftoppm unzip zip node"
PROBE_CHARS = 12_000

PROBE = f"""
echo "== python"; (python3 --version || echo "python3: missing") 2>&1
python3 - <<'PY' 2>/dev/null
import importlib.util as u
names = "{MODULES}".split()
print("modules present:", " ".join(n for n in names if u.find_spec(n)) or "none")
print("modules missing:", " ".join(n for n in names if not u.find_spec(n)) or "none")
PY
echo "== commands"
for c in {COMMANDS}; do command -v "$c" >/dev/null && printf '%s ' "$c"; done; echo
echo "== working directory: $(pwd)"
find . -maxdepth 3 -not -path '*/.*' -not -path '*/node_modules*' | sort | head -300
echo "== top-level directories"; ls -d /*/ 2>/dev/null | tr '\\n' ' '; echo
"""


async def run_probe(session: taskbridge.Session) -> str:
    """Return the probe report, or a short note when the probe cannot run."""

    try:
        result = await session.exec(PROBE, timeout_sec=60)
    except taskbridge.TaskError as exc:
        return f"(environment probe failed: {exc})"
    text = str(result.get("output") or "").strip()
    if len(text) > PROBE_CHARS:
        text = text[:PROBE_CHARS] + "\n[probe output truncated]"
    return text


__all__ = ["PROBE", "run_probe"]

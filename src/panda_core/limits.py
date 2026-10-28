"""Numeric defaults shared by the CLI and the agent.

Kept in a dependency-free module so Typer can render defaults without importing
the provider, httpx, or the agent runtime.
"""

from __future__ import annotations

DEFAULT_MAX_STEPS = 24

__all__ = ["DEFAULT_MAX_STEPS"]

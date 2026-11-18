"""In-memory ledger for runs whose task content must never reach disk.

`SessionLog` is the persisted, hash-chained ledger. Headless competition runs
on hidden tasks must not store prompts or files, so they use this drop-in with
the same `append` / `is_closed` / `session_id` surface. Events live only in
memory and are bounded; nothing is written anywhere.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LedgerEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class MemoryLedger:
    def __init__(self, session_id: str | None = None, *, max_events: int = 10_000) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self._events: deque[LedgerEvent] = deque(maxlen=max(1, max_events))
        self._closed = False

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> LedgerEvent:
        event = LedgerEvent(type=event_type, data=dict(data or {}))
        self._events.append(event)
        return event

    def events(self) -> list[LedgerEvent]:
        return list(self._events)

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


__all__ = ["LedgerEvent", "MemoryLedger"]

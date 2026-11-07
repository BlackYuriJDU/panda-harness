"""Append-only, hash-chained session ledger with a tail anchor.

Each session is a JSONL file. Every record commits to the previous record's
hash, so edits and reordering are detectable. A separate anchor file pins the
tail, which makes truncation detectable too. The anchor is updated after every
append; a missing anchor is a warning (legacy logs), a mismatched anchor is an
error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panda_core.atomic import atomic_write_json, canonical_json, ensure_dir, file_lock
from panda_core.errors import ConfigError, SessionIntegrityError
from panda_core.redact import redact

SESSION_SCHEMA = "panda.session.event.v1"
ANCHOR_SCHEMA = "panda.session.anchor.v1"
SESSION_ID_RE = re.compile(r"^s_[A-Za-z0-9_]{6,80}$")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TAIL_WINDOW = 128 * 1024
MAX_TAIL_BYTES = 256 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_session_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"s_{stamp}_{secrets.token_hex(3)}"


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def event_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionEvent:
    schema: str
    seq: int
    ts: str
    session_id: str
    type: str
    data: dict[str, Any]
    prev: str | None
    hash: str

    def to_record(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seq": self.seq,
            "ts": self.ts,
            "session_id": self.session_id,
            "type": self.type,
            "data": self.data,
            "prev": self.prev,
            "hash": self.hash,
        }

    def hash_payload(self) -> dict[str, Any]:
        record = self.to_record()
        record.pop("hash")
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> SessionEvent:
        return cls(
            schema=str(record["schema"]),
            seq=int(record["seq"]),
            ts=str(record["ts"]),
            session_id=str(record["session_id"]),
            type=str(record["type"]),
            data=record["data"],
            prev=record.get("prev"),
            hash=str(record["hash"]),
        )


@dataclass(frozen=True)
class VerifyIssue:
    code: str
    message: str
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.seq is not None:
            payload["seq"] = self.seq
        return payload


@dataclass(frozen=True)
class SessionVerification:
    session_id: str
    path: str
    ok: bool
    count: int
    last_hash: str | None
    anchor_ok: bool | None
    errors: list[VerifyIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "path": self.path,
            "events": self.count,
            "last_hash": self.last_hash,
            "anchor_ok": self.anchor_ok,
            "errors": [issue.to_dict() for issue in self.errors],
        }


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    path: str
    label: str
    started_at: str | None
    ended_at: str | None
    events: int
    closed: bool
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "label": self.label,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "events": self.events,
            "closed": self.closed,
            "size_bytes": self.size_bytes,
        }


class SessionLog:
    """Single-writer-friendly append-only log; safe across processes via a lock."""

    def __init__(self, path: Path, *, session_id: str | None = None, redact_enabled: bool = True):
        self.path = path.resolve()
        self.session_id = session_id or self.path.stem
        self.redact_enabled = redact_enabled
        if not SESSION_ID_RE.match(self.session_id):
            raise ConfigError(f"invalid session id: {self.session_id}")
        self.anchor_path = self.path.with_suffix(".anchor.json")
        self.lock_path = self.path.with_suffix(".lock")
        self._tail_type: str | None = None
        self._tail: tuple[int, str] | None = self._read_tail()
        self._size = self.path.stat().st_size if self.path.exists() else 0

    # ---- construction ----
    @classmethod
    def create(
        cls,
        sessions_dir: Path,
        *,
        label: str = "",
        metadata: dict[str, Any] | None = None,
        private: bool = False,
        redact_enabled: bool = True,
        session_id: str | None = None,
    ) -> SessionLog:
        ensure_dir(sessions_dir)
        sid = session_id or new_session_id()
        path = sessions_dir / f"{sid}.jsonl"
        if path.exists():
            raise ConfigError(f"session already exists: {sid}")
        log = cls(path, session_id=sid, redact_enabled=redact_enabled)
        log.append(
            "session.start",
            {
                "label": label,
                "private": private,
                "metadata": _jsonable(metadata or {}),
            },
        )
        return log

    @classmethod
    def open(cls, path: Path, *, redact_enabled: bool = True) -> SessionLog:
        if not path.is_file():
            raise ConfigError(f"session not found: {path}")
        return cls(path, redact_enabled=redact_enabled)

    @classmethod
    def latest(cls, sessions_dir: Path, *, redact_enabled: bool = True) -> SessionLog | None:
        if not sessions_dir.is_dir():
            return None
        paths = sorted(
            (p for p in sessions_dir.glob("s_*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return cls.open(paths[0], redact_enabled=redact_enabled) if paths else None

    @classmethod
    def resolve(
        cls, sessions_dir: Path, identifier: str, *, redact_enabled: bool = True
    ) -> SessionLog:
        if identifier == "latest":
            latest = cls.latest(sessions_dir, redact_enabled=redact_enabled)
            if latest is None:
                raise ConfigError("no sessions found", hint="run `panda session start`")
            return latest
        candidate = Path(identifier)
        if candidate.suffix == ".jsonl" and candidate.is_file():
            return cls.open(candidate, redact_enabled=redact_enabled)
        path = sessions_dir / f"{identifier}.jsonl"
        if path.is_file():
            return cls.open(path, redact_enabled=redact_enabled)
        raise ConfigError(f"session not found: {identifier}")

    # ---- reading ----
    def _read_tail(self) -> tuple[int, str] | None:
        self._tail_type = None
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return None
        with open(self.path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = TAIL_WINDOW
            while True:
                window = min(size, window)
                handle.seek(size - window)
                chunk = handle.read()
                # The last event is complete once a newline precedes it (SES-001).
                if window == size or b"\n" in chunk.rstrip():
                    break
                if window >= MAX_TAIL_BYTES:
                    raise SessionIntegrityError(
                        f"last session event is larger than {MAX_TAIL_BYTES} bytes: {self.path}"
                    )
                window *= 4
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            record = json.loads(lines[-1].decode("utf-8"))
            seq = int(record["seq"])
            digest = str(record["hash"])
            self._tail_type = str(record["type"])
        except (UnicodeDecodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SessionIntegrityError(
                f"cannot read session tail: {self.path}",
                hint="run `panda session verify` and restore a backup",
            ) from exc
        if not HASH_RE.match(digest):
            raise SessionIntegrityError(f"invalid tail hash in {self.path}")
        body = {key: value for key, value in record.items() if key != "hash"}
        if event_hash(body) != digest:
            # R2-224: a rewritten last line keeps its old `hash` field; recompute it.
            raise SessionIntegrityError(
                f"the last session event does not match its hash: {self.path}",
                hint="the session log was modified; run `panda session verify`",
            )
        return seq, digest

    def _refresh_tail(self, *, force: bool = False) -> None:
        size = self.path.stat().st_size if self.path.exists() else 0
        if force or size != self._size:
            self._tail = self._read_tail()
            self._size = size

    def _load_anchor(self) -> dict[str, Any] | None:
        if not self.anchor_path.is_file():
            return None
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionIntegrityError(f"invalid session anchor: {self.anchor_path}") from exc
        if anchor.get("schema") != ANCHOR_SCHEMA:
            raise SessionIntegrityError(f"unsupported anchor schema: {self.anchor_path}")
        return anchor

    def _write_anchor(self, seq: int, digest: str) -> None:
        atomic_write_json(
            self.anchor_path,
            {
                "schema": ANCHOR_SCHEMA,
                "session_id": self.session_id,
                "seq": seq,
                "hash": digest,
                "updated_at": utc_now(),
            },
        )

    # ---- writing ----
    def append(self, event_type: str, data: dict[str, Any] | None = None) -> SessionEvent:
        if not EVENT_TYPE_RE.match(event_type):
            raise ConfigError(f"invalid event type: {event_type}")
        payload = _jsonable(data or {})
        if self.redact_enabled:
            payload = redact(payload)

        with file_lock(self.lock_path):
            return self._append_locked(event_type, payload)

    def _check_anchor(self) -> None:
        """Refuse to extend a log whose tail is not exactly what the anchor names.

        Accepting an anchor that is behind the tail would let the next legitimate
        append legitimize an event forged into the file (SES-003); a crash between
        fsync and the anchor needs the explicit `recover()` instead (SES-002).
        """

        anchor = self._load_anchor()
        if not anchor:
            return
        if self._tail is None:
            raise SessionIntegrityError(
                f"session anchor exists but the log is empty: {self.path}",
                hint="the session log was truncated; run `panda session verify`",
            )
        anchor_seq = int(anchor.get("seq", -1))
        if anchor_seq > self._tail[0]:
            raise SessionIntegrityError(
                f"session log is shorter than its anchor: {self.path}",
                hint="the session log was truncated; restore it from a backup",
            )
        if anchor_seq < self._tail[0]:
            raise SessionIntegrityError(
                f"session log has events beyond its anchor: {self.path}",
                hint="after a crash run `panda session recover`; otherwise the log was modified",
            )
        if anchor.get("hash") != self._tail[1]:
            raise SessionIntegrityError(
                f"session anchor does not match tail: {self.path}",
                hint="the session log was modified; run `panda session verify`",
            )

    def _append_locked(
        self, event_type: str, payload: dict[str, Any], *, check_anchor: bool = True
    ) -> SessionEvent:
        # Re-read the tail under the lock every time: a same-size rewrite of the last
        # line was invisible to the size-keyed cache of a long-lived writer (R2-224).
        self._refresh_tail(force=True)
        if check_anchor:
            self._check_anchor()
        if self._tail_type == "session.end" and event_type != "session.recovered":
            # R2-225: a closed session stays closed; appending after it broke verify.
            raise SessionIntegrityError(
                f"session {self.session_id} is closed; start a new session",
                hint="run `panda session start`",
            )
        seq = (self._tail[0] + 1) if self._tail else 1
        prev = self._tail[1] if self._tail else None
        ts = utc_now()
        record: dict[str, Any] = {
            "schema": SESSION_SCHEMA,
            "seq": seq,
            "ts": ts,
            "session_id": self.session_id,
            "type": event_type,
            "data": payload,
            "prev": prev,
        }
        digest = event_hash(record)
        record["hash"] = digest
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

        ensure_dir(self.path.parent)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(self.path, flags, 0o600)
        try:
            view = memoryview(line.encode("utf-8"))
            while view:  # os.write may be short (SES-002)
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        self._tail = (seq, digest)
        self._tail_type = event_type
        self._size = self.path.stat().st_size
        self._write_anchor(seq, digest)
        return SessionEvent(
            schema=SESSION_SCHEMA,
            seq=seq,
            ts=ts,
            session_id=self.session_id,
            type=event_type,
            data=payload,
            prev=prev,
            hash=digest,
        )

    def recover(self) -> bool:
        """Adopt exactly one event that was written but never anchored.

        Only the crash window is recoverable: the chain must be intact, the anchor
        must name the event just before the tail, and nothing else may be wrong.
        The adopted event stays in the log and is named in a `session.recovered`
        event, so recovery is visible, never silent. Returns False if there was
        nothing to recover.
        """

        with file_lock(self.lock_path):
            self._refresh_tail()
            anchor = self._load_anchor()
            if anchor is None or self._tail is None:
                return False
            anchor_seq, anchor_hash = int(anchor.get("seq", -1)), str(anchor.get("hash"))
            if (anchor_seq, anchor_hash) == self._tail:
                if self.verify().ok:
                    return False
                raise SessionIntegrityError(
                    f"cannot recover {self.path}: the hash chain is not intact",
                    hint="run `panda session verify`; restore a backup",
                )
            if anchor_seq != self._tail[0] - 1:
                raise SessionIntegrityError(
                    f"cannot recover {self.path}: the log is {self._tail[0] - anchor_seq} "
                    "events away from its anchor",
                    hint="only a single unanchored event is recoverable; restore a backup",
                )
            report = self.verify()
            if {issue.code for issue in report.errors} - {"anchor_mismatch"}:
                raise SessionIntegrityError(
                    f"cannot recover {self.path}: the hash chain is not intact",
                    hint="run `panda session verify`; restore a backup",
                )
            events = list(self.events())
            if events[-2].hash != anchor_hash:
                raise SessionIntegrityError(
                    f"cannot recover {self.path}: the anchor does not name the previous event"
                )
            adopted = events[-1]
            problem = _implausible_crash_event(events[-2], adopted)
            if problem:
                # R2-227: recovery adopts the one event a crash can leave unanchored;
                # it must not legitimise a backdated or approval event added by hand.
                raise SessionIntegrityError(
                    f"cannot recover {self.path}: {problem}",
                    hint="the log was edited, not interrupted; restore a backup",
                )
            self._append_locked(
                "session.recovered",
                {
                    "adopted_seq": adopted.seq,
                    "adopted_hash": adopted.hash,
                    "adopted_type": adopted.type,
                    "adopted_ts": adopted.ts,
                    "anchor_seq": anchor_seq,
                },
                check_anchor=False,
            )
            return True

    def close(self, status: str = "completed", data: dict[str, Any] | None = None) -> SessionEvent:
        if self.is_closed():
            raise ConfigError(f"session already closed: {self.session_id}")
        payload = {"status": status, "summary": _jsonable(data or {})}
        return self.append("session.end", payload)

    # ---- verification ----
    def events(self) -> Iterator[SessionEvent]:
        with open(self.path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise SessionIntegrityError(f"blank line at {self.path}:{line_number}")
                yield SessionEvent.from_record(json.loads(line))

    def verify(self) -> SessionVerification:
        errors: list[VerifyIssue] = []
        expected_seq = 1
        expected_prev: str | None = None
        last_hash: str | None = None
        count = 0
        seen_end = False

        try:
            handle = open(self.path, encoding="utf-8")  # noqa: SIM115 - closed by `with` below
        except OSError as exc:
            return SessionVerification(
                session_id=self.session_id,
                path=str(self.path),
                ok=False,
                count=0,
                last_hash=None,
                anchor_ok=None,
                errors=[VerifyIssue("unreadable", str(exc))],
            )

        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    errors.append(VerifyIssue("blank_line", "blank line", line_number))
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(
                        VerifyIssue("invalid_json", f"invalid JSON: {exc.msg}", line_number)
                    )
                    continue
                if not isinstance(record, dict):
                    errors.append(
                        VerifyIssue("invalid_record", "record is not an object", line_number)
                    )
                    continue
                missing = [
                    key
                    for key in ("schema", "seq", "ts", "session_id", "type", "data", "prev", "hash")
                    if key not in record
                ]
                if missing:
                    errors.append(
                        VerifyIssue(
                            "missing_fields",
                            f"missing fields: {', '.join(missing)}",
                            line_number,
                        )
                    )
                    continue
                if record.get("schema") != SESSION_SCHEMA:
                    errors.append(
                        VerifyIssue(
                            "schema",
                            f"unexpected schema: {record.get('schema')}",
                            line_number,
                        )
                    )
                if record.get("session_id") != self.session_id:
                    errors.append(
                        VerifyIssue(
                            "session_id",
                            f"expected {self.session_id}, got {record.get('session_id')}",
                            line_number,
                        )
                    )
                try:
                    seq = int(record["seq"])
                except (TypeError, ValueError):
                    errors.append(VerifyIssue("seq", "seq is not an integer", line_number))
                    continue
                if seq != expected_seq:
                    errors.append(
                        VerifyIssue("seq", f"expected seq {expected_seq}, got {seq}", line_number)
                    )
                if record.get("prev") != expected_prev:
                    errors.append(
                        VerifyIssue("prev", "prev does not match previous hash", line_number)
                    )
                if not isinstance(record.get("data"), dict):
                    errors.append(VerifyIssue("data", "data is not an object", line_number))
                if not EVENT_TYPE_RE.match(str(record.get("type", ""))):
                    errors.append(
                        VerifyIssue("type", f"invalid type: {record.get('type')}", line_number)
                    )
                if not HASH_RE.match(str(record.get("hash", ""))):
                    errors.append(VerifyIssue("hash_format", "invalid hash", line_number))
                else:
                    recomputed = event_hash({key: record[key] for key in record if key != "hash"})
                    if recomputed != record["hash"]:
                        errors.append(VerifyIssue("hash", "hash mismatch", line_number))
                if seen_end:
                    errors.append(
                        VerifyIssue("event_after_end", "event after session.end", line_number)
                    )
                if record.get("type") == "session.end":
                    seen_end = True
                expected_prev = str(record.get("hash")) if record.get("hash") else None
                last_hash = expected_prev
                expected_seq = seq + 1
                count += 1

        anchor_ok: bool | None = None
        anchor = None
        with contextlib.suppress(SessionIntegrityError):
            anchor = self._load_anchor()
        if anchor is None:
            if self.anchor_path.exists():
                anchor_ok = False
                errors.append(VerifyIssue("anchor_invalid", "session anchor is unreadable"))
            else:
                anchor_ok = None
        else:
            anchor_ok = anchor.get("seq") == count and anchor.get("hash") == last_hash
            if not anchor_ok:
                errors.append(
                    VerifyIssue(
                        "anchor_mismatch",
                        "anchor does not match the last event (truncated or rewritten log)",
                    )
                )
            if anchor.get("session_id") != self.session_id:
                errors.append(VerifyIssue("anchor_session", "anchor belongs to another session"))

        if count == 0:
            errors.append(VerifyIssue("empty", "session has no events"))

        return SessionVerification(
            session_id=self.session_id,
            path=str(self.path),
            ok=not errors,
            count=count,
            last_hash=last_hash,
            anchor_ok=anchor_ok,
            errors=errors,
        )

    def is_closed(self) -> bool:
        # O(1): the tail is tracked in memory and refreshed on size change (PERF-001).
        try:
            self._refresh_tail()
        except (OSError, SessionIntegrityError):
            return False
        return self._tail_type == "session.end"

    def summary(self) -> SessionSummary:
        label = ""
        started_at = None
        ended_at = None
        count = 0
        closed = False
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                if record.get("type") == "session.start":
                    started_at = record.get("ts")
                    data = record.get("data") or {}
                    label = str(data.get("label") or "")
                if record.get("type") == "session.end":
                    ended_at = record.get("ts")
                    closed = True
        return SessionSummary(
            session_id=self.session_id,
            path=str(self.path),
            label=label,
            started_at=started_at,
            ended_at=ended_at,
            events=count,
            closed=closed,
            size_bytes=self.path.stat().st_size if self.path.exists() else 0,
        )


def list_sessions(sessions_dir: Path, *, redact_enabled: bool = True) -> list[SessionSummary]:
    if not sessions_dir.is_dir():
        return []
    paths = sorted(
        (p for p in sessions_dir.glob("s_*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    summaries: list[SessionSummary] = []
    for path in paths:
        with contextlib.suppress(ConfigError, OSError, SessionIntegrityError):
            summaries.append(SessionLog.open(path, redact_enabled=redact_enabled).summary())
    return summaries


_NEVER_ADOPTED = frozenset({"approval.request", "approval.decision", "policy.decision"})


def _implausible_crash_event(previous: Any, adopted: Any) -> str | None:
    """Why an unanchored event cannot be the result of a crash, or None."""

    if adopted.type in _NEVER_ADOPTED:
        return f"an unanchored {adopted.type} is never adopted"
    if adopted.ts < previous.ts:
        return "the unanchored event is dated before the event it follows"
    now = utc_now()
    if adopted.ts > now and adopted.ts[:16] > now[:16]:
        return "the unanchored event is dated in the future"
    return None


"""Pluggable storage for the audit log.

The governance guarantees — append-only, hash-chained, tamper-evident — live in
``DecisionLogger``. This module is only about *where the lines are kept*. A store
is a dumb, ordered, append-only sink of opaque strings; it knows nothing about
hashing or schemas. That separation is the point: a durable backend (Postgres, or
an object store with an append log) can implement this tiny interface without
touching the governance logic, so the same tamper-evidence holds whatever the
storage. The default is a JSONL file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable


@runtime_checkable
class AuditStore(Protocol):
    """Append-only storage of opaque audit lines, preserved in write order."""

    def append(self, line: str) -> None:
        """Append one record. Must be durable before returning, for a real store."""
        ...

    def read_lines(self) -> List[str]:
        """All records, oldest first."""
        ...

    def last_line(self) -> Optional[str]:
        """The most recently appended record, or None if the store is empty."""
        ...


class JsonlAuditStore:
    """The default store: one record per line in a UTF-8 JSONL file.

    Append-only and grep-able, with no dependency beyond the standard library.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_lines(self) -> List[str]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def last_line(self) -> Optional[str]:
        last: Optional[str] = None
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        last = ln.strip()
        return last


class InMemoryAuditStore:
    """An ephemeral store for tests and demos. NOT durable — lines vanish on exit."""

    def __init__(self) -> None:
        self._lines: List[str] = []

    def append(self, line: str) -> None:
        self._lines.append(line)

    def read_lines(self) -> List[str]:
        return list(self._lines)

    def last_line(self) -> Optional[str]:
        return self._lines[-1] if self._lines else None


class SqliteAuditStore:
    """A durable, transactional store backed by stdlib ``sqlite3``.

    A real database with no server to run. It proves the AuditStore interface
    works over SQL and de-risks a Postgres backend, which has the same shape: one
    append-only table, insertion order preserved by an autoincrement sequence. The
    governance logic in ``DecisionLogger`` is unchanged, so the tamper-evidence
    holds exactly as it does over the JSONL file.

    A fresh connection is opened per call: simple, thread-safe (the data layer
    refreshes on background threads), and ample for the audit-write volume.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def append(self, line: str) -> None:
        conn = self._connect()
        try:
            conn.execute("INSERT INTO audit(line) VALUES (?)", (line,))
            conn.commit()
        finally:
            conn.close()

    def read_lines(self) -> List[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT line FROM audit ORDER BY seq").fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def last_line(self) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT line FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return row[0] if row else None

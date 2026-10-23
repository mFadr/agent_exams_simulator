"""SQLite connection management."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import apply_migrations

log = logging.getLogger(__name__)


class DatabaseError(Exception):
    """User-facing database error."""


class Database:
    """Thread-safe wrapper: one connection per thread, WAL journal, FK enforcement."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._check_integrity()
        with self.transaction() as conn:
            apply_migrations(conn)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit; transactions are explicit via transaction().
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def _check_integrity(self) -> None:
        if self.path == ":memory:" or not Path(self.path).exists():
            return
        try:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] != "ok":
                log.error("Database integrity check reported: %s", row[0])
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(
                f"The database file is damaged or not a database ({exc}). Restore a backup from Application Settings "
                f"or move '{self.path}' away to start fresh."
            ) from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write block - rolled back entirely on any exception."""
        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def backup_to(self, target: Path) -> None:
        """Consistent online copy using SQLite's backup API."""
        dest = sqlite3.connect(str(target))
        try:
            with self._write_lock:
                self.conn.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

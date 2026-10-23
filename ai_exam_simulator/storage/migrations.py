"""Versioned schema migrations (tracked with PRAGMA user_version)."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE questions (
        id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        qtype TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        text TEXT NOT NULL,
        data TEXT NOT NULL,
        provenance TEXT NOT NULL,
        review_status TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        deleted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_questions_subject ON questions(subject, topic);
    CREATE TABLE question_versions (
        question_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        data TEXT NOT NULL,
        saved_at TEXT NOT NULL,
        PRIMARY KEY (question_id, version)
    );
    CREATE TABLE collections (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE collection_items (
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        PRIMARY KEY (collection_id, question_id)
    );
    CREATE TABLE templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        config TEXT NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE exams (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        subject TEXT NOT NULL,
        mode TEXT NOT NULL,
        config TEXT NOT NULL,
        question_ids TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE attempts (
        id TEXT PRIMARY KEY,
        exam_id TEXT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
        attempt_number INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        deadline TEXT,
        submitted_at TEXT,
        submit_reason TEXT,
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_attempts_status ON attempts(status);
    CREATE TABLE attempt_results (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        summary TEXT NOT NULL,
        percentage REAL NOT NULL,
        passed INTEGER,
        provisional INTEGER NOT NULL DEFAULT 0,
        graded_at TEXT NOT NULL
    );
    CREATE TABLE question_results (
        attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        question_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        qtype TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        points REAL NOT NULL,
        max_points REAL NOT NULL,
        status TEXT NOT NULL,
        method TEXT NOT NULL,
        needs_review INTEGER NOT NULL DEFAULT 0,
        error_tags TEXT NOT NULL DEFAULT '',
        time_spent REAL NOT NULL DEFAULT 0,
        confidence INTEGER,
        flagged INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL,
        graded_at TEXT NOT NULL,
        PRIMARY KEY (attempt_id, question_id)
    );
    CREATE INDEX idx_qr_topic ON question_results(subject, topic);
    CREATE TABLE documents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        format TEXT NOT NULL,
        status TEXT NOT NULL,
        page_count INTEGER,
        char_count INTEGER NOT NULL DEFAULT 0,
        warnings TEXT NOT NULL DEFAULT '[]',
        subject TEXT NOT NULL DEFAULT '',
        imported_at TEXT NOT NULL
    );
    CREATE TABLE document_sections (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        title TEXT NOT NULL,
        page_start INTEGER,
        page_end INTEGER,
        text TEXT NOT NULL,
        included INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX idx_sections_doc ON document_sections(document_id, idx);
    CREATE TABLE ai_usage_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        operation TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        kind TEXT NOT NULL,
        success INTEGER NOT NULL,
        error_kind TEXT NOT NULL DEFAULT '',
        chars_sent INTEGER NOT NULL DEFAULT 0,
        duration_s REAL NOT NULL DEFAULT 0
    );
    """,
    # 2 - free-form profile notes and per-topic overrides
    """
    CREATE TABLE learning_profile (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    ALTER TABLE exams ADD COLUMN source_document_ids TEXT NOT NULL DEFAULT '[]';
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations inside the caller's transaction. Returns the new version."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"The database was created by a newer version of the application (schema {version} > {SCHEMA_VERSION})."
        )
    for idx in range(version, SCHEMA_VERSION):
        log.info("Applying database migration %d", idx + 1)
        for statement in _split(MIGRATIONS[idx]):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {idx + 1}")
    return SCHEMA_VERSION


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]

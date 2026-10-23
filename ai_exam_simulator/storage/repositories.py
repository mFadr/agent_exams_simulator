"""Repositories: all SQL lives here, the rest of the app works with models."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from pydantic import ValidationError

from ..exams.exam_engine import SessionState, SessionStatus
from ..exams.question_models import ExamConfig, Question, new_id, utcnow
from ..grading.models import ExamSummary, QuestionResult
from .database import Database

log = logging.getLogger(__name__)


def _now() -> str:
    return utcnow().isoformat()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ============================================================================ questions


class QuestionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row_to_question(row: Any) -> Question | None:
        try:
            return Question.model_validate_json(row["data"])
        except ValidationError as exc:
            log.error("Skipping malformed question record %s: %s", row["id"], exc.errors()[0].get("msg"))
            return None

    def save(self, q: Question, *, record_history: bool = True) -> Question:
        """Insert or update. Updates keep the previous version in ``question_versions``."""
        now = _now()
        with self.db.transaction() as c:
            existing = c.execute("SELECT data, version FROM questions WHERE id = ?", (q.id,)).fetchone()
            if existing is None:
                q.version = max(1, q.version)
                c.execute(
                    "INSERT INTO questions (id, subject, topic, qtype, difficulty, text, data, provenance, review_status, "
                    "tags, version, deleted, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (q.id, q.subject, q.topic, q.type.value, q.difficulty.value, q.text, "", q.provenance.value,
                     q.review_status.value, ",".join(q.tags), q.version, now, now),
                )
            else:
                old_version = int(existing["version"])
                if record_history:
                    c.execute(
                        "INSERT OR REPLACE INTO question_versions (question_id, version, data, saved_at) VALUES (?,?,?,?)",
                        (q.id, old_version, existing["data"], now),
                    )
                    q.version = old_version + 1
            c.execute(
                "UPDATE questions SET subject=?, topic=?, qtype=?, difficulty=?, text=?, data=?, provenance=?, "
                "review_status=?, tags=?, version=?, deleted=0, updated_at=? WHERE id=?",
                (q.subject, q.topic, q.type.value, q.difficulty.value, q.text, q.model_dump_json(),
                 q.provenance.value, q.review_status.value, ",".join(q.tags), q.version, now, q.id),
            )
        return q

    def save_many(self, questions: Iterable[Question]) -> int:
        n = 0
        for q in questions:
            self.save(q, record_history=False)
            n += 1
        return n

    def upsert_if_changed(self, q: Question) -> Question:
        """Insert new questions; update (keeping version history) only when content changed."""
        existing = self.get(q.id)
        if existing is None:
            return self.save(q, record_history=False)
        strip = {"version", "created_at"}
        if existing.model_dump(mode="json", exclude=strip) == q.model_dump(mode="json", exclude=strip):
            return existing
        return self.save(q, record_history=True)

    def get(self, qid: str) -> Question | None:
        row = self.db.query_one("SELECT id, data FROM questions WHERE id = ?", (qid,))
        return self._row_to_question(row) if row else None

    def get_many(self, ids: list[str]) -> list[Question]:
        if not ids:
            return []
        out: dict[str, Question] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.db.query(f"SELECT id, data FROM questions WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
            for r in rows:
                q = self._row_to_question(r)
                if q:
                    out[q.id] = q
        return [out[i] for i in ids if i in out]

    def delete(self, qid: str) -> None:
        """Soft delete: completed exams keep their own snapshots, so history is unaffected."""
        with self.db.transaction() as c:
            c.execute("UPDATE questions SET deleted = 1, updated_at = ? WHERE id = ?", (_now(), qid))
            c.execute("DELETE FROM collection_items WHERE question_id = ?", (qid,))

    def versions(self, qid: str) -> list[tuple[int, str, Question | None]]:
        rows = self.db.query("SELECT version, saved_at, data, question_id AS id FROM question_versions WHERE question_id = ? ORDER BY version DESC", (qid,))
        return [(r["version"], r["saved_at"], self._row_to_question(r)) for r in rows]

    def search(
        self,
        *,
        text: str = "",
        subject: str = "",
        topic: str = "",
        qtype: str = "",
        difficulty: str = "",
        provenance: str = "",
        review_status: str = "",
        tag: str = "",
        collection_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Question], int]:
        where = ["q.deleted = 0"]
        params: list[Any] = []
        join = ""
        if collection_id:
            join = "JOIN collection_items ci ON ci.question_id = q.id AND ci.collection_id = ?"
            params.append(collection_id)
        for col, val in (("subject", subject), ("topic", topic), ("qtype", qtype), ("difficulty", difficulty),
                         ("provenance", provenance), ("review_status", review_status)):
            if val:
                where.append(f"q.{col} = ?")
                params.append(val)
        if text:
            where.append("(q.text LIKE ? OR q.topic LIKE ? OR q.tags LIKE ?)")
            like = f"%{text}%"
            params += [like, like, like]
        if tag:
            where.append("(',' || q.tags || ',') LIKE ?")
            params.append(f"%,{tag},%")
        sql_where = " AND ".join(where)
        total = self.db.query_one(f"SELECT COUNT(*) FROM questions q {join} WHERE {sql_where}", tuple(params))[0]
        rows = self.db.query(
            f"SELECT q.id, q.data FROM questions q {join} WHERE {sql_where} ORDER BY q.updated_at DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        )
        return [q for q in (self._row_to_question(r) for r in rows) if q], int(total)

    def subjects(self) -> list[str]:
        return [r[0] for r in self.db.query("SELECT DISTINCT subject FROM questions WHERE deleted = 0 ORDER BY subject")]

    def topics(self, subject: str = "") -> list[str]:
        if subject:
            rows = self.db.query("SELECT DISTINCT topic FROM questions WHERE deleted = 0 AND subject = ? ORDER BY topic", (subject,))
        else:
            rows = self.db.query("SELECT DISTINCT topic FROM questions WHERE deleted = 0 ORDER BY topic")
        return [r[0] for r in rows]

    def tags(self) -> list[str]:
        out: set[str] = set()
        for r in self.db.query("SELECT tags FROM questions WHERE deleted = 0 AND tags != ''"):
            out.update(t for t in r[0].split(",") if t)
        return sorted(out)

    def candidates(
        self, subject: str, topics: list[str] | None = None, types: list[str] | None = None,
        difficulties: list[str] | None = None, exclude_ids: Iterable[str] = (), limit: int = 2000,
    ) -> list[Question]:
        where = ["deleted = 0", "subject = ?", "review_status != 'flagged'"]
        params: list[Any] = [subject]
        for col, vals in (("topic", topics), ("qtype", types), ("difficulty", difficulties)):
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                params += list(vals)
        rows = self.db.query(f"SELECT id, data FROM questions WHERE {' AND '.join(where)} LIMIT ?", tuple(params + [limit]))
        excl = set(exclude_ids)
        return [q for q in (self._row_to_question(r) for r in rows) if q and q.id not in excl]

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) FROM questions WHERE deleted = 0")[0])

    # -------------------------------------------------------------- collections
    def create_collection(self, name: str, description: str = "") -> str:
        cid = new_id("col")
        with self.db.transaction() as c:
            c.execute("INSERT INTO collections (id, name, description, created_at) VALUES (?,?,?,?)", (cid, name.strip(), description, _now()))
        return cid

    def list_collections(self) -> list[dict]:
        rows = self.db.query(
            "SELECT c.id, c.name, c.description, COUNT(ci.question_id) AS n FROM collections c "
            "LEFT JOIN collection_items ci ON ci.collection_id = c.id GROUP BY c.id ORDER BY c.name"
        )
        return [dict(r) for r in rows]

    def add_to_collection(self, cid: str, qids: Iterable[str]) -> None:
        with self.db.transaction() as c:
            c.executemany("INSERT OR IGNORE INTO collection_items (collection_id, question_id) VALUES (?,?)", [(cid, q) for q in qids])

    def remove_from_collection(self, cid: str, qids: Iterable[str]) -> None:
        with self.db.transaction() as c:
            c.executemany("DELETE FROM collection_items WHERE collection_id = ? AND question_id = ?", [(cid, q) for q in qids])

    def delete_collection(self, cid: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM collections WHERE id = ?", (cid,))


# ============================================================================ templates


class TemplateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, name: str, config: ExamConfig, builtin: bool = False, template_id: str | None = None) -> str:
        tid = template_id or new_id("tpl")
        with self.db.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO templates (id, name, config, builtin, created_at) VALUES (?,?,?,?,?)",
                (tid, name.strip() or config.title, config.model_dump_json(), int(builtin), _now()),
            )
        return tid

    def list(self) -> list[tuple[str, str, ExamConfig, bool]]:
        out = []
        for r in self.db.query("SELECT id, name, config, builtin FROM templates ORDER BY builtin DESC, name"):
            try:
                out.append((r["id"], r["name"], ExamConfig.model_validate_json(r["config"]), bool(r["builtin"])))
            except ValidationError:
                log.error("Skipping malformed template %s", r["id"])
        return out

    def delete(self, tid: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM templates WHERE id = ? AND builtin = 0", (tid,))

    def has_builtin(self) -> bool:
        return self.db.query_one("SELECT 1 FROM templates WHERE builtin = 1") is not None


# ============================================================================ exams & attempts


@dataclass
class AttemptRow:
    attempt_id: str
    exam_id: str
    title: str
    subject: str
    mode: str
    status: str
    attempt_number: int
    started_at: str
    submitted_at: str | None
    percentage: float | None
    passed: bool | None
    provisional: bool


class ExamRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- exams
    def create_exam(self, config: ExamConfig, questions: list[Question]) -> str:
        eid = new_id("exam")
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO exams (id, title, subject, mode, config, question_ids, created_at, source_document_ids) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eid, config.title, config.subject, config.mode.value, config.model_dump_json(),
                 json.dumps([q.id for q in questions]), _now(), json.dumps(config.source_document_ids)),
            )
        return eid

    def get_exam(self, eid: str) -> tuple[ExamConfig, list[str]] | None:
        r = self.db.query_one("SELECT config, question_ids FROM exams WHERE id = ?", (eid,))
        if not r:
            return None
        return ExamConfig.model_validate_json(r["config"]), json.loads(r["question_ids"])

    def attempt_count(self, eid: str) -> int:
        return int(self.db.query_one("SELECT COUNT(*) FROM attempts WHERE exam_id = ?", (eid,))[0])

    # ---------------------------------------------------------------- sessions
    def save_state(self, state: SessionState) -> None:
        data = state.model_dump_json()
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO attempts (id, exam_id, attempt_number, status, started_at, deadline, submitted_at, submit_reason, state, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, deadline=excluded.deadline, "
                "submitted_at=excluded.submitted_at, submit_reason=excluded.submit_reason, state=excluded.state, updated_at=excluded.updated_at",
                (state.attempt_id, state.exam_id, state.attempt_number, state.status.value, _iso(state.started_at),
                 _iso(state.deadline), _iso(state.submitted_at), state.submit_reason.value if state.submit_reason else None,
                 data, _now()),
            )

    def load_state(self, attempt_id: str) -> SessionState | None:
        r = self.db.query_one("SELECT state FROM attempts WHERE id = ?", (attempt_id,))
        if not r:
            return None
        try:
            return SessionState.model_validate_json(r["state"])
        except ValidationError as exc:
            log.error("Attempt %s has a malformed saved state: %s", attempt_id, exc.errors()[0].get("msg"))
            return None

    def open_attempt_ids(self) -> list[str]:
        return [r[0] for r in self.db.query("SELECT id FROM attempts WHERE status = ? ORDER BY started_at DESC", (SessionStatus.IN_PROGRESS.value,))]

    def ungraded_attempt_ids(self) -> list[str]:
        return [r[0] for r in self.db.query("SELECT id FROM attempts WHERE status = ? ORDER BY started_at DESC", (SessionStatus.SUBMITTED.value,))]

    def delete_attempt(self, attempt_id: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))

    # ---------------------------------------------------------------- results
    def save_results(self, state: SessionState, results: list[QuestionResult], summary: ExamSummary) -> None:
        now = _now()
        by_q = {q.id: q for q in state.questions}
        with self.db.transaction() as c:
            c.execute("DELETE FROM question_results WHERE attempt_id = ?", (state.attempt_id,))
            for r in results:
                q = by_q[r.question_id]
                ans = state.answers.get(q.id)
                c.execute(
                    "INSERT INTO question_results (attempt_id, question_id, subject, topic, qtype, difficulty, points, max_points, "
                    "status, method, needs_review, error_tags, time_spent, confidence, flagged, result, graded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (state.attempt_id, q.id, q.subject, q.topic, q.type.value, q.difficulty.value, r.points, r.max_points,
                     r.status.value, r.method.value, int(r.needs_review), ",".join(r.error_tags),
                     ans.time_spent_s if ans else 0, ans.confidence if ans else None, int(bool(ans and ans.flagged)),
                     r.model_dump_json(), now),
                )
            c.execute(
                "INSERT OR REPLACE INTO attempt_results (attempt_id, summary, percentage, passed, provisional, graded_at) VALUES (?,?,?,?,?,?)",
                (state.attempt_id, summary.model_dump_json(), summary.percentage,
                 None if summary.passed is None else int(summary.passed), int(summary.provisional), now),
            )
            c.execute("UPDATE attempts SET status = ? WHERE id = ?", (SessionStatus.GRADED.value, state.attempt_id))
            state.status = SessionStatus.GRADED
            c.execute("UPDATE attempts SET state = ? WHERE id = ?", (state.model_dump_json(), state.attempt_id))

    def load_results(self, attempt_id: str) -> tuple[ExamSummary, list[QuestionResult]] | None:
        r = self.db.query_one("SELECT summary FROM attempt_results WHERE attempt_id = ?", (attempt_id,))
        if not r:
            return None
        rows = self.db.query("SELECT result FROM question_results WHERE attempt_id = ?", (attempt_id,))
        return ExamSummary.model_validate_json(r["summary"]), [QuestionResult.model_validate_json(x["result"]) for x in rows]

    def list_attempts(self, *, subject: str = "", date_from: str = "", date_to: str = "", limit: int = 500) -> list[AttemptRow]:
        where = ["1=1"]
        params: list[Any] = []
        if subject:
            where.append("e.subject = ?")
            params.append(subject)
        if date_from:
            where.append("a.started_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("a.started_at <= ?")
            params.append(date_to)
        rows = self.db.query(
            "SELECT a.id, a.exam_id, e.title, e.subject, e.mode, a.status, a.attempt_number, a.started_at, a.submitted_at, "
            "ar.percentage, ar.passed, ar.provisional FROM attempts a JOIN exams e ON e.id = a.exam_id "
            f"LEFT JOIN attempt_results ar ON ar.attempt_id = a.id WHERE {' AND '.join(where)} "
            "ORDER BY a.started_at DESC LIMIT ?",
            tuple(params + [limit]),
        )
        return [
            AttemptRow(r["id"], r["exam_id"], r["title"], r["subject"], r["mode"], r["status"], r["attempt_number"],
                       r["started_at"], r["submitted_at"], r["percentage"],
                       None if r["passed"] is None else bool(r["passed"]), bool(r["provisional"] or 0))
            for r in rows
        ]

    def incorrect_question_ids(self, subject: str = "", limit: int = 200) -> list[str]:
        """Questions most recently answered incorrectly/partially (for revision exams)."""
        params: list[Any] = []
        where = "status IN ('incorrect','partial','unanswered')"
        if subject:
            where += " AND subject = ?"
            params.append(subject)
        rows = self.db.query(
            f"SELECT question_id, MAX(graded_at) AS g FROM question_results WHERE {where} GROUP BY question_id ORDER BY g DESC LIMIT ?",
            tuple(params + [limit]),
        )
        return [r[0] for r in rows]

    def delete_all_history(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM question_results")
            c.execute("DELETE FROM attempt_results")
            c.execute("DELETE FROM attempts")
            c.execute("DELETE FROM exams")
            c.execute("DELETE FROM learning_profile")


# ============================================================================ documents


class DocumentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, doc: dict, sections: list[dict]) -> str:
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO documents (id, name, path, file_hash, format, status, page_count, char_count, warnings, subject, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (doc["id"], doc["name"], doc["path"], doc["file_hash"], doc["format"], doc["status"], doc.get("page_count"),
                 doc.get("char_count", 0), json.dumps(doc.get("warnings", [])), doc.get("subject", ""), _now()),
            )
            for s in sections:
                c.execute(
                    "INSERT INTO document_sections (id, document_id, idx, title, page_start, page_end, text, included) VALUES (?,?,?,?,?,?,?,1)",
                    (s["id"], doc["id"], s["idx"], s["title"], s.get("page_start"), s.get("page_end"), s["text"]),
                )
        return doc["id"]

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT id, name, path, file_hash, format, status, page_count, char_count, warnings, subject, imported_at FROM documents ORDER BY imported_at DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["warnings"] = json.loads(d["warnings"] or "[]")
            out.append(d)
        return out

    def get(self, doc_id: str) -> dict | None:
        r = self.db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if not r:
            return None
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"] or "[]")
        return d

    def find_by_hash(self, file_hash: str) -> dict | None:
        r = self.db.query_one("SELECT id, name FROM documents WHERE file_hash = ?", (file_hash,))
        return dict(r) if r else None

    def sections(self, doc_id: str, included_only: bool = False) -> list[dict]:
        sql = "SELECT id, document_id, idx, title, page_start, page_end, text, included FROM document_sections WHERE document_id = ?"
        if included_only:
            sql += " AND included = 1"
        return [dict(r) for r in self.db.query(sql + " ORDER BY idx", (doc_id,))]

    def set_section_included(self, section_id: str, included: bool) -> None:
        with self.db.transaction() as c:
            c.execute("UPDATE document_sections SET included = ? WHERE id = ?", (int(included), section_id))

    def set_subject(self, doc_id: str, subject: str) -> None:
        with self.db.transaction() as c:
            c.execute("UPDATE documents SET subject = ? WHERE id = ?", (subject, doc_id))

    def delete(self, doc_id: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# ============================================================================ usage log & profile


class UsageLogRepository:
    """Metadata-only log of AI calls (no prompts, answers or keys)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def __call__(self, *, operation: str, provider: str, model: str, kind: str, success: bool,
                 error_kind: str, chars_sent: int, duration_s: float) -> None:
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO ai_usage_log (at, operation, provider, model, kind, success, error_kind, chars_sent, duration_s) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), operation, provider, model, kind, int(success), error_kind, chars_sent, round(duration_s, 2)),
            )

    def recent(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM ai_usage_log ORDER BY id DESC LIMIT ?", (limit,))]

    def totals(self) -> dict:
        r = self.db.query_one(
            "SELECT COUNT(*) AS calls, SUM(success) AS ok, SUM(CASE WHEN kind='cloud' THEN 1 ELSE 0 END) AS cloud_calls, "
            "SUM(chars_sent) AS cloud_chars FROM ai_usage_log"
        )
        return {k: int(r[k] or 0) for k in ("calls", "ok", "cloud_calls", "cloud_chars")}

    def clear(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM ai_usage_log")


class ProfileRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        r = self.db.query_one("SELECT value FROM learning_profile WHERE key = ?", (key,))
        return json.loads(r[0]) if r else default

    def set(self, key: str, value: Any) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT OR REPLACE INTO learning_profile (key, value, updated_at) VALUES (?,?,?)", (key, json.dumps(value), _now()))

    def all(self) -> dict[str, Any]:
        return {r[0]: json.loads(r[1]) for r in self.db.query("SELECT key, value FROM learning_profile")}

    def delete(self, key: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM learning_profile WHERE key = ?", (key,))

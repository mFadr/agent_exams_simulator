"""Aggregated performance statistics (all deterministic, computed from stored results)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..storage.database import Database

DIMENSIONS = {"subject": "qr.subject", "topic": "qr.topic", "qtype": "qr.qtype", "difficulty": "qr.difficulty"}


@dataclass
class ResultRecord:
    attempt_id: str
    started_at: str
    subject: str
    topic: str
    qtype: str
    difficulty: str
    points: float
    max_points: float
    status: str
    error_tags: list[str]
    time_spent: float
    confidence: int | None
    question_id: str

    @property
    def fraction(self) -> float:
        return max(0.0, self.points) / self.max_points if self.max_points else 0.0

    @property
    def is_wrong(self) -> bool:
        return self.status in ("incorrect", "unanswered") or (self.status == "partial" and self.fraction < 0.5)


class Analytics:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _filters(subject: str, date_from: str, date_to: str) -> tuple[str, list[Any]]:
        where = ["qr.status != 'pending_review'"]
        params: list[Any] = []
        if subject:
            where.append("qr.subject = ?")
            params.append(subject)
        if date_from:
            where.append("a.started_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("a.started_at <= ?")
            params.append(date_to)
        return " AND ".join(where), params

    def records(self, subject: str = "", date_from: str = "", date_to: str = "") -> list[ResultRecord]:
        where, params = self._filters(subject, date_from, date_to)
        rows = self.db.query(
            "SELECT qr.attempt_id, a.started_at, qr.subject, qr.topic, qr.qtype, qr.difficulty, qr.points, qr.max_points, "
            "qr.status, qr.error_tags, qr.time_spent, qr.confidence, qr.question_id FROM question_results qr "
            f"JOIN attempts a ON a.id = qr.attempt_id JOIN exams e ON e.id = a.exam_id WHERE {where} AND e.mode != 'practice' "
            "ORDER BY a.started_at",
            tuple(params),
        )
        return [
            ResultRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], [t for t in r[9].split(",") if t], r[10], r[11], r[12])
            for r in rows
        ]

    def score_history(self, subject: str = "", date_from: str = "", date_to: str = "") -> list[dict]:
        where = ["ar.percentage IS NOT NULL"]
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
            "SELECT a.id, a.started_at, e.title, e.subject, e.mode, ar.percentage, ar.passed, ar.provisional FROM attempts a "
            "JOIN exams e ON e.id = a.exam_id JOIN attempt_results ar ON ar.attempt_id = a.id "
            f"WHERE {' AND '.join(where)} ORDER BY a.started_at",
            tuple(params),
        )
        return [dict(r) for r in rows]

    def breakdown(self, dim: str, subject: str = "", date_from: str = "", date_to: str = "") -> list[dict]:
        col = DIMENSIONS[dim]
        where, params = self._filters(subject, date_from, date_to)
        rows = self.db.query(
            f"SELECT {col} AS key, COUNT(*) AS n, SUM(MAX(qr.points, 0)) AS pts, SUM(qr.max_points) AS mx, "
            "SUM(CASE WHEN qr.status='correct' THEN 1 ELSE 0 END) AS correct, "
            "SUM(CASE WHEN qr.status IN ('incorrect','unanswered') THEN 1 ELSE 0 END) AS wrong, "
            "AVG(qr.time_spent) AS avg_time FROM question_results qr JOIN attempts a ON a.id = qr.attempt_id "
            f"WHERE {where} GROUP BY {col} ORDER BY {col}",
            tuple(params),
        )
        out = []
        for r in rows:
            mx = r["mx"] or 0
            out.append({
                "key": r["key"], "n": r["n"], "points": r["pts"] or 0, "max_points": mx,
                "pct": 100.0 * (r["pts"] or 0) / mx if mx else 0.0, "correct": r["correct"], "wrong": r["wrong"],
                "avg_time": r["avg_time"] or 0.0,
            })
        return out

    def status_counts(self, subject: str = "", date_from: str = "", date_to: str = "") -> dict[str, int]:
        where, params = self._filters(subject, date_from, date_to)
        rows = self.db.query(
            f"SELECT qr.status, COUNT(*) FROM question_results qr JOIN attempts a ON a.id = qr.attempt_id WHERE {where} GROUP BY qr.status",
            tuple(params),
        )
        return {r[0]: r[1] for r in rows}

    def subjects(self) -> list[str]:
        return [r[0] for r in self.db.query("SELECT DISTINCT subject FROM exams ORDER BY subject")]

    def totals(self) -> dict:
        r = self.db.query_one(
            "SELECT COUNT(*) AS n, AVG(percentage) AS avg, MAX(percentage) AS best FROM attempt_results"
        )
        return {"attempts": r["n"] or 0, "average": r["avg"], "best": r["best"]}

"""Authoritative, deterministic score calculation."""

from __future__ import annotations

from ..grading.models import ExamSummary, QuestionResult, ResultStatus, TopicBreakdown
from .question_models import QUESTION_TYPE_LABELS, ExamConfig, Question


def _bucket(store: dict[str, TopicBreakdown], key: str, r: QuestionResult) -> None:
    b = store.setdefault(key, TopicBreakdown(key=key))
    b.points += r.points
    b.max_points += r.max_points
    b.count += 1
    b.correct += r.status == ResultStatus.CORRECT


def compute_summary(questions: list[Question], results: list[QuestionResult], config: ExamConfig) -> ExamSummary:
    by_id = {r.question_id: r for r in results}
    total = 0.0
    max_total = 0.0
    counts = {s: 0 for s in ResultStatus}
    topics: dict[str, TopicBreakdown] = {}
    types: dict[str, TopicBreakdown] = {}
    diffs: dict[str, TopicBreakdown] = {}
    for q in questions:
        r = by_id.get(q.id)
        if r is None:
            raise ValueError(f"No grading result for question {q.id}")
        total += r.points
        max_total += r.max_points
        counts[r.status] += 1
        _bucket(topics, q.topic, r)
        _bucket(types, QUESTION_TYPE_LABELS[q.type], r)
        _bucket(diffs, q.difficulty.value, r)
    if config.scoring.floor_total_at_zero:
        total = max(0.0, total)
    pct = 100.0 * total / max_total if max_total else 0.0
    provisional = counts[ResultStatus.PENDING_REVIEW] > 0
    passed = None if config.is_practice else pct + 1e-9 >= config.pass_threshold
    for d in (topics, types, diffs):
        for b in d.values():
            b.points = round(b.points, 3)
            b.max_points = round(b.max_points, 3)
    return ExamSummary(
        points=round(total, 3), max_points=round(max_total, 3), percentage=round(pct, 2), passed=passed,
        pass_threshold=None if config.is_practice else config.pass_threshold,
        correct=counts[ResultStatus.CORRECT], incorrect=counts[ResultStatus.INCORRECT],
        partial=counts[ResultStatus.PARTIAL], unanswered=counts[ResultStatus.UNANSWERED],
        pending_review=counts[ResultStatus.PENDING_REVIEW],
        by_topic=sorted(topics.values(), key=lambda b: b.key),
        by_type=sorted(types.values(), key=lambda b: b.key),
        by_difficulty=sorted(diffs.values(), key=lambda b: ["easy", "medium", "hard", "expert"].index(b.key)),
        provisional=provisional,
    )

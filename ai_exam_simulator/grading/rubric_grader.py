"""Deterministic aggregation of rubric judgements into a score."""

from __future__ import annotations

from ..ai.answer_evaluator import RubricEvaluation, effective_criteria
from ..exams.question_models import Question
from .models import CriterionScore, GradingMethod, QuestionResult, status_for

REVIEW_CONFIDENCE_THRESHOLD = 0.6


def aggregate_rubric(q: Question, ev: RubricEvaluation) -> QuestionResult:
    """Sum criterion points (clamped), scale to the question's max points."""
    criteria = effective_criteria(q)
    total_rubric = sum(c.points for c in criteria) or 1.0
    scale = q.max_points / total_rubric
    by_id = {c.id: c for c in ev.criteria}
    scores: list[CriterionScore] = []
    raw = 0.0
    for c in criteria:
        ce = by_id.get(c.id)
        awarded = max(0.0, min(c.points, ce.awarded if ce else 0.0))
        raw += awarded
        scores.append(CriterionScore(
            criterion_id=c.id, description=c.description, awarded=round(awarded * scale, 3),
            max_points=round(c.points * scale, 3),
            satisfied=ce.satisfied if ce and ce.satisfied is not None else awarded >= c.points - 1e-9,
            comment=ce.comment if ce else "Not assessed",
        ))
    points = round(raw * scale, 3)
    needs_review = ev.confidence < REVIEW_CONFIDENCE_THRESHOLD or bool(ev.anomalies)
    tags = [ev.error_type] if ev.error_type and ev.error_type != "none" else []
    feedback = ev.overall_feedback.strip()
    if ev.anomalies:
        feedback += "\n\nGrading notes: " + " ".join(ev.anomalies)
    return QuestionResult(
        question_id=q.id, points=points, max_points=q.max_points, status=status_for(points, q.max_points),
        method=GradingMethod.AI_RUBRIC, feedback=feedback, criteria=scores, error_tags=tags,
        misconception=ev.misconception, confidence=ev.confidence, needs_review=needs_review, provider=ev.provider,
    )

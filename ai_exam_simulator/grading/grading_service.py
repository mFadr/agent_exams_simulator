"""Grading orchestration: deterministic first, AI rubric evaluation only where needed."""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..ai.answer_evaluator import AnswerEvaluator
from ..ai.provider_interface import AIError, CancelToken, OperationCancelled
from ..exams.question_models import AnswerRecord, OBJECTIVE_TYPES, Question, QuestionType, ScoringRules
from .code_grader import grade_with_tests, static_check
from .models import GradingMethod, QuestionResult, ResultStatus, status_for
from .numerical_grader import extract_final_answer, grade_numerical, grade_symbolic
from .objective_grader import grade_objective, grade_short_text, unanswered
from .rubric_grader import aggregate_rubric

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]


def _pending(q: Question, feedback: str, tags: list[str] | None = None) -> QuestionResult:
    return QuestionResult(
        question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.PENDING_REVIEW,
        method=GradingMethod.NONE, feedback=feedback, needs_review=True, error_tags=tags or [],
    )


class GradingService:
    def __init__(
        self,
        evaluator: AnswerEvaluator | None = None,
        code_execution_enabled: Callable[[], bool] = lambda: False,
        code_timeout_s: Callable[[], int] = lambda: 5,
    ) -> None:
        self.evaluator = evaluator
        self._code_enabled = code_execution_enabled
        self._code_timeout = code_timeout_s

    # ------------------------------------------------------------------ deterministic
    def grade_deterministic(self, q: Question, value: Any, rules: ScoringRules) -> tuple[QuestionResult, bool]:
        """Grade without AI. Returns (result, ai_candidate).

        ``ai_candidate`` means an AI rubric evaluation could improve or complete the grading.
        """
        if not AnswerRecord(question_id=q.id, value=value).is_answered():
            return unanswered(q), False
        t = q.type
        if t in OBJECTIVE_TYPES:
            return grade_objective(q, value, rules), False
        if t == QuestionType.NUMERICAL:
            return grade_numerical(q, value, rules), False
        if t == QuestionType.SHORT_ANSWER:
            r = grade_short_text(q, value)
            if r is not None:
                return r, False
            if q.accepted_answers:
                res = QuestionResult(
                    question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
                    method=GradingMethod.DETERMINISTIC, needs_review=True,
                    feedback="The answer does not match any accepted answer. If it is an equivalent wording, "
                             "AI evaluation or a manual override can award credit.",
                )
                return res, True
            return _pending(q, "Awaiting rubric evaluation (AI or manual)."), True
        if t == QuestionType.MATH_PROBLEM:
            has_rubric = bool(q.rubric and q.rubric.criteria)
            if q.numerical is not None:
                r = grade_numerical(q, extract_final_answer(str(value)), rules)
                if r.status == ResultStatus.CORRECT or not has_rubric:
                    return r, False
                return r, True
            r2 = grade_symbolic(q, value)
            if r2 is not None:
                if r2.status == ResultStatus.CORRECT or not has_rubric:
                    return r2, False
                return r2, True  # rubric may award partial credit for correct steps
            if has_rubric:
                return _pending(q, "Final answer could not be read automatically; awaiting rubric evaluation."), True
            return _pending(q, "Final answer could not be read automatically. Please review manually."), False
        if t == QuestionType.PROGRAMMING:
            code = str(value)
            ok, notes = static_check(code, q.function_name)
            tags = [] if ok else ["syntax_error"]
            if q.test_cases and self._code_enabled() and ok:
                r = grade_with_tests(q, code, self._code_timeout())
                if r is not None:
                    if notes:
                        r.feedback += "\nStatic checks: " + " ".join(notes)
                    return r, False
                notes.append("Isolated execution is not available on this system.")
            elif q.test_cases and not self._code_enabled():
                notes.append("Code execution is disabled (Application Settings), so test cases were not run.")
            if not ok:
                return QuestionResult(
                    question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
                    method=GradingMethod.STATIC_CODE, feedback="Static check: " + " ".join(notes), error_tags=tags,
                    needs_review=True,
                ), True
            return _pending(q, "Static check passed. " + " ".join(notes) + " Awaiting rubric evaluation (AI or manual)."), True
        if t in (QuestionType.ESSAY, QuestionType.CASE_STUDY):
            return _pending(q, "Awaiting rubric evaluation (AI or manual)."), True
        raise ValueError(f"Unsupported question type {t}")

    # ------------------------------------------------------------------ AI
    def grade_with_ai(
        self, q: Question, value: Any, base: QuestionResult, *, cloud_consent: bool = False,
        cancel: CancelToken | None = None,
    ) -> QuestionResult:
        """Rubric evaluation. On failure returns ``base`` annotated - never a fabricated score."""
        if self.evaluator is None:
            return base
        try:
            ev = self.evaluator.evaluate(q, str(value), cloud_consent=cloud_consent, cancel=cancel)
        except OperationCancelled:
            raise
        except AIError as exc:
            log.warning("AI grading failed for %s: %s", q.id, exc)
            out = base.model_copy(deep=True)
            out.needs_review = True
            out.feedback = (out.feedback + f"\nAI evaluation failed: {exc}").strip()
            return out
        r = aggregate_rubric(q, ev)
        # Keep deterministic evidence: e.g. a symbolic check that found the final answer wrong.
        if base.method in (GradingMethod.SYMBOLIC, GradingMethod.NUMERICAL, GradingMethod.STATIC_CODE, GradingMethod.DETERMINISTIC) and base.status != ResultStatus.PENDING_REVIEW:
            r.feedback = f"Automatic check: {base.feedback}\n\nRubric evaluation: {r.feedback}"
            r.error_tags = list(dict.fromkeys(base.error_tags + r.error_tags))
        return r

    def grade_all(
        self,
        questions: list[Question],
        answers: dict[str, AnswerRecord],
        rules: ScoringRules,
        *,
        use_ai: bool = True,
        cloud_consent: bool = False,
        cancel: CancelToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[QuestionResult]:
        results: list[QuestionResult] = []
        candidates: list[int] = []
        for q in questions:
            rec = answers.get(q.id)
            value = rec.value if rec else None
            r, ai_cand = self.grade_deterministic(q, value, rules)
            results.append(r)
            if ai_cand:
                candidates.append(len(results) - 1)
        if use_ai and self.evaluator is not None:
            for n, idx in enumerate(candidates):
                if cancel and cancel.cancelled:
                    break
                q = questions[idx]
                if progress:
                    progress(f"AI evaluating answer {n + 1}/{len(candidates)}…", n, len(candidates))
                rec = answers.get(q.id)
                try:
                    results[idx] = self.grade_with_ai(q, rec.value if rec else "", results[idx],
                                                      cloud_consent=cloud_consent, cancel=cancel)
                except OperationCancelled:
                    break
        return results

    @staticmethod
    def override(result: QuestionResult, points: float, note: str) -> QuestionResult:
        """Manual grading decision by the user. Always wins over automatic results."""
        points = max(-result.max_points, min(result.max_points, float(points)))
        out = result.model_copy(deep=True)
        out.points = round(points, 3)
        out.status = status_for(points, result.max_points)
        out.method = GradingMethod.MANUAL
        out.overridden = True
        out.override_note = note.strip()
        out.needs_review = False
        return out

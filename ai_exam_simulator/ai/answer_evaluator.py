"""AI-assisted evaluation of open answers, explanations and hints.

The model only proposes per-criterion judgements; the score itself is computed
deterministically by :mod:`ai_exam_simulator.grading.rubric_grader`.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config.settings import AIOperation
from ..exams.question_models import Question, QuestionType, RubricCriterion
from . import prompt_templates as pt
from .hybrid_router import HybridRouter, RouteDecision
from .json_utils import as_object, extract_json
from .provider_interface import CancelToken, MalformedResponseError

log = logging.getLogger(__name__)

ERROR_TYPES = {"none", "conceptual", "factual", "incomplete", "reasoning", "calculation", "clarity", "off_topic"}


class CriterionEval(BaseModel):
    id: str
    awarded: float
    satisfied: bool | None = None
    comment: str = ""


class RubricEvaluation(BaseModel):
    criteria: list[CriterionEval]
    overall_feedback: str = ""
    misconception: str = ""
    error_type: str = "none"
    confidence: float = Field(default=0.5)
    provider: str = ""
    anomalies: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: object) -> float:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if f > 1:  # model answered in percent
            f = f / 100.0
        return max(0.0, min(1.0, f))

    @field_validator("error_type", mode="before")
    @classmethod
    def _etype(cls, v: object) -> str:
        s = str(v or "none").lower().strip()
        return s if s in ERROR_TYPES else "none"


def effective_criteria(q: Question) -> list[RubricCriterion]:
    """The rubric used for AI evaluation (synthesised when a question has none)."""
    if q.rubric and q.rubric.criteria:
        return list(q.rubric.criteria)
    mp = q.max_points
    if q.type == QuestionType.PROGRAMMING:
        return [
            RubricCriterion(id="correctness", description="Solution is functionally correct for the specified task", points=round(mp * 0.6, 3)),
            RubricCriterion(id="edge_cases", description="Handles edge cases (empty input, boundaries, invalid values)", points=round(mp * 0.2, 3), dimension="completeness"),
            RubricCriterion(id="quality", description="Clear, idiomatic and reasonably efficient code", points=round(mp * 0.2, 3), dimension="clarity"),
        ]
    reference = q.model_answer or " | ".join(q.accepted_answers) or q.correct_answer_display()
    return [RubricCriterion(id="meaning", description=f"Answer conveys the same meaning as the reference: {reference}", points=mp)]


def parse_rubric_evaluation(text: str, criteria: list[RubricCriterion]) -> RubricEvaluation:
    data = as_object(extract_json(text))
    try:
        ev = RubricEvaluation.model_validate(
            {k: v for k, v in data.items() if k in RubricEvaluation.model_fields and k != "anomalies"}
        )
    except ValidationError as exc:
        raise MalformedResponseError(f"Grading response has an invalid structure: {exc.errors()[0].get('msg')}") from exc
    by_id = {c.id: c for c in ev.criteria}
    if len(criteria) == 1 and len(ev.criteria) == 1 and criteria[0].id not in by_id:
        by_id = {criteria[0].id: ev.criteria[0]}  # tolerate an id typo when unambiguous
    missing = [c.id for c in criteria if c.id not in by_id]
    if missing:
        raise MalformedResponseError(f"Grading response is incomplete (missing criteria: {', '.join(missing)}).")
    fixed = []
    for c in criteria:
        ce = by_id[c.id]
        aw = ce.awarded
        if aw < 0 or aw > c.points + 1e-9:
            ev.anomalies.append(f"Criterion '{c.id}': model awarded {aw:g} outside 0-{c.points:g}; clamped.")
            aw = max(0.0, min(c.points, aw))
        fixed.append(CriterionEval(id=c.id, awarded=aw, satisfied=ce.satisfied, comment=ce.comment))
    ev.criteria = fixed
    return ev


class AnswerEvaluator:
    def __init__(self, router: HybridRouter) -> None:
        self.router = router

    @staticmethod
    def operation_for(q: Question) -> AIOperation:
        return AIOperation.EVALUATE_CODE if q.type == QuestionType.PROGRAMMING else AIOperation.EVALUATE_WRITTEN

    def plan(self, q: Question, override: Literal["local", "cloud"] | None = None) -> RouteDecision:
        return self.router.plan(self.operation_for(q), complexity="high" if q.type in (QuestionType.ESSAY, QuestionType.CASE_STUDY) else "normal", override=override)

    def evaluate(
        self,
        q: Question,
        answer: str,
        *,
        decision: RouteDecision | None = None,
        cloud_consent: bool = False,
        cancel: CancelToken | None = None,
    ) -> RubricEvaluation:
        criteria = effective_criteria(q)
        crit_json = [
            {"id": c.id, "description": c.description, "max_points": c.points, "dimension": c.dimension,
             "expected_concepts": c.expected_concepts}
            for c in criteria
        ]
        messages = [
            {"role": "system", "content": pt.EVALUATION_SYSTEM},
            {"role": "user", "content": pt.evaluation_prompt(q, answer, crit_json)},
        ]
        op = self.operation_for(q)
        last_exc: Exception | None = None
        for _ in range(2):  # one re-ask when the structure is invalid
            resp = self.router.execute(op, messages, decision=decision or self.plan(q), cloud_consent=cloud_consent,
                                       json_mode=True, cancel=cancel)
            try:
                ev = parse_rubric_evaluation(resp.text, criteria)
                ev.provider = f"{resp.provider_name} [{resp.model}]"
                if resp.notes:
                    ev.anomalies.extend(resp.notes)
                return ev
            except MalformedResponseError as exc:
                last_exc = exc
                log.warning("Rubric evaluation malformed, retrying: %s", exc)
        raise MalformedResponseError(f"The AI did not return a usable grading result: {last_exc}")

    def explain(
        self, q: Question, student_answer: str, correct_display: str, feedback: str, *,
        cloud_consent: bool = False, cancel: CancelToken | None = None,
        override: Literal["local", "cloud"] | None = None,
    ) -> dict[str, str]:
        resp = self.router.execute(
            AIOperation.EXPLAIN,
            [{"role": "system", "content": pt.TUTOR_SYSTEM},
             {"role": "user", "content": pt.explanation_prompt(q, student_answer, correct_display, feedback)}],
            cloud_consent=cloud_consent, json_mode=True, cancel=cancel, override=override,
        )
        data = as_object(extract_json(resp.text))
        out = {k: str(data.get(k) or "").strip() for k in ("explanation", "misconception", "recommendation")}
        if not out["explanation"]:
            raise MalformedResponseError("The AI returned no explanation.")
        out["provider"] = f"{resp.provider_name} [{resp.model}]"
        return out

    def hint(self, q: Question, *, cloud_consent: bool = False, cancel: CancelToken | None = None) -> str:
        resp = self.router.execute(
            AIOperation.HINT,
            [{"role": "system", "content": pt.TUTOR_SYSTEM}, {"role": "user", "content": pt.hint_prompt(q)}],
            cloud_consent=cloud_consent, json_mode=True, cancel=cancel,
        )
        hint = str(as_object(extract_json(resp.text)).get("hint") or "").strip()
        if not hint:
            raise MalformedResponseError("The AI returned no hint.")
        return hint

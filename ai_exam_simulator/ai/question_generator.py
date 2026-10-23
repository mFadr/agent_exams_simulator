"""AI question generation with structured validation, de-duplication and verification."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import ValidationError

from ..config.settings import AIOperation, AISettings
from ..exams.question_models import (
    CodeTestCase,
    Difficulty,
    MatchPair,
    NumericalAnswer,
    Option,
    Provenance,
    Question,
    QuestionType,
    ReviewStatus,
    Rubric,
    RubricCriterion,
    SourceRef,
    new_id,
)
from ..exams.validation import is_duplicate_of_any, validate_question
from . import prompt_templates as pt
from .hybrid_router import HybridRouter, RouteDecision
from .json_utils import as_object, extract_json
from .provider_interface import AIError, CancelToken, MalformedResponseError, OperationCancelled

log = logging.getLogger(__name__)

BATCH_SIZE = 4


@dataclass
class SourceExcerpt:
    id: str  # short label used in the prompt, e.g. "S1"
    text: str
    ref: SourceRef


@dataclass
class GenerationRequest:
    subject: str
    topics: list[str]
    difficulty: Difficulty
    count: int
    types: list[QuestionType]
    language: str = "English"
    avoid_texts: list[str] = field(default_factory=list)
    focus_notes: str = ""
    excerpts: list[SourceExcerpt] = field(default_factory=list)
    override: Literal["local", "cloud"] | None = None

    @property
    def operation(self) -> AIOperation:
        return AIOperation.GENERATE_FROM_SOURCE if self.excerpts else AIOperation.GENERATE_QUESTIONS

    @property
    def complexity(self) -> Literal["low", "normal", "high"]:
        return "high" if self.difficulty in (Difficulty.HARD, Difficulty.EXPERT) else "normal"


@dataclass
class GenerationResult:
    questions: list[Question] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    providers_used: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None


ProgressCallback = Callable[[str, int, int], None]


class QuestionGenerator:
    def __init__(self, router: HybridRouter, settings_getter: Callable[[], AISettings]) -> None:
        self.router = router
        self._settings = settings_getter

    def plan(self, req: GenerationRequest) -> RouteDecision:
        return self.router.plan(
            req.operation, complexity=req.complexity, includes_source_material=bool(req.excerpts), override=req.override
        )

    def generate(
        self,
        req: GenerationRequest,
        *,
        cloud_consent: bool = False,
        cancel: CancelToken | None = None,
        progress: ProgressCallback | None = None,
        existing: list[Question] | None = None,
    ) -> GenerationResult:
        result = GenerationResult()
        accepted: list[Question] = []
        pool = list(existing or [])
        max_rounds = math.ceil(req.count / BATCH_SIZE) + 3
        rounds = 0
        decision = self.plan(req)
        while len(accepted) < req.count and rounds < max_rounds:
            rounds += 1
            need = min(BATCH_SIZE, req.count - len(accepted))
            if progress:
                progress(f"Generating questions ({len(accepted)}/{req.count})…", len(accepted), req.count)
            types = _rotate(req.types, len(accepted), need)
            excerpts = _rotate(req.excerpts, len(accepted), 3) if req.excerpts else []
            prompt = pt.generation_prompt(
                subject=req.subject, topics=req.topics, difficulty=req.difficulty.value, count=need, types=types,
                language=req.language, avoid=req.avoid_texts + [q.text for q in pool + accepted],
                focus_notes=req.focus_notes, excerpts=[(e.id, e.text) for e in excerpts],
            )
            try:
                resp = self.router.execute(
                    req.operation,
                    [{"role": "system", "content": pt.GENERATION_SYSTEM}, {"role": "user", "content": prompt}],
                    decision=decision, cloud_consent=cloud_consent, json_mode=True, cancel=cancel,
                )
            except OperationCancelled:
                result.error = "Cancelled."
                break
            except AIError as exc:
                result.error = str(exc)
                break
            result.providers_used.add(f"{resp.provider_name} [{resp.model}]")
            result.notes.extend(n for n in resp.notes if n not in result.notes)
            try:
                raw_items = as_object(extract_json(resp.text), "questions").get("questions")
                if not isinstance(raw_items, list):
                    raise MalformedResponseError("missing 'questions' list")
            except MalformedResponseError as exc:
                result.rejected.append(f"Unusable model output ({exc}).")
                continue
            provider_label = f"{resp.provider_name} [{resp.model}]"
            for raw in raw_items:
                if len(accepted) >= req.count:
                    break
                q, problem = self._convert(raw, req, excerpts, provider_label)
                if q is None:
                    result.rejected.append(problem)
                    continue
                dup = is_duplicate_of_any(q, pool + accepted)
                if dup is not None:
                    result.rejected.append(f"Duplicate of an existing question: {q.text[:70]}…")
                    continue
                accepted.append(q)
        if len(accepted) < req.count and result.error is None:
            result.error = (
                f"Only {len(accepted)} of {req.count} questions passed validation. "
                "Try again, use a stronger model, or reduce the number of question types."
            )
        result.questions = accepted
        if accepted and self._settings().verify_generated_questions and not (cancel and cancel.cancelled):
            self.verify(accepted, cloud_consent=cloud_consent, cancel=cancel, progress=progress, override=req.override)
        return result

    # ------------------------------------------------------------------ conversion
    def _convert(
        self, raw: Any, req: GenerationRequest, excerpts: list[SourceExcerpt], provider_label: str
    ) -> tuple[Question | None, str]:
        if not isinstance(raw, dict):
            return None, "Model returned a non-object question entry."
        try:
            q = raw_to_question(raw, req.subject, req.topics, req.difficulty)
        except (ValueError, ValidationError, TypeError, KeyError) as exc:
            snippet = str(raw.get("question", ""))[:60]
            return None, f"Invalid structure ({_short_err(exc)}): {snippet}"
        if q.type not in req.types:
            return None, f"Wrong question type '{q.type.value}' (not requested): {q.text[:60]}"
        report = validate_question(q)
        if not report.ok:
            return None, f"Failed validation ({'; '.join(report.errors)}): {q.text[:60]}"
        q.validation_notes = report.warnings + [f"Generated by {provider_label}"]
        if report.warnings:
            q.review_status = ReviewStatus.FLAGGED
        q.provenance = Provenance.SOURCE_DERIVED if excerpts else Provenance.AI_GENERATED
        if excerpts:
            sid = str(raw.get("source_id") or "").strip()
            match = next((e for e in excerpts if e.id == sid), None)
            if match:
                q.source_ref = match.ref
            else:
                q.validation_notes.append("Model did not name a valid source excerpt; source link uncertain.")
                q.review_status = ReviewStatus.FLAGGED
                q.source_ref = excerpts[0].ref.model_copy(update={"excerpt": ""})
        return q, ""

    # ------------------------------------------------------------------ verification
    def verify(
        self,
        questions: list[Question],
        *,
        cloud_consent: bool = False,
        cancel: CancelToken | None = None,
        progress: ProgressCallback | None = None,
        override: Literal["local", "cloud"] | None = None,
    ) -> None:
        """Second, independent pass. Sets review_status; never silently marks as verified."""
        from ..grading.numerical_grader import MathParseError, parse_number_with_unit, within_tolerance

        decision = self.router.plan(AIOperation.VERIFY_QUESTIONS, override=override)
        for i, q in enumerate(questions):
            if cancel and cancel.cancelled:
                return
            if progress:
                progress(f"Verifying question {i + 1}/{len(questions)}…", i, len(questions))
            if q.review_status == ReviewStatus.FLAGGED:
                continue
            # deterministic tool check for numerical answers
            if q.type == QuestionType.NUMERICAL and q.numerical and q.numerical.solution_expression:
                try:
                    v, _ = parse_number_with_unit(q.numerical.solution_expression)
                    if within_tolerance(v, q.numerical.value, q.numerical.abs_tolerance, max(q.numerical.rel_tolerance, 1e-6)):
                        q.review_status = ReviewStatus.TOOL_VERIFIED
                        q.validation_notes.append("Answer re-computed with SymPy from the solution expression.")
                    else:
                        q.review_status = ReviewStatus.FLAGGED
                        q.validation_notes.append(
                            f"Answer key {q.numerical.value:g} disagrees with the solution expression ({v:g})."
                        )
                    continue
                except MathParseError:
                    q.validation_notes.append("Solution expression could not be evaluated.")
            if q.type not in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_SELECT, QuestionType.TRUE_FALSE, QuestionType.NUMERICAL):
                continue
            if not decision.chain:
                continue
            try:
                resp = self.router.execute(
                    AIOperation.VERIFY_QUESTIONS,
                    [{"role": "system", "content": pt.GENERATION_SYSTEM}, {"role": "user", "content": pt.verification_prompt(q)}],
                    decision=decision, cloud_consent=cloud_consent, json_mode=True, cancel=cancel,
                )
                data = as_object(extract_json(resp.text))
            except OperationCancelled:
                return
            except AIError as exc:
                q.validation_notes.append(f"Verification pass failed: {exc}")
                continue
            agrees, note = _compare_verification(q, data)
            if data.get("ambiguous") is True:
                q.review_status = ReviewStatus.FLAGGED
                q.validation_notes.append("Second pass judged the question ambiguous. " + str(data.get("issue") or ""))
            elif agrees is True:
                q.review_status = ReviewStatus.AI_VERIFIED
                q.validation_notes.append(f"Independent AI pass agreed with the answer key ({resp.model}).")
            elif agrees is False:
                q.review_status = ReviewStatus.FLAGGED
                q.validation_notes.append("Independent AI pass DISAGREED with the answer key - review it. " + note)


def _compare_verification(q: Question, data: dict) -> tuple[bool | None, str]:
    ans = data.get("answer")
    try:
        if q.type == QuestionType.TRUE_FALSE:
            got = _to_bool(ans)
            return (("true" if got else "false") in q.correct_option_ids), f"(model said {got})"
        if q.type == QuestionType.NUMERICAL and q.numerical:
            from ..grading.numerical_grader import parse_number_with_unit, within_tolerance

            v, _ = parse_number_with_unit(str(ans))
            return within_tolerance(v, q.numerical.value, q.numerical.abs_tolerance, max(q.numerical.rel_tolerance, 0.02)), f"(model computed {v:g})"
        idx = ans if isinstance(ans, list) else [ans]
        chosen = {q.options[int(i)].id for i in idx if str(i).lstrip("-").isdigit() and 0 <= int(i) < len(q.options)}
        texts = ", ".join(o.text for o in q.options if o.id in chosen)
        return chosen == set(q.correct_option_ids), f"(model chose: {texts})"
    except Exception:
        return None, ""


# ---------------------------------------------------------------------------- raw → Question


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, list) and v:
        v = v[0]
    s = str(v).strip().lower()
    if s in ("true", "t", "yes", "1", "pravda", "correct"):
        return True
    if s in ("false", "f", "no", "0", "nepravda", "incorrect"):
        return False
    raise ValueError(f"not a boolean: {v!r}")


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x) for x in v if str(x).strip()]


def _float(v: Any, default: float) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


_TYPE_ALIASES = {
    "mcq": "single_choice", "multiple_choice": "single_choice", "single": "single_choice",
    "multi_select": "multiple_select", "multiple_response": "multiple_select", "checkbox": "multiple_select",
    "truefalse": "true_false", "true/false": "true_false", "boolean": "true_false",
    "short": "short_answer", "fill_in_the_blank": "fill_blank", "fill_in_blank": "fill_blank", "cloze": "fill_blank",
    "numeric": "numerical", "calculation": "numerical", "math": "math_problem", "code": "programming",
    "coding": "programming", "case": "case_study", "scenario": "case_study", "match": "matching",
    "sequence": "ordering", "order": "ordering", "sequencing": "ordering", "extended_response": "essay",
}


def _correct_indices(raw_correct: Any, options: list[str]) -> list[int]:
    items = raw_correct if isinstance(raw_correct, list) else [raw_correct]
    out: list[int] = []
    for it in items:
        if isinstance(it, bool):
            continue
        if isinstance(it, (int, float)) or (isinstance(it, str) and it.strip().isdigit()):
            out.append(int(it))
        elif isinstance(it, str):
            s = it.strip()
            if len(s) == 1 and s.upper() in "ABCDEFGH":
                out.append("ABCDEFGH".index(s.upper()))
            else:
                norm = [o.strip().lower() for o in options]
                if s.lower() in norm:
                    out.append(norm.index(s.lower()))
    return sorted(set(out))


def raw_to_question(raw: dict, subject: str, topics: list[str], difficulty: Difficulty) -> Question:
    """Map the simplified JSON format the model produces into a validated :class:`Question`."""
    t = str(raw.get("type", "")).strip().lower().replace("-", "_").replace(" ", "_")
    qtype = QuestionType(_TYPE_ALIASES.get(t, t))
    text = str(raw.get("question") or raw.get("text") or "").strip()
    diff_raw = str(raw.get("difficulty") or difficulty.value).lower()
    diff = Difficulty(diff_raw) if diff_raw in Difficulty._value2member_map_ else difficulty
    topic = str(raw.get("topic") or (topics[0] if topics else subject)).strip() or subject
    points = max(1.0, min(20.0, _float(raw.get("points"), 1.0)))

    q: dict[str, Any] = dict(
        id=new_id(), subject=subject, topic=topic, text=text, type=qtype, difficulty=diff, max_points=points,
        explanation=str(raw.get("explanation") or ""), hint=str(raw.get("hint") or ""),
        skills=_str_list(raw.get("skills"))[:5], model_answer=str(raw.get("answer") or raw.get("model_answer") or ""),
    )
    if qtype in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_SELECT):
        opts = [o.get("text") if isinstance(o, dict) else o for o in (raw.get("options") or [])]
        opts = [str(o).strip() for o in opts if str(o).strip()]
        opts = [re.sub(r"^[A-Ha-h][\).:]\s+", "", o) for o in opts]  # strip "A) " prefixes
        idx = _correct_indices(raw.get("correct"), opts)
        if any(i < 0 or i >= len(opts) for i in idx):
            raise ValueError("correct index out of range")
        q["options"] = [Option(id=f"o{i}", text=o) for i, o in enumerate(opts)]
        q["correct_option_ids"] = [f"o{i}" for i in idx]
    elif qtype == QuestionType.TRUE_FALSE:
        q["options"] = [Option(id="true", text="True"), Option(id="false", text="False")]
        q["correct_option_ids"] = ["true" if _to_bool(raw.get("correct")) else "false"]
    elif qtype == QuestionType.SHORT_ANSWER:
        q["accepted_answers"] = _str_list(raw.get("accepted_answers"))
        if not q["accepted_answers"] and q["model_answer"] and len(q["model_answer"]) < 60:
            q["accepted_answers"] = [q["model_answer"]]
    elif qtype == QuestionType.FILL_BLANK:
        q["text"] = re.sub(r"_{3,}", "___", text)
        blanks = raw.get("blanks") or []
        q["blank_answers"] = [_str_list(b) for b in blanks]
    elif qtype == QuestionType.NUMERICAL:
        unit = raw.get("unit")
        q["numerical"] = NumericalAnswer(
            value=float(raw["numeric_answer"]),
            rel_tolerance=max(0.0, min(0.2, _float(raw.get("tolerance_percent"), 1.0) / 100.0)),
            unit=str(unit).strip() if unit not in (None, "", "null", "none") else None,
            solution_expression=str(raw.get("solution_expression") or "") or None,
        )
    elif qtype == QuestionType.MATH_PROBLEM:
        q["expected_expression"] = str(raw.get("expected_expression") or "").strip() or None
    elif qtype == QuestionType.MATCHING:
        pairs = raw.get("pairs") or []
        q["match_pairs"] = [MatchPair(id=f"p{i}", left=str(p["left"]), right=str(p["right"])) for i, p in enumerate(pairs)]
    elif qtype == QuestionType.ORDERING:
        q["order_items"] = _str_list(raw.get("order") or raw.get("items"))
    elif qtype == QuestionType.PROGRAMMING:
        q["function_name"] = str(raw.get("function_name") or "").strip() or None
        q["code_context"] = str(raw.get("code") or "")
        q["test_cases"] = [
            CodeTestCase(call=str(tc["call"]), expected=str(tc["expected"]) if not isinstance(tc["expected"], str) else tc["expected"])
            for tc in (raw.get("tests") or []) if isinstance(tc, dict) and "call" in tc and "expected" in tc
        ]
        for tc in q["test_cases"]:
            import ast

            try:
                ast.literal_eval(tc.expected)
            except (ValueError, SyntaxError):
                tc.expected = repr(tc.expected)

    rubric_raw = raw.get("rubric")
    if isinstance(rubric_raw, list) and rubric_raw:
        criteria = []
        for i, c in enumerate(rubric_raw):
            if isinstance(c, dict):
                desc = str(c.get("criterion") or c.get("description") or "").strip()
                pts = _float(c.get("points"), 1.0)
                if desc and pts > 0:
                    criteria.append(RubricCriterion(id=f"c{i}", description=desc, points=pts,
                                                    expected_concepts=_str_list(c.get("concepts"))))
        if criteria:
            q["rubric"] = Rubric(criteria=criteria, common_misconceptions=_str_list(raw.get("misconceptions")))
            if qtype in (QuestionType.ESSAY, QuestionType.CASE_STUDY, QuestionType.MATH_PROBLEM):
                q["max_points"] = round(sum(c.points for c in criteria), 2)
    return Question(**q)


def _rotate(items: list, start: int, n: int) -> list:
    if not items:
        return []
    if len(items) <= n:
        return list(items)
    return [items[(start + i) % len(items)] for i in range(n)]


def _short_err(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        e = exc.errors()[0]
        return f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}"
    return str(exc)[:80]

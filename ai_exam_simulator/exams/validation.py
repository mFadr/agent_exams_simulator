"""Deterministic quality control for questions (user-written or AI-generated).

Errors make a question unusable (it must not enter an exam); warnings mark it
for review but allow use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .question_models import (
    BLANK_MARKER,
    CHOICE_TYPES,
    OPEN_ENDED_TYPES,
    Question,
    QuestionType,
)

DUPLICATE_THRESHOLD = 0.85
_GIVEAWAY_PHRASES = ("all of the above", "none of the above", "both a and b")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def light_normalize(text: str) -> str:
    """Case/whitespace-insensitive form that keeps signs and symbols (for matching/ordering items)."""
    return " ".join(str(text).lower().split())


def similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] combining sequence ratio and token Jaccard overlap."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(seq, jac)


def find_duplicates(questions: list[Question], threshold: float = DUPLICATE_THRESHOLD) -> list[tuple[int, int, float]]:
    """Return index pairs (i, j) with i < j whose question texts are near-duplicates."""
    pairs = []
    for i in range(len(questions)):
        for j in range(i + 1, len(questions)):
            s = similarity(questions[i].text, questions[j].text)
            if s >= threshold:
                pairs.append((i, j, s))
    return pairs


def is_duplicate_of_any(q: Question, existing: list[Question], threshold: float = DUPLICATE_THRESHOLD) -> Question | None:
    for other in existing:
        if other.id != q.id and similarity(q.text, other.text) >= threshold:
            return other
    return None


def count_blanks(text: str) -> int:
    return len(re.findall(r"_{3,}", text))


def validate_question(q: Question) -> ValidationReport:
    r = ValidationReport()
    if len(q.text) < 8:
        r.errors.append("Question text is missing or too short.")
    if not q.subject or not q.topic:
        r.errors.append("Subject and topic are required.")
    if q.max_points <= 0:
        r.errors.append("Maximum points must be positive.")

    t = q.type
    if t in CHOICE_TYPES:
        _validate_choice(q, r)
    elif t == QuestionType.SHORT_ANSWER:
        if not q.accepted_answers and not (q.rubric and q.rubric.criteria) and not q.model_answer:
            r.errors.append("Short-answer question needs accepted answers, a model answer or a rubric.")
    elif t == QuestionType.FILL_BLANK:
        n = count_blanks(q.text)
        if n == 0:
            r.errors.append(f"Fill-in-the-blank text must contain at least one blank marker ({BLANK_MARKER}).")
        if len(q.blank_answers) != n:
            r.errors.append(f"Found {n} blank(s) in the text but {len(q.blank_answers)} answer list(s).")
        if any(not [a for a in answers if a.strip()] for answers in q.blank_answers):
            r.errors.append("Every blank needs at least one accepted answer.")
    elif t == QuestionType.NUMERICAL:
        if q.numerical is None:
            r.errors.append("Numerical question needs a numeric answer.")
        elif q.numerical.abs_tolerance < 0 or q.numerical.rel_tolerance < 0:
            r.errors.append("Tolerances must not be negative.")
    elif t == QuestionType.MATH_PROBLEM:
        if not q.expected_expression and not (q.rubric and q.rubric.criteria) and q.numerical is None:
            r.errors.append("Math problem needs an expected expression, a numeric answer or a rubric.")
        if q.expected_expression:
            from ..grading.numerical_grader import parse_math_expression, MathParseError

            try:
                parse_math_expression(q.expected_expression)
            except MathParseError as exc:
                r.errors.append(f"Expected expression cannot be parsed: {exc}")
    elif t == QuestionType.MATCHING:
        if len(q.match_pairs) < 2:
            r.errors.append("Matching question needs at least two pairs.")
        lefts = [light_normalize(p.left) for p in q.match_pairs]
        rights = [light_normalize(p.right) for p in q.match_pairs]
        if len(set(lefts)) != len(lefts) or len(set(rights)) != len(rights):
            r.errors.append("Matching items must be unique on both sides.")
        if any(not p.left.strip() or not p.right.strip() for p in q.match_pairs):
            r.errors.append("Matching items must not be empty.")
    elif t == QuestionType.ORDERING:
        if len(q.order_items) < 2:
            r.errors.append("Ordering question needs at least two items.")
        if len({light_normalize(i) for i in q.order_items}) != len(q.order_items):
            r.errors.append("Ordering items must be unique.")
    elif t == QuestionType.PROGRAMMING:
        if not q.test_cases and not (q.rubric and q.rubric.criteria):
            r.errors.append("Programming question needs test cases or a rubric.")
        if q.test_cases and not q.function_name:
            r.errors.append("Programming question with test cases needs the function name.")
        for tc in q.test_cases:
            if not tc.call.strip() or not tc.expected.strip():
                r.errors.append("Test cases need both a call and an expected value.")
                break
    elif t in OPEN_ENDED_TYPES:
        if not q.rubric or not q.rubric.criteria:
            r.errors.append("Essay / case-study questions need a grading rubric with criteria.")

    if q.rubric and q.rubric.criteria:
        total = q.rubric.total_points
        if abs(total - q.max_points) > 1e-6:
            r.warnings.append(f"Rubric points ({total:g}) differ from max points ({q.max_points:g}); rubric will be scaled.")
        if any(not c.description.strip() for c in q.rubric.criteria):
            r.errors.append("Every rubric criterion needs a description.")

    if not q.explanation.strip():
        r.warnings.append("No explanation provided for the correct answer.")
    if t in (QuestionType.CASE_STUDY,) and len(q.text) < 120:
        r.warnings.append("Case study text looks short - check that the scenario includes all needed context.")
    if re.search(r"\b(see|refer to|shown in) (the )?(figure|diagram|graph|table|image)\b", q.text, re.I):
        r.warnings.append("Question refers to a figure/diagram/table that the app cannot display.")
    if t == QuestionType.PROGRAMMING and re.search(r"\b(following|below|this) code\b", q.text, re.I) and not q.code_context.strip():
        r.errors.append("Question refers to code that is not included.")
    return r


def _validate_choice(q: Question, r: ValidationReport) -> None:
    if q.type == QuestionType.TRUE_FALSE:
        ids = {o.id for o in q.options}
        if ids != {"true", "false"}:
            r.errors.append("True/false questions must have exactly the options 'true' and 'false'.")
    elif len(q.options) < 2:
        r.errors.append("Choice questions need at least two options.")
    elif len(q.options) < 3:
        r.warnings.append("Only two options - consider adding plausible distractors.")

    ids = [o.id for o in q.options]
    if len(set(ids)) != len(ids):
        r.errors.append("Option ids must be unique.")
    texts = [normalize_text(o.text) for o in q.options]
    if any(not t for t in texts):
        r.errors.append("Options must not be empty.")
    if len(set(texts)) != len(texts):
        r.errors.append("Two or more options have the same text.")
    missing = [c for c in q.correct_option_ids if c not in ids]
    if missing:
        r.errors.append(f"Answer key refers to unknown option(s): {', '.join(missing)}.")
    n_correct = len(set(q.correct_option_ids))
    if q.type in (QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE) and n_correct != 1:
        r.errors.append(f"Exactly one correct option required, found {n_correct}.")
    if q.type == QuestionType.MULTIPLE_SELECT:
        if n_correct < 1:
            r.errors.append("Multiple-select question needs at least one correct option.")
        if n_correct == len(q.options) and len(q.options) > 1:
            r.warnings.append("All options are correct - the question may not discriminate.")

    if q.type != QuestionType.TRUE_FALSE and q.options:
        if any(p in normalize_text(o.text) for o in q.options for p in _GIVEAWAY_PHRASES):
            r.warnings.append("Uses 'all/none of the above' style options, which often make questions ambiguous.")
        lengths = {o.id: len(o.text) for o in q.options}
        correct = [lengths[c] for c in q.correct_option_ids if c in lengths]
        others = [length for oid, length in lengths.items() if oid not in q.correct_option_ids]
        if correct and others and min(correct) > 2.5 * max(others) and min(correct) > 40:
            r.warnings.append("Correct option is much longer than the distractors - this can give the answer away.")


def validate_exam_questions(questions: list[Question]) -> dict[str, ValidationReport]:
    """Validate a full question set, including duplicate detection across it."""
    reports = {q.id: validate_question(q) for q in questions}
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        for q in questions:
            if ids.count(q.id) > 1:
                reports[q.id].errors.append("Duplicate question id in exam.")
    for i, j, s in find_duplicates(questions):
        reports[questions[j].id].warnings.append(
            f"Near-duplicate of question {i + 1} ({s:.0%} similar)."
        )
    return reports

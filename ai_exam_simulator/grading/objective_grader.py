"""Deterministic grading for objective question formats."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from ..exams.question_models import MultiSelectScoring, Question, QuestionType, ScoringRules
from ..exams.validation import light_normalize, normalize_text
from .models import GradingMethod, QuestionResult, ResultStatus, status_for


def _result(q: Question, points: float, feedback: str, **kw: Any) -> QuestionResult:
    status = kw.pop("status", None) or status_for(points, q.max_points)
    return QuestionResult(
        question_id=q.id, points=round(points, 4), max_points=q.max_points, status=status,
        method=kw.pop("method", GradingMethod.DETERMINISTIC), feedback=feedback, **kw,
    )


def unanswered(q: Question) -> QuestionResult:
    return QuestionResult(
        question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.UNANSWERED,
        method=GradingMethod.DETERMINISTIC, feedback="No answer was given.",
    )


def _penalty(q: Question, rules: ScoringRules) -> float:
    return -rules.negative_fraction * q.max_points if rules.negative_marking else 0.0


def grade_single(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    chosen = str(value)
    if chosen in q.correct_option_ids:
        return _result(q, q.max_points, "Correct.")
    pen = _penalty(q, rules)
    msg = "Incorrect." + (f" Negative marking applied ({pen:g} points)." if pen else "")
    return _result(q, pen, msg, status=ResultStatus.INCORRECT)


def grade_multi(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    chosen = {str(v) for v in (value or [])}
    correct = set(q.correct_option_ids)
    if chosen == correct:
        return _result(q, q.max_points, "Correct - all right options selected.")
    hits = len(chosen & correct)
    wrong = len(chosen - correct)
    missed = len(correct - chosen)
    detail = f"{hits} of {len(correct)} correct option(s) selected, {wrong} wrong selection(s), {missed} missed."
    if rules.multi_select == MultiSelectScoring.PARTIAL and rules.partial_credit:
        frac = max(0.0, (hits - wrong) / len(correct))
        points = frac * q.max_points
        if points <= 0 and rules.negative_marking:
            points = _penalty(q, rules)
        return _result(q, points, detail)
    pen = _penalty(q, rules)
    return _result(q, pen, "Incorrect (all-or-nothing scoring). " + detail, status=ResultStatus.INCORRECT)


def _norm_answer(s: str, case_sensitive: bool) -> str:
    s = " ".join(str(s).strip().split())
    s = s.rstrip(".")
    return s if case_sensitive else normalize_text(s)


def text_matches(answer: str, accepted: list[str], case_sensitive: bool = False) -> tuple[bool, bool]:
    """Return (exact_match, fuzzy_match). Fuzzy = minor spelling difference."""
    a = _norm_answer(answer, case_sensitive)
    if not a:
        return False, False
    normed = [_norm_answer(x, case_sensitive) for x in accepted if str(x).strip()]
    if a in normed:
        return True, False
    from .numerical_grader import try_parse_number

    av = try_parse_number(answer)
    if av is not None:
        for x in accepted:
            xv = try_parse_number(x)
            if xv is not None and abs(av - xv) <= 1e-9 * max(1.0, abs(xv)):
                return True, False
    for x in normed:
        if len(x) >= 5 and not any(ch.isdigit() for ch in x) and SequenceMatcher(None, a, x).ratio() >= 0.88:
            return False, True
    return False, False


def grade_short_text(q: Question, value: Any) -> QuestionResult | None:
    """Deterministic short-answer check. Returns None when no decision is possible."""
    if not q.accepted_answers:
        return None
    exact, fuzzy = text_matches(str(value), q.accepted_answers, q.case_sensitive)
    if exact:
        return _result(q, q.max_points, "Correct - matches an accepted answer.")
    if fuzzy:
        return _result(q, q.max_points, "Accepted - matches an accepted answer apart from a minor spelling difference.",
                       error_tags=["spelling"])
    return None


def grade_fill_blank(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    answers = list(value or [])
    n = len(q.blank_answers)
    answers += [""] * (n - len(answers))
    correct = 0
    per = []
    for i, accepted in enumerate(q.blank_answers):
        exact, fuzzy = text_matches(str(answers[i] or ""), accepted, q.case_sensitive)
        ok = exact or fuzzy
        correct += ok
        per.append(f"Blank {i + 1}: {'correct' if ok else 'incorrect'}")
    if correct == n:
        return _result(q, q.max_points, "All blanks correct.")
    points = (correct / n) * q.max_points if rules.partial_credit else 0.0
    return _result(q, points, "; ".join(per))


def grade_matching(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    chosen: dict[str, str] = dict(value or {})
    n = len(q.match_pairs)
    correct = sum(1 for p in q.match_pairs if light_normalize(str(chosen.get(p.id, ""))) == light_normalize(p.right))
    if correct == n:
        return _result(q, q.max_points, "All pairs matched correctly.")
    points = (correct / n) * q.max_points if rules.partial_credit else 0.0
    return _result(q, points, f"{correct} of {n} pairs matched correctly.")


def grade_ordering(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    given = [light_normalize(str(x)) for x in (value or [])]
    expected = [light_normalize(x) for x in q.order_items]
    if given == expected:
        return _result(q, q.max_points, "Correct order.")
    n = len(expected)
    # Credit for each adjacent pair that appears in the right relative order.
    pos = {item: i for i, item in enumerate(given)}
    pairs_ok = sum(
        1 for a, b in zip(expected, expected[1:]) if a in pos and b in pos and pos[a] < pos[b]
    )
    frac = pairs_ok / (n - 1) if n > 1 else 0.0
    points = frac * q.max_points if rules.partial_credit else 0.0
    return _result(q, points, f"{pairs_ok} of {n - 1} neighbouring steps are in the correct relative order.")


def grade_objective(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    t = q.type
    if t in (QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE):
        return grade_single(q, value, rules)
    if t == QuestionType.MULTIPLE_SELECT:
        return grade_multi(q, value, rules)
    if t == QuestionType.FILL_BLANK:
        return grade_fill_blank(q, value, rules)
    if t == QuestionType.MATCHING:
        return grade_matching(q, value, rules)
    if t == QuestionType.ORDERING:
        return grade_ordering(q, value, rules)
    raise ValueError(f"{t} is not an objective type")

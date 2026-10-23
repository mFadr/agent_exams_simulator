"""Deterministic scoring: objective formats, partial credit, negative marking, numerical tolerance."""

from __future__ import annotations

import pytest

from ai_exam_simulator.exams.question_models import (
    ExamConfig,
    MultiSelectScoring,
    Question,
    QuestionType,
    ScoringRules,
)
from ai_exam_simulator.exams.scoring import compute_summary
from ai_exam_simulator.grading.grading_service import GradingService
from ai_exam_simulator.grading.models import ResultStatus
from ai_exam_simulator.grading.numerical_grader import (
    diagnose_numeric_error,
    expressions_equivalent,
    extract_final_answer,
    grade_numerical,
    grade_symbolic,
    parse_math_expression,
    parse_number_with_unit,
    MathParseError,
    unit_factor,
)
from ai_exam_simulator.grading.objective_grader import grade_objective

from conftest import matching, mcq, multi, numerical, ordering

RULES = ScoringRules()


def test_single_choice_correct_and_wrong():
    q = mcq()
    assert grade_objective(q, "b", RULES).status == ResultStatus.CORRECT
    r = grade_objective(q, "a", RULES)
    assert r.status == ResultStatus.INCORRECT and r.points == 0


def test_negative_marking():
    q = mcq(points=4)
    rules = ScoringRules(negative_marking=True, negative_fraction=0.25)
    r = grade_objective(q, "a", rules)
    assert r.points == -1.0
    assert grade_objective(q, "b", rules).points == 4


def test_multi_select_partial_and_all_or_nothing():
    q = multi()
    partial = grade_objective(q, ["a"], ScoringRules(multi_select=MultiSelectScoring.PARTIAL))
    assert partial.status == ResultStatus.PARTIAL and partial.points == pytest.approx(1.0)
    # a wrong pick cancels a right pick
    assert grade_objective(q, ["a", "b"], RULES).points == 0
    aon = grade_objective(q, ["a"], ScoringRules(multi_select=MultiSelectScoring.ALL_OR_NOTHING))
    assert aon.points == 0 and aon.status == ResultStatus.INCORRECT
    assert grade_objective(q, ["c", "a"], RULES).status == ResultStatus.CORRECT


def test_partial_credit_can_be_disabled():
    q = matching()
    ans = {"p1": "+1", "p2": "0", "p3": "−1"}
    assert grade_objective(q, ans, ScoringRules(partial_credit=True)).points == pytest.approx(1.0)
    assert grade_objective(q, ans, ScoringRules(partial_credit=False)).points == 0


def test_matching_distinguishes_signs():
    q = matching()
    r = grade_objective(q, {"p1": "−1", "p2": "+1", "p3": "0"}, RULES)
    assert r.status == ResultStatus.PARTIAL and r.points == pytest.approx(1.0)
    assert grade_objective(q, {"p1": "+1", "p2": "−1", "p3": "0"}, RULES).status == ResultStatus.CORRECT


def test_ordering_partial_credit():
    q = ordering()
    assert grade_objective(q, ["A", "B", "C", "D"], RULES).status == ResultStatus.CORRECT
    r = grade_objective(q, ["B", "A", "C", "D"], RULES)  # A<B wrong, B<C ok, C<D ok
    assert r.points == pytest.approx(2.0)


def test_fill_blank_and_short_answer_normalisation():
    q = Question(subject="Languages", topic="Grammar", type=QuestionType.FILL_BLANK,
                 text="She ___ to school and ___ a book.", blank_answers=[["went"], ["bought", "purchased"]])
    assert grade_objective(q, ["Went", " purchased "], RULES).status == ResultStatus.CORRECT
    assert grade_objective(q, ["went", "buyed"], RULES).points == pytest.approx(0.5)
    svc = GradingService()
    sa = Question(subject="Physics", topic="Heat", type=QuestionType.SHORT_ANSWER, text="Name 0 K.", accepted_answers=["absolute zero"])
    assert svc.grade_deterministic(sa, "Absolute Zero.", RULES)[0].status == ResultStatus.CORRECT
    fuzzy, _ = svc.grade_deterministic(sa, "absolut zero", RULES)
    assert fuzzy.status == ResultStatus.CORRECT and "spelling" in fuzzy.error_tags
    wrong, ai_candidate = svc.grade_deterministic(sa, "boiling point", RULES)
    assert wrong.status == ResultStatus.INCORRECT and wrong.needs_review and ai_candidate


@pytest.mark.parametrize("text,value,unit", [
    ("9.81", 9.81, None), ("9,81 m/s^2", 9.81, "m/s^2"), ("1/3", 1 / 3, None), ("2*sqrt(2) cm", 2 * 2 ** 0.5, "cm"),
    ("1.2e3 J", 1200, "J"), ("pi", 3.14159265, None), ("-3", -3, None), ("4.8m", 4.8, "m"),
])
def test_parse_number_with_unit(text, value, unit):
    v, u = parse_number_with_unit(text)
    assert v == pytest.approx(value, rel=1e-6)
    assert u == unit


@pytest.mark.parametrize("bad", ["__import__('os')", "open('x')", "lambda: 1", "().__class__", "exec('1')", "a" * 500])
def test_math_parser_rejects_code(bad):
    with pytest.raises(MathParseError):
        parse_math_expression(bad)


def test_numerical_tolerance_and_units():
    q = numerical()
    assert grade_numerical(q, "9.8 m/s^2", RULES).status == ResultStatus.CORRECT  # within 1 %
    assert grade_numerical(q, "9.5 m/s^2", RULES).status == ResultStatus.INCORRECT
    missing = grade_numerical(q, "9.81", RULES)
    assert missing.status == ResultStatus.PARTIAL and "missing_unit" in missing.error_tags
    wrong_unit = grade_numerical(q, "9.81 kg", RULES)
    assert "wrong_unit" in wrong_unit.error_tags and wrong_unit.points < q.max_points
    length = numerical(value=1500, unit="m")
    assert grade_numerical(length, "1.5 km", RULES).status == ResultStatus.CORRECT  # unit conversion
    assert unit_factor("km", "m") == 1000 and unit_factor("min", "s") == 60 and unit_factor("kg", "m") is None


def test_numerical_absolute_tolerance_and_equivalent_forms():
    q = numerical(value=1 / 3, unit=None, rel=0)
    q.numerical.abs_tolerance = 0.001
    assert grade_numerical(q, "1/3", RULES).status == ResultStatus.CORRECT
    assert grade_numerical(q, "0.333", RULES).status == ResultStatus.CORRECT
    assert grade_numerical(q, "0.33", RULES).status == ResultStatus.INCORRECT
    assert grade_numerical(q, "banana", RULES).error_tags == ["unreadable_number"]


def test_numeric_error_diagnosis():
    assert "sign_error" in diagnose_numeric_error(-5, 5)
    assert "order_of_magnitude" in diagnose_numeric_error(50, 5)
    assert "rounding_or_precision" in diagnose_numeric_error(5.1, 5)
    assert diagnose_numeric_error(7, 5) == ["calculation_error"]


def test_symbolic_equivalence():
    assert expressions_equivalent("x^3 cos(x) + 3x^2 sin(x)", "3*x^2*sin(x) + x^3*cos(x)")
    assert expressions_equivalent("sqrt(2)/2", "1/sqrt(2)")
    assert expressions_equivalent("(x+1)^2", "x^2 + 2x + 1")
    assert not expressions_equivalent("x + 1", "x + 2")
    q = Question(subject="Mathematics", topic="Calculus", type=QuestionType.MATH_PROBLEM, text="Differentiate x^2 sin x",
                 expected_expression="2*x*sin(x) + x^2*cos(x)")
    working = "product rule\nf'(x) = 2x sin(x) + x^2 cos(x)"
    assert extract_final_answer(working) == "2x sin(x) + x^2 cos(x)"
    assert grade_symbolic(q, working).status == ResultStatus.CORRECT
    assert grade_symbolic(q, "f'(x) = 2x cos(x)").status == ResultStatus.INCORRECT


def test_summary_counts_breakdowns_and_pass():
    qs = [mcq("a", topic="T1"), mcq("b", topic="T1"), mcq("c", topic="T2"), multi("d")]
    svc = GradingService()
    answers = {"a": "b", "b": "a", "c": None, "d": ["a"]}
    results = [svc.grade_deterministic(q, answers[q.id], RULES)[0] for q in qs]
    cfg = ExamConfig(title="t", pass_threshold=40)
    s = compute_summary(qs, results, cfg)
    assert (s.correct, s.incorrect, s.unanswered, s.partial) == (1, 1, 1, 1)
    assert s.points == 2 and s.max_points == 5 and s.percentage == 40.0 and s.passed is True
    assert {b.key: b.count for b in s.by_topic} == {"T1": 2, "T2": 1, "Number theory": 1}
    cfg.pass_threshold = 41
    assert compute_summary(qs, results, cfg).passed is False


def test_total_floored_at_zero_with_negative_marking():
    qs = [mcq("a"), mcq("b")]
    rules = ScoringRules(negative_marking=True, negative_fraction=1.0)
    svc = GradingService()
    results = [svc.grade_deterministic(q, "a", rules)[0] for q in qs]
    s = compute_summary(qs, results, ExamConfig(title="t", scoring=rules))
    assert s.points == 0 and s.percentage == 0


def test_override_is_bounded_and_marked():
    q = mcq(points=2)
    r = grade_objective(q, "a", RULES)
    o = GradingService.override(r, 5, "accepted")
    assert o.points == 2 and o.overridden and o.status == ResultStatus.CORRECT and o.method.value == "manual"

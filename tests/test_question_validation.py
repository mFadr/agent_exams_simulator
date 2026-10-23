"""Question validation, duplicate detection, AI output conversion and bank import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_exam_simulator.ai.question_generator import raw_to_question
from ai_exam_simulator.documents.question_bank_io import export_json, import_json
from ai_exam_simulator.exams.question_models import Difficulty, Option, Question, QuestionType, ReviewStatus
from ai_exam_simulator.exams.validation import find_duplicates, similarity, validate_exam_questions, validate_question

from conftest import essay, mcq, numerical

SAMPLE = Path(__file__).resolve().parent.parent / "ai_exam_simulator" / "data" / "sample_questions.json"


def test_valid_question_passes():
    assert validate_question(mcq()).ok


def test_single_choice_needs_exactly_one_key():
    q = mcq()
    q.correct_option_ids = ["a", "b"]
    assert any("Exactly one" in e for e in validate_question(q).errors)
    q.correct_option_ids = ["zz"]
    assert any("unknown option" in e for e in validate_question(q).errors)


def test_duplicate_options_and_empty_text_rejected():
    q = mcq()
    q.options = [Option(id="a", text="4"), Option(id="b", text="4")]
    q.correct_option_ids = ["a"]
    assert not validate_question(q).ok
    q2 = mcq()
    q2.text = "?"
    assert not validate_question(q2).ok


def test_essay_requires_rubric_and_warns_on_point_mismatch():
    q = essay()
    assert validate_question(q).ok
    q.max_points = 10
    assert any("scaled" in w for w in validate_question(q).warnings)
    q.rubric = None
    assert not validate_question(q).ok


def test_fill_blank_count_must_match():
    q = Question(subject="L", topic="G", type=QuestionType.FILL_BLANK, text="A ___ and a ___.", blank_answers=[["x"]])
    assert any("blank" in e for e in validate_question(q).errors)


def test_programming_requires_function_name_and_code_reference():
    q = Question(subject="P", topic="F", type=QuestionType.PROGRAMMING, text="What does the following code print?",
                 test_cases=[], rubric=None)
    rep = validate_question(q)
    assert any("test cases or a rubric" in e for e in rep.errors)
    assert any("refers to code" in e for e in rep.errors)


def test_figure_reference_and_giveaway_warnings():
    q = mcq(text="Refer to the figure: what is the area of the triangle shown in the diagram?")
    assert any("figure" in w for w in validate_question(q).warnings)
    q = mcq()
    q.options[1] = Option(id="b", text="Four, because adding two and two always gives exactly four in decimal arithmetic")
    assert any("longer" in w for w in validate_question(q).warnings)


def test_duplicate_detection():
    a = mcq("a", text="What is the capital city of France?")
    b = mcq("b", text="What is the capital of France?")
    c = mcq("c", text="Which planet is closest to the Sun?")
    assert similarity(a.text, b.text) > 0.85
    dups = find_duplicates([a, b, c])
    assert [(i, j) for i, j, _ in dups] == [(0, 1)]
    reports = validate_exam_questions([a, b, c])
    assert any("Near-duplicate" in w for w in reports["b"].warnings)


def test_raw_to_question_variants():
    q = raw_to_question({"type": "mcq", "question": "Which is a vector quantity?", "options": ["A) Mass", "B) Velocity", "C) Time"],
                         "correct": "B", "explanation": "Velocity has direction."}, "Physics", ["Mechanics"], Difficulty.EASY)
    assert q.type == QuestionType.SINGLE_CHOICE and q.correct_option_ids == ["o1"] and q.options[1].text == "Velocity"
    tf = raw_to_question({"type": "true/false", "question": "Water boils at 100 °C at sea level.", "correct": "true"}, "Physics", [], Difficulty.EASY)
    assert tf.correct_option_ids == ["true"]
    num = raw_to_question({"type": "numerical", "question": "What is 3 * 4?", "numeric_answer": 12, "unit": "null",
                           "tolerance_percent": 0, "solution_expression": "3*4"}, "Mathematics", [], Difficulty.EASY)
    assert num.numerical.value == 12 and num.numerical.unit is None
    code = raw_to_question({"type": "programming", "question": "Write add(a, b).", "function_name": "add",
                            "tests": [{"call": "add(1, 2)", "expected": 3}, {"call": "add('a','b')", "expected": "ab"}]},
                           "Programming", [], Difficulty.EASY)
    assert code.test_cases[0].expected == "3" and code.test_cases[1].expected == "'ab'"
    es = raw_to_question({"type": "essay", "question": "Discuss X in detail please.", "rubric": [{"criterion": "A", "points": 2}, {"criterion": "B", "points": 3}]},
                         "History", [], Difficulty.HARD)
    assert es.max_points == 5 and len(es.rubric.criteria) == 2
    with pytest.raises(Exception):
        raw_to_question({"type": "single_choice", "question": "x?", "options": ["a", "b"], "correct": [5]}, "S", [], Difficulty.EASY)


def test_sample_bank_is_fully_valid():
    res = import_json(SAMPLE, keep_ids=True)
    assert res.errors == []
    assert len(res.questions) >= 25
    assert {q.type for q in res.questions} == set(QuestionType)  # every format has a sample


def test_bank_import_rejects_invalid_and_distrusts_verification(tmp_path):
    good = mcq("g").model_dump(mode="json")
    good["review_status"] = "ai_verified"
    bad = mcq("x").model_dump(mode="json")
    bad["correct_option_ids"] = ["nope"]
    p = tmp_path / "bank.json"
    p.write_text(json.dumps({"questions": [good, bad, "junk"]}), encoding="utf-8")
    res = import_json(p)
    assert len(res.questions) == 1 and len(res.errors) == 2
    assert res.questions[0].review_status == ReviewStatus.UNVERIFIED
    assert res.questions[0].id != "g"  # new ids by default


def test_bank_roundtrip(tmp_path):
    p = tmp_path / "out.json"
    qs = [mcq("a"), numerical("b"), essay("c")]
    export_json(qs, p)
    res = import_json(p, keep_ids=True)
    assert [q.id for q in res.questions] == ["a", "b", "c"]


def test_import_rejects_wrong_extension_and_garbage(tmp_path):
    p = tmp_path / "bank.txt"
    p.write_text("{}", encoding="utf-8")
    assert import_json(p).errors
    p2 = tmp_path / "bank.json"
    p2.write_text("{not json", encoding="utf-8")
    assert "JSON" in import_json(p2).errors[0]

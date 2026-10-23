"""Weakness detection and adaptive exam planning."""

from __future__ import annotations

import random

from ai_exam_simulator.exams.question_models import Difficulty, ExamConfig, ExamMode, QuestionType
from ai_exam_simulator.learning.adaptive_engine import AdaptiveEngine
from ai_exam_simulator.learning.analytics import ResultRecord
from ai_exam_simulator.learning.weakness_detection import classify_topic, detect, wilson_interval

from conftest import mcq


def rec(attempt, topic, ok, qtype="single_choice", diff="medium", tags=(), subject="Mathematics", conf=None, t="2026-01-01"):
    return ResultRecord(attempt_id=attempt, started_at=f"{t}T{attempt}", subject=subject, topic=topic, qtype=qtype, difficulty=diff,
                        points=1.0 if ok else 0.0, max_points=1.0, status="correct" if ok else "incorrect",
                        error_tags=list(tags), time_spent=10, confidence=conf, question_id=f"{attempt}{topic}{random.random()}")


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = wilson_interval(1, 1)
    assert lo < 0.3 and hi == 1.0
    lo2, hi2 = wilson_interval(45, 50)
    assert lo2 > 0.75


def test_insufficient_data_vs_recurring_vs_one_time():
    assert classify_topic([rec("a1", "T", False), rec("a1", "T", False)]).status == "insufficient_data"
    recurring = [rec("a1", "T", False), rec("a1", "T", True), rec("a2", "T", False), rec("a2", "T", False)]
    assert classify_topic(recurring).status == "recurring_weakness"
    one_time = [rec("a1", "T", True), rec("a2", "T", True), rec("a3", "T", False), rec("a3", "T", True)]
    assert classify_topic(one_time).status == "one_time_mistake"
    mastered = [rec(f"a{i}", "T", True) for i in range(8)]
    assert classify_topic(mastered).status == "mastered"


def test_insights_patterns():
    records = []
    for i in range(4):
        a = f"a{i}"
        records += [rec(a, "Kinematics", True, "single_choice", subject="Physics"),
                    rec(a, "Kinematics", False, "essay", subject="Physics"),
                    rec(a, "Kinematics", False, "numerical", tags=["missing_unit"], subject="Physics"),
                    rec(a, "Kinematics", True, "true_false", diff="easy", subject="Physics"),
                    rec(a, "Kinematics", False, "numerical", diff="hard", tags=["sign_error"], subject="Physics")]
    rep = detect(records)
    kinds = {i.kind for i in rep.insights}
    assert {"recognition_vs_recall", "calculation_gap", "difficulty_drop", "errors_units", "errors_signs"} <= kinds
    assert rep.recommendations


def test_no_conclusions_from_tiny_history():
    rep = detect([rec("a1", "Algebra", False)])
    assert rep.insights == [] and rep.weak_topics() == []
    assert "Not enough data" in rep.recommendations[0]


def _records_at(level, n, acc):
    return [rec(f"a{i // 5}", "Algebra", i < acc * n, diff=level) for i in range(n)]


def test_adaptive_steps_up_after_sustained_success():
    eng = AdaptiveEngine(random.Random(0))
    cfg = ExamConfig(title="x", mode=ExamMode.ADAPTIVE, difficulty=Difficulty.MEDIUM, topics=["Algebra"])
    recs = _records_at("medium", 10, 0.9)
    plan = eng.plan(cfg, detect(recs), recs)
    assert Difficulty.HARD in plan.difficulty_mix and plan.difficulty_mix[Difficulty.HARD] > 0.5
    assert any("Raising difficulty" in r for r in plan.reasons)


def test_adaptive_steps_down_gradually_and_keeps_measuring():
    eng = AdaptiveEngine(random.Random(0))
    cfg = ExamConfig(title="x", mode=ExamMode.ADAPTIVE, difficulty=Difficulty.HARD, topics=["Algebra"])
    recs = _records_at("hard", 10, 0.2)
    plan = eng.plan(cfg, detect(recs), recs)
    assert plan.difficulty_mix == {Difficulty.MEDIUM: 0.5, Difficulty.HARD: 0.5}  # half stays at the requested level
    floor = cfg.model_copy(update={"min_difficulty": Difficulty.HARD})
    plan2 = eng.plan(floor, detect(recs), recs)
    assert plan2.difficulty_mix == {Difficulty.HARD: 1.0}  # never below the configured floor


def test_adaptive_keeps_level_with_little_data():
    cfg = ExamConfig(title="x", mode=ExamMode.ADAPTIVE)
    recs = _records_at("medium", 3, 0.0)
    plan = AdaptiveEngine().plan(cfg, detect(recs), recs)
    assert plan.difficulty_mix == {Difficulty.MEDIUM: 1.0} and "keeping the level" in plan.reasons[0]


def test_fixed_mode_is_untouched():
    cfg = ExamConfig(title="x", mode=ExamMode.FIXED, topics=["Algebra"], difficulty=Difficulty.EASY)
    recs = _records_at("easy", 20, 1.0)
    plan = AdaptiveEngine().plan(cfg, detect(recs), recs)
    assert plan.difficulty_mix == {Difficulty.EASY: 1.0} and plan.topic_weights == {"Algebra": 1.0}


def test_weakness_mode_targets_weak_topics_and_formats():
    recs = []
    for i in range(3):
        recs += [rec(f"a{i}", "Fractions", False, "numerical"), rec(f"a{i}", "Fractions", False, "numerical"),
                 rec(f"a{i}", "Geometry", True), rec(f"a{i}", "Geometry", True)]
    cfg = ExamConfig(title="x", mode=ExamMode.WEAKNESS, question_types=[QuestionType.SINGLE_CHOICE, QuestionType.NUMERICAL])
    plan = AdaptiveEngine().plan(cfg, detect(recs), recs)
    assert plan.topics == ["Fractions"]
    assert plan.type_weights[QuestionType.NUMERICAL] > plan.type_weights[QuestionType.SINGLE_CHOICE]
    assert "Fractions" in plan.focus_notes


def test_revision_and_bank_selection():
    eng = AdaptiveEngine(random.Random(3))
    bank = [mcq(f"q{i}", topic="Algebra" if i < 5 else "Geometry", difficulty=Difficulty.MEDIUM) for i in range(10)]
    cfg = ExamConfig(title="x", mode=ExamMode.REVISION)
    plan = eng.plan(cfg, detect([]), [], revision_ids=["q7", "q2", "zzz"])
    picked, unfilled = eng.select_from_bank(plan, bank, 5, [QuestionType.SINGLE_CHOICE])
    assert [q.id for q in picked] == ["q7", "q2"] and unfilled == []

    cfg2 = ExamConfig(title="x", topics=["Algebra"], num_questions=8)
    plan2 = eng.plan(cfg2, detect([]), [])
    picked2, unfilled2 = eng.select_from_bank(plan2, bank, 8, [QuestionType.SINGLE_CHOICE])
    assert len(picked2) == 5 and all(q.topic == "Algebra" for q in picked2)
    assert len(unfilled2) == 3  # the rest must be generated or added
    assert len({q.id for q in picked2}) == 5


def test_follow_up_exam_targets_weak_area(ctx):
    """Workflow 6: detected weakness → weakness-practice build plan focuses on it."""
    from ai_exam_simulator.exams.question_models import ExamConfig as EC

    qs = [mcq(f"f{i}", topic="Fractions", text=f"Fraction question number {i}?") for i in range(6)] + \
         [mcq(f"g{i}", topic="Geometry", text=f"Geometry question number {i}?") for i in range(6)]
    ctx.questions.save_many(qs)
    for attempt in range(3):
        s = ctx.start_exam(EC(title=f"Run {attempt}", subject="Mathematics", num_questions=4, shuffle_questions=False),
                           [qs[attempt], qs[attempt + 3], qs[6 + attempt], qs[9 + attempt - 1]])
        for q in s.state.questions:
            s.set_answer(q.id, "a" if q.topic == "Fractions" else "b")  # fractions always wrong
        s.submit()
        ctx.finalize(s, ctx.grading.grade_all(s.state.questions, s.state.answers, s.config.scoring, use_ai=False))
    cfg = EC(title="Follow-up", subject="Mathematics", mode=ExamMode.WEAKNESS, num_questions=4)
    bp = ctx.builder.build_plan(cfg, use_bank=True, generate_missing=False)
    assert bp.plan.topics == ["Fractions"]
    assert bp.from_bank and all(q.topic == "Fractions" for q in bp.from_bank)

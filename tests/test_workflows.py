"""Representative end-to-end workflows (service level, no UI)."""

from __future__ import annotations

import json

from ai_exam_simulator.ai.provider_interface import ProviderUnavailableError
from ai_exam_simulator.config.settings import RoutingMode
from ai_exam_simulator.exams.exam_engine import ExamSession, SubmitReason
from ai_exam_simulator.exams.question_models import ExamConfig, ExamMode, QuestionType
from ai_exam_simulator.grading.models import GradingMethod, ResultStatus

from conftest import FakeClock, essay, mcq, multi, numerical


def _finish(ctx, session, use_ai=False, consent=False):
    results = ctx.grading.grade_all(session.state.questions, session.state.answers, session.config.scoring,
                                    use_ai=use_ai, cloud_consent=consent)
    return ctx.finalize(session, results), results


def test_1_create_and_complete_exam(ctx):
    qs = [mcq("a"), multi("b"), numerical("c")]
    ctx.questions.save_many(qs)
    s = ctx.start_exam(ExamConfig(title="W1", pass_threshold=60), qs)
    s.set_answer("a", "b")
    s.set_answer("b", ["a", "c"])
    s.set_answer("c", "9.81 m/s^2")
    s.submit()
    summary, _ = _finish(ctx, s)
    assert summary.percentage == 100 and summary.passed and not summary.provisional


def test_2_unanswered_questions(ctx):
    qs = [mcq("a"), mcq("b"), mcq("c")]
    s = ctx.start_exam(ExamConfig(title="W2"), qs)
    s.set_answer("a", "b")
    s.submit()
    summary, _ = _finish(ctx, s)
    assert summary.unanswered == 2 and summary.correct == 1


def test_3_timer_expires(ctx):
    clock = FakeClock()
    qs = [mcq("a"), mcq("b")]
    s = ExamSession.create("e", ExamConfig(title="W3", duration_minutes=1), qs, persist=ctx.persist_session, clock=clock)
    eid = ctx.exams.create_exam(s.config, qs)
    s.state.exam_id = eid
    s.save_now()
    s.set_answer("a", "b")
    clock.advance(65)
    assert s.check_expiry() and s.state.submit_reason == SubmitReason.TIME_EXPIRED
    summary, _ = _finish(ctx, s)
    assert summary.correct == 1 and summary.unanswered == 1


def test_4_restart_and_recover(ctx, tmp_path, local_mock, cloud_mock):
    from ai_exam_simulator.app.application import AppContext

    qs = [mcq("a"), mcq("b")]
    s = ctx.start_exam(ExamConfig(title="W4", duration_minutes=45), qs)
    s.set_answer("a", "b")
    s.toggle_flag("b")
    aid = s.state.attempt_id
    ctx.db.close()
    # a completely new application context on the same data directory
    ctx2 = AppContext.create(ctx.paths.root, secrets=ctx.secrets, local_provider=local_mock, cloud_provider=cloud_mock, setup_logging=False)
    assert aid in ctx2.exams.open_attempt_ids()
    r = ctx2.resume_session(aid)
    assert r.answer("a").value == "b" and r.answer("b").flagged and r.is_open
    r.submit()
    summary, _ = _finish(ctx2, r)
    assert summary.correct == 1
    ctx2.db.close()


def test_5_objective_and_written_with_ai(ctx, local_mock):
    ev = {"criteria": [{"id": "c1", "awarded": 2, "satisfied": True, "comment": "stated"},
                       {"id": "c2", "awarded": 1, "satisfied": False, "comment": "example vague"}],
          "overall_feedback": "Mostly correct.", "misconception": "", "error_type": "incomplete", "confidence": 0.85}
    local_mock._responses = [json.dumps(ev)]
    ctx.settings.settings.ai.routing_mode = RoutingMode.LOCAL_ONLY
    qs = [mcq("a"), essay("e")]
    s = ctx.start_exam(ExamConfig(title="W5", question_types=[QuestionType.SINGLE_CHOICE, QuestionType.ESSAY]), qs)
    s.set_answer("a", "b")
    s.set_answer("e", "Every action has an equal and opposite reaction, e.g. a rocket.")
    s.submit()
    summary, results = _finish(ctx, s, use_ai=True)
    by = {r.question_id: r for r in results}
    assert by["a"].method == GradingMethod.DETERMINISTIC
    assert by["e"].method == GradingMethod.AI_RUBRIC and by["e"].points == 3
    assert summary.points == 4 and summary.max_points == 5
    # user disagrees with the AI and overrides
    new = ctx.grading.override(by["e"], 4, "example is fine")
    summary2 = ctx.update_result(s.state, [by["a"], new])
    assert summary2.points == 5


def test_6_follow_up_from_weaknesses(ctx):
    qs = [mcq(f"q{i}", topic="Fractions" if i < 4 else "Geometry", text=f"Question about topic number {i}?") for i in range(8)]
    ctx.questions.save_many(qs)
    for run in range(2):
        s = ctx.start_exam(ExamConfig(title=f"W6-{run}", subject="Mathematics", shuffle_questions=False), qs)
        for q in qs:
            s.set_answer(q.id, "a" if q.topic == "Fractions" else "b")
        s.submit()
        _finish(ctx, s)
    bp = ctx.builder.build_plan(ExamConfig(title="Follow", subject="Mathematics", mode=ExamMode.WEAKNESS, num_questions=3),
                                generate_missing=False)
    assert bp.plan.topics == ["Fractions"] and all(q.topic == "Fractions" for q in bp.from_bank)
    rev = ctx.builder.build_plan(ExamConfig(title="Rev", subject="Mathematics", mode=ExamMode.REVISION, num_questions=10))
    assert {q.topic for q in rev.from_bank} == {"Fractions"}


def test_7_cloud_unavailable_app_still_works(ctx, cloud_mock, local_mock):
    ctx.settings.settings.ai.routing_mode = RoutingMode.CLOUD_ONLY
    cloud_mock._responses = [ProviderUnavailableError("provider outage")]
    qs = [mcq("a"), essay("e")]
    s = ctx.start_exam(ExamConfig(title="W7"), qs)
    s.set_answer("a", "b")
    s.set_answer("e", "text")
    s.submit()
    summary, results = _finish(ctx, s, use_ai=True, consent=True)
    by = {r.question_id: r for r in results}
    assert by["a"].status == ResultStatus.CORRECT  # objective scoring unaffected
    assert by["e"].status == ResultStatus.PENDING_REVIEW and "outage" in by["e"].feedback  # honest, not fabricated
    assert summary.provisional


def test_8_local_only_without_api_key(ctx, local_mock):
    ctx.settings.settings.ai.routing_mode = RoutingMode.LOCAL_ONLY
    assert ctx.secrets.get("cloud_api_key") is None
    gen = {"questions": [{"type": "true_false", "question": "The Earth orbits the Sun once per year.", "correct": True, "explanation": "Yes"}]}
    local_mock._responses = [json.dumps(gen), json.dumps({"answer": True, "ambiguous": False})]
    from ai_exam_simulator.ai.question_generator import GenerationRequest
    from ai_exam_simulator.exams.question_models import Difficulty

    res = ctx.generator.generate(GenerationRequest(subject="Science", topics=["Astronomy"], difficulty=Difficulty.EASY,
                                                   count=1, types=[QuestionType.TRUE_FALSE]))
    assert len(res.questions) == 1 and res.questions[0].review_status.value == "ai_verified"
    assert ctx.usage.totals()["cloud_calls"] == 0


def test_practice_attempts_excluded_from_statistics(ctx):
    qs = [mcq("a")]
    s = ctx.start_exam(ExamConfig(title="Practice", mode=ExamMode.PRACTICE), qs)
    s.set_answer("a", "a")
    s.submit()
    summary, _ = _finish(ctx, s)
    assert summary.passed is None
    assert ctx.analytics.records() == []

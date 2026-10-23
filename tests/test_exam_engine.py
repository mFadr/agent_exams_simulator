"""Exam sessions: creation, timer, submission rules, autosave and recovery."""

from __future__ import annotations

import random

import pytest

from ai_exam_simulator.exams.exam_engine import ExamSession, SessionError, SessionState, SessionStatus, SubmitReason
from ai_exam_simulator.exams.question_models import ExamConfig, ExamMode, QuestionType

from conftest import essay, matching, mcq, multi, numerical, ordering


def make(clock, questions=None, persist=None, **cfg) -> ExamSession:
    config = ExamConfig(title="Test", **cfg)
    return ExamSession.create("exam1", config, questions or [mcq("a"), mcq("b"), mcq("c")], persist=persist, clock=clock,
                              rng=random.Random(1))


def test_creation_snapshots_and_shuffles(clock):
    qs = [mcq(str(i)) for i in range(10)] + [ordering("o"), matching("m")]
    s = make(clock, qs, shuffle_questions=True, shuffle_options=True, question_types=[QuestionType.SINGLE_CHOICE])
    assert sorted(s.state.order) == sorted(q.id for q in qs)
    assert s.state.order != [q.id for q in qs]
    assert s.state.display_order["o"] != ordering().order_items  # never shown in solved order
    qs[0].text = "changed after start"
    assert all(q.text != "changed after start" for q in s.state.questions)


def test_empty_exam_rejected(clock):
    with pytest.raises(SessionError):
        ExamSession.create("e", ExamConfig(title="x"), [], clock=clock)


def test_timer_uses_persisted_deadline(clock):
    s = make(clock, duration_minutes=10)
    assert s.remaining_seconds() == 600
    clock.advance(125)
    assert s.remaining_seconds() == pytest.approx(475)
    untimed = make(clock, duration_minutes=0)
    assert untimed.remaining_seconds() is None and not untimed.is_expired()


def test_auto_submit_on_expiry_keeps_saved_answers(clock):
    s = make(clock, duration_minutes=1, shuffle_questions=False)
    s.set_answer("a", "b")
    clock.advance(61)
    assert s.check_expiry() is True
    assert s.state.status == SessionStatus.SUBMITTED
    assert s.state.submit_reason == SubmitReason.TIME_EXPIRED
    assert s.state.submitted_at == s.state.deadline  # never later than the deadline
    assert s.answer("a").value == "b"
    with pytest.raises(SessionError):
        s.set_answer("b", "b")


def test_answer_after_deadline_is_refused(clock):
    s = make(clock, duration_minutes=1, shuffle_questions=False)
    clock.advance(90)
    with pytest.raises(SessionError):
        s.set_answer("a", "b")
    assert s.state.submit_reason == SubmitReason.TIME_EXPIRED


def test_navigation_and_revisit_rule(clock):
    s = make(clock, allow_revisit=False, shuffle_questions=False)
    s.next()
    assert s.current_index == 1
    assert not s.can_go_to(0)
    with pytest.raises(SessionError):
        s.go_to(0)
    s2 = make(clock, allow_revisit=True)
    s2.go_to(2)
    s2.go_to(0)
    assert s2.current_index == 0


def test_unanswered_rule(clock):
    s = make(clock, allow_unanswered=False, shuffle_questions=False)
    s.set_answer("a", "b")
    ok, msg = s.can_submit()
    assert not ok and "2, 3" in msg
    with pytest.raises(SessionError):
        s.submit()
    s.set_answer("b", "a")
    s.set_answer("c", "a")
    s.submit()
    assert s.state.status == SessionStatus.SUBMITTED


def test_unanswered_rule_does_not_block_time_expiry(clock):
    s = make(clock, allow_unanswered=False, duration_minutes=1)
    clock.advance(61)
    assert s.check_expiry()


def test_flags_confidence_and_time_tracking(clock):
    s = make(clock, shuffle_questions=False)
    assert s.toggle_flag("a") is True
    s.set_confidence("a", 4)
    clock.advance(30)
    s.next()
    clock.advance(10)
    s.submit()
    assert s.answer("a").flagged and s.answer("a").confidence == 4
    assert s.answer("a").time_spent_s == pytest.approx(30)
    assert s.answer("b").time_spent_s == pytest.approx(10)
    assert s.flagged_indices() == [0]


def test_autosave_called_on_every_change(clock):
    saved: list[SessionState] = []
    s = make(clock, persist=lambda st: saved.append(st.model_copy(deep=True)), shuffle_questions=False)
    n = len(saved)
    s.set_answer("a", "b")
    s.toggle_flag("b")
    s.next()
    assert len(saved) == n + 3
    assert saved[-1].answers["a"].value == "b"


def test_save_failure_is_reported_not_lost(clock):
    calls = {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")

    s = make(clock, persist=flaky, shuffle_questions=False)
    s.set_answer("a", "a")  # call 2 ok
    s.set_answer("b", "b")  # call 3 fails
    assert s.last_save_error and "disk full" in s.last_save_error
    assert s.answer("b").value == "b"  # kept in memory
    s.set_answer("c", "b")  # retried successfully
    assert s.last_save_error is None


def test_recovery_after_restart_from_database(ctx, clock):
    qs = [mcq("a"), numerical("n"), essay("e")]
    cfg = ExamConfig(title="Recover me", duration_minutes=30, question_types=[QuestionType.SINGLE_CHOICE])
    ctx.questions.save_many(qs)
    session = ctx.start_exam(cfg, qs)
    session.set_answer("a", "b")
    session.set_answer("n", "9.81 m/s^2")
    aid = session.state.attempt_id
    # simulate a crash: drop the object and reload everything from disk
    del session
    ctx.db.close()
    assert aid in ctx.exams.open_attempt_ids()
    restored = ctx.resume_session(aid)
    assert restored is not None and restored.is_open
    assert restored.answer("a").value == "b"
    assert restored.answer("n").value == "9.81 m/s^2"
    assert restored.remaining_seconds() <= 30 * 60


def test_reopen_after_deadline_submits_with_expired_while_closed(clock):
    store: dict = {}
    s = make(clock, persist=lambda st: store.__setitem__("s", st.model_dump_json()), duration_minutes=5, shuffle_questions=False)
    s.set_answer("a", "b")
    clock.advance(3600)  # app was closed for an hour
    reopened = ExamSession(SessionState.model_validate_json(store["s"]), clock=clock)
    reopened.resume()
    assert not reopened.is_open
    assert reopened.state.submit_reason == SubmitReason.EXPIRED_WHILE_CLOSED
    assert reopened.answer("a").value == "b"
    assert reopened.answer("a").time_spent_s < 1800  # long closed gap is not counted


def test_immediate_feedback_locks_answer_outside_practice(clock):
    s = make(clock, shuffle_questions=False)
    s.set_answer("a", "b")
    s.mark_checked("a")
    with pytest.raises(SessionError):
        s.set_answer("a", "a")


def test_practice_mode_retry_and_insert(clock):
    s = make(clock, mode=ExamMode.PRACTICE, shuffle_questions=False)
    s.set_answer("a", "a")
    s.mark_checked("a")
    s.reset_check("a")
    s.set_answer("a", "b")
    idx = s.insert_question(multi("extra"), 0)
    assert s.state.order[idx] == "extra" and s.total == 4
    fixed = make(clock)
    with pytest.raises(SessionError):
        fixed.insert_question(multi("x"), 0)


def test_attempt_limit(ctx):
    qs = [mcq("a")]
    ctx.questions.save_many(qs)
    cfg = ExamConfig(title="Once", attempts_allowed=1)
    s = ctx.start_exam(cfg, qs)
    with pytest.raises(ValueError):
        ctx.start_exam(cfg, qs, exam_id=s.state.exam_id)

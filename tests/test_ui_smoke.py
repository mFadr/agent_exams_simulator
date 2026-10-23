"""UI smoke tests (offscreen Qt): every page builds, an exam can be taken, submitted and reviewed."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from ai_exam_simulator.app.navigation import Page  # noqa: E402
from ai_exam_simulator.exams.question_models import QuestionType, preset_config  # noqa: E402


@pytest.fixture
def window(ctx, qtbot, monkeypatch):
    import ai_exam_simulator.ui.exam_session as es
    import ai_exam_simulator.ui.exam_setup as setup
    import ai_exam_simulator.ui.main_window as mw
    import ai_exam_simulator.ui.widgets as w

    msgs: list = []
    for mod in (es, setup, mw, w):
        monkeypatch.setattr(mod, "show_error", lambda *a: msgs.append(("error",) + a[1:]), raising=False)
        monkeypatch.setattr(mod, "show_info", lambda *a: msgs.append(("info",) + a[1:]), raising=False)
        monkeypatch.setattr(mod, "confirm", lambda *a, **k: True, raising=False)
    win = mw.MainWindow(ctx)
    win.msgs = msgs
    qtbot.addWidget(win)
    return win


def test_all_pages_open(window, qtbot):
    for p in Page:
        window.go(p)
        assert window.stack.currentWidget() is window.pages[p]


def test_take_exam_from_bank_and_review(window, qtbot, ctx):
    cfg = preset_config("Standard Exam")
    cfg.subject = "Mathematics"
    cfg.num_questions = 4
    cfg.question_types = [QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_SELECT, QuestionType.TRUE_FALSE,
                          QuestionType.NUMERICAL, QuestionType.FILL_BLANK]
    window.setup.prefill(cfg)
    window.setup.src_bank.setChecked(True)
    window.setup._build()
    assert len(window.setup.questions) >= 3
    window.setup._start()
    s = window.active_session
    assert s is not None and window.take_stack.currentWidget() is window.session_view
    view = window.session_view
    # answer the first question through its widget, navigate, flag
    q0 = s.current_question
    w = view.answer_widget
    if q0.type in (QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE):
        w.buttons[q0.correct_option_ids[0]].setChecked(True)
    elif q0.type == QuestionType.MULTIPLE_SELECT:
        for oid in q0.correct_option_ids:
            w.boxes[oid].setChecked(True)
    else:
        w.set_value(q0.correct_answer_display().split(" ")[0])
        view._save_current()
    view.flush()
    assert s.answer(q0.id).is_answered()
    view._flag()
    view._next()
    assert s.current_index == 1
    view._submit()
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.results, timeout=10000)
    assert window.results.summary is not None
    assert window.results.summary.unanswered >= 1
    window.results.filter.setCurrentIndex(3)  # flagged
    assert window.results.qlist.count() == 1
    assert ctx.exams.list_attempts()[0].status == "graded"

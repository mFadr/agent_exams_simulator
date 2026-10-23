"""The focused exam-taking interface."""

from __future__ import annotations

import html
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..ai.question_generator import GenerationRequest
from ..app.application import AppContext
from ..config.settings import AIOperation
from ..exams.exam_engine import ExamSession, SessionError
from ..exams.question_models import EXAM_MODE_LABELS, QUESTION_TYPE_LABELS, Question
from ..grading.models import ResultStatus
from ..grading.numerical_grader import MathParseError, parse_math_expression
from .answer_widgets import AnswerWidget, make_answer_widget
from .widgets import Banner, ai_preflight, button, card, confirm, label, run_with_progress, set_role, show_error, show_info

log = logging.getLogger(__name__)


def question_html(q: Question) -> str:
    text = html.escape(q.text).replace("\n", "<br>")
    return f"<div style='line-height:140%'>{text}</div>"


class CalculatorDialog(QDialog):
    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calculator")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Enter an expression, e.g. 2*sqrt(3) + sin(pi/6) or 9.81*2.5^2/2", "muted"))
        self.expr = QLineEdit()
        self.result = label("", "h2")
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumHeight(140)
        lay.addWidget(self.expr)
        lay.addWidget(self.result)
        lay.addWidget(self.history)
        self.expr.returnPressed.connect(self._eval)

    def _eval(self) -> None:
        import sympy

        try:
            e = parse_math_expression(self.expr.text())
            if e.free_symbols:
                raise MathParseError("contains variables")
            v = sympy.N(e, 12)
            self.result.setText(f"= {v}")
            self.history.appendPlainText(f"{self.expr.text()} = {v}")
        except (MathParseError, TypeError, ValueError) as exc:
            self.result.setText(f"Error: {exc}")


class ExamSessionView(QWidget):
    finished = Signal(object)  # ExamSession (submitted)
    focus_mode = Signal(bool)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.session: ExamSession | None = None
        self.answer_widget: AnswerWidget | None = None
        self.nav_buttons: list[QPushButton] = []
        self._warned: set[int] = set()
        self._loading = False

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(700)
        self.debounce.timeout.connect(self._save_current)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)

        # header -----------------------------------------------------------------
        head, hl = card(QHBoxLayout)
        col = QVBoxLayout()
        self.title = label("", "h2")
        self.subtitle = label("", "muted")
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        hl.addLayout(col, 1)
        self.progress = QProgressBar()
        self.progress.setMinimumWidth(180)
        self.progress.setFormat("%v / %m answered")
        hl.addWidget(self.progress)
        self.tools_row = QHBoxLayout()
        self.calc_btn = button("Calculator", slot=lambda: CalculatorDialog(self).exec())
        self.notes_btn = button("Notes", slot=self._notes)
        self.formula_btn = button("Formula sheet", slot=self._formulas)
        for b in (self.calc_btn, self.notes_btn, self.formula_btn):
            hl.addWidget(b)
        self.full_btn = button("Full screen", slot=self._toggle_full, tooltip="Distraction-reduced mode (F11)")
        hl.addWidget(self.full_btn)
        self.timer_label = label("--:--", "timer", wrap=False)
        hl.addWidget(self.timer_label)
        root.addWidget(head)

        self.save_banner = Banner(warn=True)
        root.addWidget(self.save_banner)

        split = QSplitter()
        root.addWidget(split, 1)

        # navigation panel ----------------------------------------------------------
        navw, navl = card()
        navl.addWidget(label("Questions", "h2"))
        self.nav_grid_host = QWidget()
        self.nav_grid = QGridLayout(self.nav_grid_host)
        self.nav_grid.setSpacing(6)
        self.nav_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(self.nav_grid_host)
        navl.addWidget(sa, 1)
        navl.addWidget(label("■ current  ▣ answered  ⚑ flagged (orange border)", "muted"))
        self.unanswered_lbl = label("", "muted")
        navl.addWidget(self.unanswered_lbl)
        navw.setMinimumWidth(240)
        navw.setMaximumWidth(320)
        split.addWidget(navw)

        # question area ---------------------------------------------------------------
        qw = QWidget()
        ql = QVBoxLayout(qw)
        ql.setContentsMargins(8, 0, 0, 0)
        qcard, self.qcl = card()
        self.q_number = label("", "h2")
        self.q_meta = label("", "muted")
        self.q_text = QLabel()
        self.q_text.setWordWrap(True)
        self.q_text.setProperty("role", "question")
        self.q_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.q_code = QPlainTextEdit()
        self.q_code.setReadOnly(True)
        self.q_code.setFont(QFont("Consolas", 10))
        self.q_code.setMaximumHeight(220)
        self.qcl.addWidget(self.q_number)
        self.qcl.addWidget(self.q_meta)
        self.qcl.addWidget(self.q_text)
        self.qcl.addWidget(self.q_code)
        self.answer_host = QVBoxLayout()
        self.qcl.addLayout(self.answer_host, 1)

        self.feedback = Banner()
        self.qcl.addWidget(self.feedback)

        conf_row = QHBoxLayout()
        conf_row.addWidget(label("Confidence (optional):", "muted", wrap=False))
        self.confidence = QComboBox()
        self.confidence.addItems(["—", "1 - guessing", "2", "3", "4", "5 - certain"])
        self.confidence.currentIndexChanged.connect(self._confidence_changed)
        conf_row.addWidget(self.confidence)
        conf_row.addStretch()
        self.hint_btn = button("Hint", slot=self._hint)
        self.check_btn = button("Check answer", slot=self._check)
        self.retry_btn = button("Try again", slot=self._retry)
        self.similar_btn = button("Similar question", slot=self._similar, tooltip="Generate a similar practice question (AI)")
        for b in (self.hint_btn, self.check_btn, self.retry_btn, self.similar_btn):
            conf_row.addWidget(b)
        self.qcl.addLayout(conf_row)

        sa2 = QScrollArea()
        sa2.setWidgetResizable(True)
        sa2.setWidget(qcard)
        ql.addWidget(sa2, 1)

        nav_row = QHBoxLayout()
        self.prev_btn = button("◀ Previous", slot=self._prev)
        self.flag_btn = button("⚑ Flag for review", slot=self._flag)
        self.next_btn = button("Next ▶", slot=self._next)
        self.submit_btn = button("Submit exam", "primary", slot=self._submit)
        nav_row.addWidget(self.prev_btn)
        nav_row.addWidget(self.flag_btn)
        nav_row.addStretch()
        nav_row.addWidget(self.next_btn)
        nav_row.addWidget(self.submit_btn)
        ql.addLayout(nav_row)
        split.addWidget(qw)
        split.setStretchFactor(1, 1)

        QShortcut(QKeySequence("F11"), self, activated=self._toggle_full)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self._next)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self._prev)
        self._notes_dialog: QDialog | None = None
        self._full = False

    # ------------------------------------------------------------------ session binding
    def load(self, session: ExamSession) -> None:
        self.session = session
        self._warned.clear()
        cfg = session.config
        self.title.setText(cfg.title)
        mode = EXAM_MODE_LABELS[cfg.mode]
        fb = "immediate feedback" if cfg.shows_immediate_feedback else "results after submission"
        revisit = "" if cfg.allow_revisit else " · no going back"
        self.subtitle.setText(f"{cfg.subject} · {mode} · attempt {session.state.attempt_number} · {fb}{revisit}")
        self.calc_btn.setVisible(cfg.tools.calculator)
        self.notes_btn.setVisible(cfg.tools.notes)
        self.formula_btn.setVisible(cfg.tools.formula_sheet)
        self.check_btn.setVisible(cfg.shows_immediate_feedback)
        self.retry_btn.setVisible(False)
        self.hint_btn.setVisible(cfg.is_practice)
        self.similar_btn.setVisible(cfg.is_practice)
        self.submit_btn.setText("Finish practice" if cfg.is_practice else "Submit exam")
        self._build_nav()
        self._show_question()
        self.timer.start()
        self._tick()
        if self.ctx.settings.settings.app.fullscreen_exams and not self._full:
            self._toggle_full()

    def _build_nav(self) -> None:
        for b in self.nav_buttons:
            b.deleteLater()
        self.nav_buttons = []
        assert self.session
        cols = 4
        for i in range(self.session.total):
            b = QPushButton(str(i + 1))
            b.setProperty("role", "nav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._goto(idx))
            self.nav_grid.addWidget(b, i // cols, i % cols)
            self.nav_buttons.append(b)
        self._refresh_nav()

    def _refresh_nav(self) -> None:
        s = self.session
        if not s:
            return
        for i, b in enumerate(self.nav_buttons):
            qid = s.state.order[i]
            a = s.answer(qid)
            state = "current" if i == s.current_index else ("answered" if a.is_answered() else "")
            if a.flagged and i != s.current_index:
                state = "flagged"
            b.setProperty("state", state)
            b.setText(("⚑" if a.flagged else "") + str(i + 1))
            b.setEnabled(s.can_go_to(i) or i == s.current_index)
            tip = "Answered" if a.is_answered() else "Not answered"
            b.setToolTip(tip + (" · flagged" if a.flagged else ""))
            b.style().unpolish(b)
            b.style().polish(b)
        self.progress.setMaximum(s.total)
        self.progress.setValue(s.answered_count())
        un = len(s.unanswered_indices())
        fl = len(s.flagged_indices())
        self.unanswered_lbl.setText(f"{un} unanswered · {fl} flagged")
        self.save_banner.show_text(s.last_save_error or "", warn=True)

    # ------------------------------------------------------------------ question display
    def _show_question(self) -> None:
        s = self.session
        assert s
        self._loading = True
        q = s.current_question
        idx = s.current_index
        self.q_number.setText(f"Question {idx + 1} of {s.total}")
        self.q_meta.setText(f"{q.topic} · {QUESTION_TYPE_LABELS[q.type]} · {q.difficulty.value} · {q.max_points:g} point(s)")
        self.q_text.setText(question_html(q))
        self.q_code.setVisible(bool(q.code_context.strip()))
        self.q_code.setPlainText(q.code_context)
        if self.answer_widget is not None:
            self.answer_widget.setParent(None)
            self.answer_widget.deleteLater()
        w = make_answer_widget(q, s.state.option_order.get(q.id, []), s.state.display_order.get(q.id, []))
        rec = s.answer(q.id)
        w.set_value(rec.value)
        w.changed.connect(self._answer_changed)
        self.answer_host.addWidget(w)
        self.answer_widget = w
        self.confidence.blockSignals(True)
        self.confidence.setCurrentIndex(rec.confidence or 0)
        self.confidence.blockSignals(False)
        self.flag_btn.setText("⚑ Unflag" if rec.flagged else "⚑ Flag for review")
        locked = rec.checked_in_practice
        w.set_readonly(locked)
        self.feedback.show_text("")
        if locked:
            self._show_feedback_for(q)
        self.retry_btn.setVisible(locked and s.config.is_practice)
        self.check_btn.setEnabled(not locked)
        self.prev_btn.setEnabled(idx > 0 and s.can_go_to(idx - 1))
        self.prev_btn.setVisible(s.config.allow_revisit)
        self.next_btn.setEnabled(idx + 1 < s.total)
        self._refresh_nav()
        self._loading = False

    # ------------------------------------------------------------------ answer persistence
    def _answer_changed(self) -> None:
        if self._loading or not self.answer_widget:
            return
        if self.answer_widget.debounced:
            self.debounce.start()
        else:
            self._save_current()

    def _save_current(self) -> None:
        s = self.session
        if not s or not s.is_open or not self.answer_widget:
            return
        q = s.current_question
        if s.answer(q.id).checked_in_practice:
            return
        try:
            s.set_answer(q.id, self.answer_widget.value())
        except SessionError as exc:
            self._handle_session_error(exc)
            return
        self._refresh_nav()

    def flush(self) -> None:
        """Persist any pending typed answer immediately (navigation, close, submit)."""
        if self.debounce.isActive():
            self.debounce.stop()
            self._save_current()

    def _confidence_changed(self, idx: int) -> None:
        if self._loading or not self.session:
            return
        try:
            self.session.set_confidence(self.session.current_question.id, idx or None)
        except SessionError as exc:
            self._handle_session_error(exc)

    # ------------------------------------------------------------------ navigation
    def _goto(self, idx: int) -> None:
        if not self.session or idx == self.session.current_index:
            return
        self.flush()
        try:
            self.session.go_to(idx)
        except SessionError as exc:
            self._handle_session_error(exc)
            return
        self._show_question()

    def _next(self) -> None:
        if self.session and self.session.current_index + 1 < self.session.total:
            if not self.session.config.allow_revisit and not self.session.answer(self.session.current_question.id).is_answered():
                self.flush()
                if not self.session.answer(self.session.current_question.id).is_answered() and not confirm(
                        self, "Skip question?", "You cannot return to this question later. Continue without answering?"):
                    return
            self._goto(self.session.current_index + 1)

    def _prev(self) -> None:
        if self.session and self.session.current_index > 0:
            self._goto(self.session.current_index - 1)

    def _flag(self) -> None:
        if not self.session:
            return
        try:
            on = self.session.toggle_flag(self.session.current_question.id)
        except SessionError as exc:
            self._handle_session_error(exc)
            return
        self.flag_btn.setText("⚑ Unflag" if on else "⚑ Flag for review")
        self._refresh_nav()

    # ------------------------------------------------------------------ timer
    def _tick(self) -> None:
        s = self.session
        if not s:
            return
        rem = s.remaining_seconds()
        if rem is None:
            self.timer_label.setText("untimed")
            set_role(self.timer_label, "muted")
            return
        m, sec = divmod(int(rem + 0.999), 60)
        h, m = divmod(m, 60)
        self.timer_label.setText(f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}")
        set_role(self.timer_label, "bad" if rem <= 60 else ("warn" if rem <= 300 else "timer"))
        f = self.timer_label.font()
        f.setPointSize(20)
        self.timer_label.setFont(f)
        for threshold in (300, 60):
            if rem <= threshold and threshold not in self._warned and s.config.duration_minutes * 60 > threshold:
                self._warned.add(threshold)
                self.feedback.show_text(f"⏰ {threshold // 60} minute(s) remaining.", warn=True)
        if s.is_open and s.is_expired():
            self.flush()
            s.check_expiry()
        if not s.is_open:
            self._finish(time_up=True)

    # ------------------------------------------------------------------ immediate feedback / practice
    def _check(self) -> None:
        s = self.session
        if not s:
            return
        self.flush()
        self._save_current()
        q = s.current_question
        rec = s.answer(q.id)
        if not rec.is_answered():
            show_info(self, "Check answer", "Enter an answer first.")
            return
        result, ai_cand = self.ctx.grading.grade_deterministic(q, rec.value, s.config.scoring)
        if ai_cand and s.config.is_practice:
            decision = self.ctx.evaluator.plan(q)
            consent = ai_preflight(self, decision, "this question, its rubric/reference answer and your answer") if decision.possible else None
            if consent is not None:
                def work(cancel, progress):
                    return self.ctx.grading.grade_with_ai(q, rec.value, result, cloud_consent=consent, cancel=cancel)
                run_with_progress(self, "Evaluating answer", work, on_done=lambda r: self._apply_check(q, r),
                                  text="The AI is evaluating your answer against the rubric…")
                return
        self._apply_check(q, result)

    def _apply_check(self, q: Question, result) -> None:
        s = self.session
        if not s:
            return
        s.mark_checked(q.id)
        self._last_check = (q.id, result)
        if self.answer_widget:
            self.answer_widget.set_readonly(True)
        self.check_btn.setEnabled(False)
        self.retry_btn.setVisible(s.config.is_practice)
        self._show_feedback_for(q, result)

    def _show_feedback_for(self, q: Question, result=None) -> None:
        s = self.session
        if result is None:
            last = getattr(self, "_last_check", None)
            if last and last[0] == q.id:
                result = last[1]
            else:
                rec = s.answer(q.id)
                result, _ = self.ctx.grading.grade_deterministic(q, rec.value, s.config.scoring)
        icon = {"correct": "✅ Correct", "partial": "◐ Partially correct", "incorrect": "❌ Incorrect",
                "unanswered": "— No answer", "pending_review": "⌛ Needs rubric review"}[result.status.value]
        parts = [f"<b>{icon}</b> ({result.points:g}/{result.max_points:g})", html.escape(result.feedback)]
        if result.status != ResultStatus.CORRECT:
            parts.append(f"<b>Correct answer:</b> {html.escape(q.correct_answer_display())}")
        if q.explanation:
            parts.append(f"<b>Explanation:</b> {html.escape(q.explanation)}")
        if q.source_ref:
            parts.append(f"<b>Source:</b> {html.escape(q.source_ref.label())}")
        if result.method.value == "ai_rubric":
            parts.append("<i>AI-evaluated - may be imperfect.</i>")
        self.feedback.show_text("<br>".join(p for p in parts if p), warn=result.status != ResultStatus.CORRECT)

    def _retry(self) -> None:
        s = self.session
        if not s:
            return
        q = s.current_question
        s.reset_check(q.id)
        self._show_question()

    def _hint(self) -> None:
        s = self.session
        if not s:
            return
        q = s.current_question
        s.note_hint(q.id)
        if q.hint:
            self.feedback.show_text(f"💡 <b>Hint:</b> {html.escape(q.hint)}")
            return
        decision = self.ctx.router.plan(AIOperation.HINT)
        consent = ai_preflight(self, decision, "the question text")
        if consent is None:
            return
        run_with_progress(self, "Hint", lambda cancel, progress: self.ctx.evaluator.hint(q, cloud_consent=consent, cancel=cancel),
                          on_done=lambda h: self.feedback.show_text(f"💡 <b>Hint (AI-generated):</b> {html.escape(h)}"),
                          text="Asking the AI for a hint…")

    def _similar(self) -> None:
        s = self.session
        if not s:
            return
        q = s.current_question
        req = GenerationRequest(subject=q.subject, topics=[q.topic], difficulty=q.difficulty, count=1, types=[q.type],
                                language=s.config.language, avoid_texts=[x.text for x in s.state.questions],
                                focus_notes=f"Create a question similar in skill and difficulty to: {q.text[:300]}")
        consent = ai_preflight(self, self.ctx.generator.plan(req), "the subject, topic and the current question text")
        if consent is None:
            return

        def work(cancel, progress):
            return self.ctx.generator.generate(req, cloud_consent=consent, cancel=cancel, progress=progress)

        def done(res) -> None:
            if not res.questions:
                show_error(self, "Similar question", res.error or "No valid question was generated.")
                return
            new_q = res.questions[0]
            self.ctx.questions.save(new_q)
            idx = s.insert_question(new_q, s.current_index)
            self._build_nav()
            self._goto(idx)

        run_with_progress(self, "Similar question", work, on_done=done, text="Generating a similar question…")

    # ------------------------------------------------------------------ tools
    def _notes(self) -> None:
        if self._notes_dialog is None:
            d = QDialog(self)
            d.setWindowTitle("Notes (scratch pad - not saved or graded)")
            lay = QVBoxLayout(d)
            d.edit = QPlainTextEdit()  # type: ignore[attr-defined]
            lay.addWidget(d.edit)  # type: ignore[attr-defined]
            d.resize(420, 360)
            self._notes_dialog = d
        self._notes_dialog.show()
        self._notes_dialog.raise_()

    def _formulas(self) -> None:
        text = (self.session.config.formula_sheet_text if self.session else "") or "No formula sheet was provided for this exam."
        d = QDialog(self)
        d.setWindowTitle("Formula sheet")
        lay = QVBoxLayout(d)
        e = QPlainTextEdit(text)
        e.setReadOnly(True)
        lay.addWidget(e)
        d.resize(460, 420)
        d.show()

    def _toggle_full(self) -> None:
        self._full = not self._full
        self.focus_mode.emit(self._full)
        self.full_btn.setText("Exit full screen" if self._full else "Full screen")

    # ------------------------------------------------------------------ submission
    def _submit(self) -> None:
        s = self.session
        if not s:
            return
        self.flush()
        ok, msg = s.can_submit()
        if not ok:
            show_error(self, "Cannot submit yet", msg)
            return
        un = len(s.unanswered_indices())
        fl = len(s.flagged_indices())
        details = []
        if un:
            details.append(f"{un} question(s) are unanswered.")
        if fl:
            details.append(f"{fl} question(s) are flagged for review.")
        verb = "finish this practice session" if s.config.is_practice else "submit the exam"
        if not confirm(self, "Submit", f"Do you want to {verb}? You cannot change answers afterwards.\n\n" + "\n".join(details),
                       yes="Submit"):
            return
        try:
            s.submit()
        except SessionError as exc:
            show_error(self, "Cannot submit", str(exc))
            return
        self._finish()

    def _finish(self, time_up: bool = False) -> None:
        self.timer.stop()
        s = self.session
        if s is None:
            return
        if not s.save_now():
            show_error(self, "Saving problem", (s.last_save_error or "") + "\n\nThe exam will still be graded from memory.")
        if time_up:
            show_info(self, "Time is up", "The time limit has been reached. Your exam was submitted automatically with "
                                          "all answers saved so far.")
        if self._full:
            self._toggle_full()
        self.session = None
        self.finished.emit(s)

    def _handle_session_error(self, exc: SessionError) -> None:
        if self.session and not self.session.is_open:
            self._finish(time_up=self.session.state.submit_reason is not None and self.session.state.submit_reason.value != "user")
        else:
            show_error(self, "Exam", str(exc))

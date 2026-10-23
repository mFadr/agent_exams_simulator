"""Main application window: navigation, exam lifecycle, recovery."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..app.application import AppContext
from ..app.navigation import PAGE_TITLES, Page
from ..exams.exam_engine import ExamSession, SessionStatus
from ..exams.question_models import ExamConfig, Question
from ..grading.models import QuestionResult
from .dashboard import DashboardView
from .exam_session import ExamSessionView
from .exam_setup import ExamSetupView
from .materials_view import MaterialsView
from .question_bank import QuestionBankView
from .results_view import ResultsView
from .settings_view import AISettingsView, AppSettingsView
from .statistics_view import StatisticsView
from .theme import stylesheet
from .widgets import ai_preflight, button, card, confirm, label, run_with_progress, show_error, show_info

log = logging.getLogger(__name__)


class TakeExamPlaceholder(QWidget):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.addWidget(label("Take exam", "h1"))
        c, cl = card()
        cl.addWidget(label("No exam is running. Create one, or resume an unfinished exam below.", "muted"))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._resume)
        cl.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(button("Create a new exam", "primary", slot=lambda: win.go(Page.CREATE)))
        row.addStretch()
        cl.addLayout(row)
        lay.addWidget(c, 1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.list.clear()
        for r in self.win.ctx.exams.list_attempts(limit=200):
            if r.status == SessionStatus.IN_PROGRESS.value:
                it = QListWidgetItem(f"▶ {r.title} ({r.subject}) – started {r.started_at[:16].replace('T', ' ')}")
                it.setData(Qt.ItemDataRole.UserRole, r.attempt_id)
                self.list.addItem(it)
        if not self.list.count():
            self.list.addItem("No unfinished exams.")
        super().showEvent(e)

    def _resume(self, it: QListWidgetItem) -> None:
        aid = it.data(Qt.ItemDataRole.UserRole)
        if aid:
            self.win.resume(aid)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1360, 860)
        self.active_session: ExamSession | None = None

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(210)
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        h.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.dashboard = DashboardView(ctx)
        self.setup = ExamSetupView(ctx)
        self.session_view = ExamSessionView(ctx)
        self.placeholder = TakeExamPlaceholder(self)
        self.take_stack = QStackedWidget()
        self.take_stack.addWidget(self.placeholder)
        self.take_stack.addWidget(self.session_view)
        self.results = ResultsView(ctx)
        self.bank = QuestionBankView(ctx)
        self.materials = MaterialsView(ctx)
        self.stats = StatisticsView(ctx)
        self.ai_settings = AISettingsView(ctx)
        self.app_settings = AppSettingsView(ctx)
        self.pages = {
            Page.DASHBOARD: self.dashboard, Page.CREATE: self.setup, Page.TAKE: self.take_stack, Page.RESULTS: self.results,
            Page.BANK: self.bank, Page.MATERIALS: self.materials, Page.STATISTICS: self.stats,
            Page.AI_SETTINGS: self.ai_settings, Page.APP_SETTINGS: self.app_settings,
        }
        for p, w in self.pages.items():
            self.stack.addWidget(w)
            it = QListWidgetItem(PAGE_TITLES[p])
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.sidebar.addItem(it)
        self.sidebar.currentItemChanged.connect(self._nav_changed)

        # signals
        self.dashboard.navigate.connect(lambda name: self.go(Page(name)))
        self.dashboard.open_attempt.connect(self.show_attempt)
        self.dashboard.resume_attempt.connect(self.resume)
        self.dashboard.quick_start.connect(self._prefill)
        self.setup.start_requested.connect(self.start_exam)
        self.session_view.finished.connect(self.grade_and_show)
        self.session_view.focus_mode.connect(self._focus_mode)
        self.results.followup_requested.connect(self._prefill)
        self.materials.create_exam.connect(self._prefill)
        self.stats.open_attempt.connect(self.show_attempt)
        self.ai_settings.saved.connect(self._update_status)
        self.app_settings.theme_changed.connect(self.apply_theme)

        self.status_ai = QLabel()
        self.statusBar().addPermanentWidget(self.status_ai)
        self._update_status()
        self.apply_theme(ctx.settings.settings.app.theme)
        self.go(Page.DASHBOARD)
        QTimer.singleShot(300, self.recover_sessions)

    # ------------------------------------------------------------------ navigation
    def go(self, page: Page) -> None:
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == page:
                self.sidebar.setCurrentRow(i)
                return

    def _nav_changed(self, it: QListWidgetItem | None, prev: QListWidgetItem | None) -> None:
        if it is None:
            return
        page = it.data(Qt.ItemDataRole.UserRole)
        if self.active_session is not None and page != Page.TAKE:
            self.session_view.flush()
        self.stack.setCurrentWidget(self.pages[page])
        if page == Page.TAKE:
            self.take_stack.setCurrentWidget(self.session_view if self.active_session else self.placeholder)

    def apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(stylesheet(theme))

    def _update_status(self) -> None:
        self.status_ai.setText(f"AI: {self.ctx.router.status_line()}   ·   Data: {self.ctx.paths.root}")

    def _focus_mode(self, on: bool) -> None:
        self.sidebar.setVisible(not on)
        self.statusBar().setVisible(not on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    def _prefill(self, cfg: ExamConfig) -> None:
        if self.active_session is not None:
            show_info(self, "Exam in progress", "Finish or submit the running exam first.")
            self.go(Page.TAKE)
            return
        self.setup.prefill(cfg)
        self.go(Page.CREATE)

    # ------------------------------------------------------------------ exam lifecycle
    def start_exam(self, cfg: ExamConfig, questions: list[Question]) -> None:
        if self.active_session is not None:
            show_error(self, "Exam in progress", "Another exam is already running.")
            return
        try:
            session = self.ctx.start_exam(cfg, questions)
        except ValueError as exc:
            show_error(self, "Cannot start", str(exc))
            return
        self._open_session(session)

    def _open_session(self, session: ExamSession) -> None:
        self.active_session = session
        self.session_view.load(session)
        self.take_stack.setCurrentWidget(self.session_view)
        self.go(Page.TAKE)

    def resume(self, attempt_id: str) -> None:
        if self.active_session is not None:
            if self.active_session.state.attempt_id == attempt_id:
                self.go(Page.TAKE)
                return
            show_error(self, "Exam in progress", "Finish the running exam first.")
            return
        session = self.ctx.resume_session(attempt_id)
        if session is None:
            show_error(self, "Resume", "The saved exam could not be loaded (its data may be damaged).")
            return
        if not session.is_open:
            show_info(self, "Time expired", "The time limit for this exam passed while it was closed. "
                                            "It has been submitted with the answers saved before the deadline.")
            self.grade_and_show(session)
            return
        self._open_session(session)

    def recover_sessions(self) -> None:
        """Startup: resume interrupted exams and grade ones that were submitted but never graded."""
        if self.ctx.restored_from_backup:
            show_info(self, "Backup restored", "Your data was restored from the backup.")
        for aid in self.ctx.exams.ungraded_attempt_ids():
            st = self.ctx.exams.load_state(aid)
            if st:
                self.grade_and_show(ExamSession(st, persist=self.ctx.persist_session), interactive=False)
        open_ids = self.ctx.exams.open_attempt_ids()
        if not open_ids:
            return
        st = self.ctx.exams.load_state(open_ids[0])
        if st is None:
            return
        rem = ""
        if st.deadline:
            session = ExamSession(st)
            secs = session.remaining_seconds() or 0
            rem = f" Time remaining: {int(secs // 60)} min {int(secs % 60)} s." if secs > 0 else " Its time limit has passed."
        if confirm(self, "Unfinished exam found",
                   f"'{st.config.title}' was interrupted (all answers up to the last change were saved).{rem}\n\nResume it now?",
                   yes="Resume", no="Later"):
            self.resume(open_ids[0])

    def grade_and_show(self, session: ExamSession, interactive: bool = True) -> None:
        self.active_session = None
        self.take_stack.setCurrentWidget(self.placeholder)
        qs = session.state.questions
        answers = session.state.answers
        rules = session.config.scoring
        needs_ai = [q for q in qs if self.ctx.grading.grade_deterministic(q, answers[q.id].value if q.id in answers else None, rules)[1]]
        consent: bool | None = None
        if needs_ai and interactive:
            decision = self.ctx.evaluator.plan(needs_ai[0])
            if decision.possible:
                consent = ai_preflight(self, decision, f"{len(needs_ai)} question(s) with their rubrics and your written answers")
        use_ai = consent is not None

        def work(cancel, progress) -> list[QuestionResult]:
            return self.ctx.grading.grade_all(qs, answers, rules, use_ai=use_ai, cloud_consent=bool(consent),
                                              cancel=cancel, progress=progress)

        def done(results: list[QuestionResult]) -> None:
            try:
                summary = self.ctx.finalize(session, results)
            except Exception as exc:
                log.exception("Saving results failed")
                show_error(self, "Results", f"The results could not be saved ({type(exc).__name__}). They are shown but may be lost.")
                from ..exams.scoring import compute_summary
                summary = compute_summary(qs, results, session.config)
            self.results.load(session.state, summary, results)
            self.go(Page.RESULTS)

        text = "Grading answers… (written answers are evaluated by AI)" if use_ai else "Grading answers…"
        # Cancelling stops further AI evaluation; already-graded answers are kept and the rest stay "pending review".
        run_with_progress(self, "Grading", work, on_done=done, text=text, deliver_partial=True)

    def show_attempt(self, attempt_id: str) -> None:
        if self.results.load_attempt(attempt_id):
            self.go(Page.RESULTS)

    # ------------------------------------------------------------------ shutdown
    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        if self.active_session is not None:
            self.session_view.flush()
            self.active_session.save_now()
            timed = self.active_session.state.deadline is not None
            msg = "Your answers are saved and you can resume this exam later."
            if timed:
                msg += " The exam timer keeps running while the application is closed."
            if not confirm(self, "Exam in progress", msg + "\n\nClose the application?", yes="Close"):
                e.ignore()
                return
        self.ctx.db.close()
        e.accept()

"""Home dashboard: progress overview, recent exams and suggested actions."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QTextBrowser, QVBoxLayout, QWidget

from ..app.application import AppContext
from ..exams.question_models import ExamMode, preset_config
from ..learning.weakness_detection import detect
from .widgets import StatTile, button, card, label


class DashboardView(QWidget):
    navigate = Signal(str)
    open_attempt = Signal(str)
    resume_attempt = Signal(str)
    quick_start = Signal(object)  # ExamConfig to prefill

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        self.hello = label("", "h1")
        root.addWidget(self.hello)
        self.ai_line = label("", "muted")
        root.addWidget(self.ai_line)

        tiles = QHBoxLayout()
        self.t_exams = StatTile("Graded exams")
        self.t_avg = StatTile("Average score")
        self.t_best = StatTile("Best score")
        self.t_bank = StatTile("Questions in bank")
        for t in (self.t_exams, self.t_avg, self.t_best, self.t_bank):
            tiles.addWidget(t)
        root.addLayout(tiles)

        actions = QHBoxLayout()
        actions.addWidget(button("New exam", "primary", slot=lambda: self.navigate.emit("create")))
        actions.addWidget(button("Quick practice", slot=self._quick_practice))
        actions.addWidget(button("Practise weak areas", slot=self._weakness))
        actions.addWidget(button("Import study material", slot=lambda: self.navigate.emit("materials")))
        actions.addStretch()
        root.addLayout(actions)

        row = QHBoxLayout()
        c1, l1 = card()
        l1.addWidget(label("Recent exams", "h2"))
        self.recent = QListWidget()
        self.recent.itemDoubleClicked.connect(self._open_item)
        l1.addWidget(self.recent, 1)
        l1.addWidget(label("Double-click to review or resume.", "muted"))
        row.addWidget(c1, 1)
        c2, l2 = card()
        l2.addWidget(label("Suggested study actions", "h2"))
        self.suggest = QTextBrowser()
        l2.addWidget(self.suggest, 1)
        row.addWidget(c2, 1)
        root.addLayout(row, 1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        name = self.ctx.settings.settings.app.user_name or "Student"
        self.hello.setText(f"Welcome back, {name}")
        self.ai_line.setText("AI: " + self.ctx.router.status_line())
        tot = self.ctx.analytics.totals()
        self.t_exams.set(str(tot["attempts"]))
        self.t_avg.set("—" if tot["average"] is None else f"{tot['average']:.0f} %")
        self.t_best.set("—" if tot["best"] is None else f"{tot['best']:.0f} %")
        self.t_bank.set(str(self.ctx.questions.count()))
        self.recent.clear()
        for r in self.ctx.exams.list_attempts(limit=25):
            if r.status == "in_progress":
                text = f"▶ IN PROGRESS – {r.title} ({r.subject}) – started {r.started_at[:16].replace('T', ' ')}"
            else:
                score = "—" if r.percentage is None else f"{r.percentage:.0f}%"
                res = "" if r.passed is None else (" ✅" if r.passed else " ✗")
                text = f"{r.started_at[:10]}  {r.title} ({r.subject}) – {score}{res}"
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, (r.attempt_id, r.status))
            self.recent.addItem(it)
        if not self.recent.count():
            self.recent.addItem("No exams yet – start with 'New exam' or 'Quick practice'.")
        rep = detect(self.ctx.analytics.records())
        parts = ["<ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in rep.recommendations) + "</ul>"]
        strong = [i for i in rep.insights if i.strength == "strong"][:3]
        if strong:
            parts.append("<p><b>Patterns:</b></p><ul>" + "".join(f"<li>{html.escape(i.text)}</li>" for i in strong) + "</ul>")
        open_ids = self.ctx.exams.open_attempt_ids()
        if open_ids:
            parts.insert(0, f"<p><b>You have {len(open_ids)} unfinished exam(s).</b> Double-click it in the list to resume.</p>")
        self.suggest.setHtml("".join(parts))

    def _open_item(self, it: QListWidgetItem) -> None:
        data = it.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        aid, status = data
        if status == "in_progress":
            self.resume_attempt.emit(aid)
        else:
            self.open_attempt.emit(aid)

    def _quick_practice(self) -> None:
        self.quick_start.emit(preset_config("Quick Practice"))

    def _weakness(self) -> None:
        cfg = preset_config("Standard Exam")
        cfg.mode = ExamMode.WEAKNESS
        cfg.title = "Weakness practice"
        rep = detect(self.ctx.analytics.records())
        weak = rep.weak_topics()
        if weak:
            cfg.subject = weak[0].subject
            cfg.topics = [t.topic for t in weak if t.subject == cfg.subject][:6]
        self.quick_start.emit(cfg)

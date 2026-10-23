"""Long-term statistics and weakness analysis."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QPieSeries,
    QValueAxis,
)
from PySide6.QtCore import QDate, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..app.application import AppContext
from ..learning.weakness_detection import detect
from ..reports.export_service import export_history_csv
from ..reports.report_generator import type_label
from .theme import colors
from .widgets import button, card, label, show_info


class StatisticsView(QWidget):
    open_attempt = Signal(str)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.addWidget(label("Statistics & learning profile", "h1"))
        f = QHBoxLayout()
        self.subject = QComboBox()
        self.use_dates = QCheckBox("Date range:")
        self.d_from = QDateEdit(QDate.currentDate().addMonths(-3))
        self.d_to = QDateEdit(QDate.currentDate())
        for d in (self.d_from, self.d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        f.addWidget(label("Subject:", wrap=False))
        f.addWidget(self.subject)
        f.addWidget(self.use_dates)
        f.addWidget(self.d_from)
        f.addWidget(label("–", wrap=False))
        f.addWidget(self.d_to)
        f.addWidget(button("Apply", slot=self.refresh))
        f.addStretch()
        f.addWidget(button("Export history CSV…", slot=self._export))
        root.addLayout(f)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        # charts tab
        charts = QWidget()
        g = QGridLayout(charts)
        self.chart_scores = self._view()
        self.chart_topics = self._view()
        self.chart_diff = self._view()
        self.chart_status = self._view()
        g.addWidget(self.chart_scores, 0, 0)
        g.addWidget(self.chart_status, 0, 1)
        g.addWidget(self.chart_topics, 1, 0)
        g.addWidget(self.chart_diff, 1, 1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(charts)
        self.tabs.addTab(sa, "Charts")
        # weakness tab
        wk = QWidget()
        wl = QVBoxLayout(wk)
        wl.addWidget(label("Calculated from your graded answers (practice sessions excluded). Topics need at least 3 answers "
                           "before any conclusion is drawn; 'tentative' insights rest on little evidence.", "muted"))
        self.topic_table = QTableWidget(0, 7)
        self.topic_table.setHorizontalHeaderLabels(["Subject", "Topic", "Answers", "Exams", "Accuracy", "95% range", "Assessment"])
        self.topic_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.topic_table.verticalHeader().setVisible(False)
        self.topic_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        wl.addWidget(self.topic_table, 2)
        self.insights = QTextBrowser()
        wl.addWidget(self.insights, 1)
        self.tabs.addTab(wk, "Weaknesses & insights")
        # history tab
        hist = QWidget()
        hl = QVBoxLayout(hist)
        self.hist_table = QTableWidget(0, 7)
        self.hist_table.setHorizontalHeaderLabels(["Date", "Exam", "Subject", "Mode", "Attempt", "Score", "Status"])
        self.hist_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.hist_table.verticalHeader().setVisible(False)
        self.hist_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hist_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.hist_table.doubleClicked.connect(self._open)
        hl.addWidget(label("Double-click an attempt to open its review.", "muted"))
        hl.addWidget(self.hist_table)
        self.tabs.addTab(hist, "Exam history")
        # by type / time
        tt = QWidget()
        tl = QVBoxLayout(tt)
        self.type_table = QTableWidget(0, 5)
        self.type_table.setHorizontalHeaderLabels(["Question type", "Answers", "Accuracy", "Correct", "Avg. time / question"])
        self.type_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.type_table.verticalHeader().setVisible(False)
        self.type_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tl.addWidget(self.type_table)
        self.tabs.addTab(tt, "By question type & time")
        self._history_rows = []

    def _view(self) -> QChartView:
        v = QChartView()
        v.setRenderHint(QPainter.RenderHint.Antialiasing)
        v.setMinimumHeight(280)
        return v

    def showEvent(self, e) -> None:  # noqa: N802
        cur = self.subject.currentData()
        self.subject.blockSignals(True)
        self.subject.clear()
        self.subject.addItem("All subjects", "")
        for s in self.ctx.analytics.subjects():
            self.subject.addItem(s, s)
        self.subject.setCurrentIndex(max(0, self.subject.findData(cur)))
        self.subject.blockSignals(False)
        self.refresh()
        super().showEvent(e)

    def _filters(self) -> tuple[str, str, str]:
        if not self.use_dates.isChecked():
            return self.subject.currentData() or "", "", ""
        return (self.subject.currentData() or "", self.d_from.date().toString("yyyy-MM-dd"),
                self.d_to.date().addDays(1).toString("yyyy-MM-dd"))

    def _style(self, chart: QChart, title: str) -> QChart:
        c = colors(self.ctx.settings.settings.app.theme)
        chart.setTitle(title)
        chart.setBackgroundBrush(QColor(c["surface"]))
        chart.setTitleBrush(QColor(c["text"]))
        chart.legend().setLabelColor(QColor(c["text"]))
        chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        return chart

    def _axis_color(self, *axes) -> None:
        c = colors(self.ctx.settings.settings.app.theme)
        for a in axes:
            a.setLabelsColor(QColor(c["text"]))
            a.setGridLineColor(QColor(c["border"]))

    def refresh(self) -> None:
        subject, dfrom, dto = self._filters()
        a = self.ctx.analytics
        c = colors(self.ctx.settings.settings.app.theme)

        # scores over time
        hist = a.score_history(subject, dfrom, dto)
        ch = self._style(QChart(), "Exam scores over time (%)")
        s = QLineSeries()
        s.setName("Score")
        for i, h in enumerate(hist):
            s.append(QPointF(i + 1, h["percentage"]))
        s.setPointsVisible(True)
        s.setColor(QColor(c["accent"]))
        ch.addSeries(s)
        ax = QValueAxis()
        ax.setRange(1, max(2, len(hist)))
        ax.setLabelFormat("%d")
        ax.setTitleText("attempt")
        ay = QValueAxis()
        ay.setRange(0, 100)
        self._axis_color(ax, ay)
        ch.addAxis(ax, Qt.AlignmentFlag.AlignBottom)
        ch.addAxis(ay, Qt.AlignmentFlag.AlignLeft)
        s.attachAxis(ax)
        s.attachAxis(ay)
        ch.legend().hide()
        self.chart_scores.setChart(ch)

        self.chart_topics.setChart(self._bar("Accuracy by topic (%)", a.breakdown("topic", subject, dfrom, dto)[:15], c))
        diff = sorted(a.breakdown("difficulty", subject, dfrom, dto), key=lambda d: ["easy", "medium", "hard", "expert"].index(d["key"]) if d["key"] in ("easy", "medium", "hard", "expert") else 9)
        self.chart_diff.setChart(self._bar("Accuracy by difficulty (%)", diff, c))

        counts = a.status_counts(subject, dfrom, dto)
        pie = QPieSeries()
        for key, lab, col in (("correct", "Correct", c["ok"]), ("partial", "Partial", c["warn"]),
                              ("incorrect", "Incorrect", c["bad"]), ("unanswered", "Unanswered", c["muted"])):
            if counts.get(key):
                sl = pie.append(f"{lab} ({counts[key]})", counts[key])
                sl.setColor(QColor(col))
                sl.setLabelVisible(True)
                sl.setLabelColor(QColor(c["text"]))
        pc = self._style(QChart(), "Correct vs incorrect answers")
        pc.addSeries(pie)
        pc.legend().hide()
        self.chart_status.setChart(pc)

        # weakness analysis
        rep = detect(a.records(subject, dfrom, dto))
        self.topic_table.setRowCount(len(rep.topics))
        for i, t in enumerate(rep.topics):
            vals = [t.subject, t.topic, str(t.n), str(t.attempts), f"{t.accuracy:.0%}", f"{t.low:.0%}–{t.high:.0%}", t.label]
            for j, v in enumerate(vals):
                self.topic_table.setItem(i, j, QTableWidgetItem(v))
        parts = ["<h3>Detected patterns <span style='font-weight:normal;font-size:9pt'>(rule-based, calculated – not AI)</span></h3>"]
        if rep.insights:
            parts.append("<ul>" + "".join(
                f"<li><b>{'●' if i.strength == 'strong' else '○'} {html.escape(i.text)}</b> <span style='color:gray'>"
                f"[{i.strength}; evidence: {html.escape(i.evidence)}]</span></li>" for i in rep.insights) + "</ul>")
        else:
            parts.append("<p>No patterns detected yet.</p>")
        parts.append("<h3>Recommended next steps</h3><ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in rep.recommendations) + "</ul>")
        self.insights.setHtml("".join(parts))

        # history
        rows = self.ctx.exams.list_attempts(subject=subject, date_from=dfrom, date_to=dto)
        self._history_rows = rows
        self.hist_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            score = "—" if r.percentage is None else f"{r.percentage:.1f}%" + (" (prov.)" if r.provisional else "")
            status = r.status if r.passed is None else ("passed" if r.passed else "not passed")
            for j, v in enumerate([r.started_at[:16].replace("T", " "), r.title, r.subject, r.mode, str(r.attempt_number), score, status]):
                self.hist_table.setItem(i, j, QTableWidgetItem(v))

        types = a.breakdown("qtype", subject, dfrom, dto)
        self.type_table.setRowCount(len(types))
        for i, t in enumerate(types):
            for j, v in enumerate([type_label(t["key"]), str(t["n"]), f"{t['pct']:.0f}%", str(t["correct"]), f"{t['avg_time']:.0f} s"]):
                self.type_table.setItem(i, j, QTableWidgetItem(v))

    def _bar(self, title: str, data: list[dict], c: dict) -> QChart:
        chart = self._style(QChart(), title)
        bs = QBarSet("Accuracy")
        bs.setColor(QColor(c["accent"]))
        cats = []
        for d in data:
            bs.append(round(d["pct"], 1))
            cats.append(f"{d['key'][:18]} ({d['n']})")
        series = QBarSeries()
        series.append(bs)
        chart.addSeries(series)
        ax = QBarCategoryAxis()
        ax.append(cats or ["no data"])
        ay = QValueAxis()
        ay.setRange(0, 100)
        self._axis_color(ax, ay)
        chart.addAxis(ax, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(ay, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.legend().hide()
        return chart

    def _open(self, idx) -> None:
        r = self._history_rows[idx.row()]
        if r.percentage is None:
            show_info(self, "Exam", "This attempt has not been graded yet (it may still be in progress).")
            return
        self.open_attempt.emit(r.attempt_id)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export history", str(self.ctx.paths.exports_dir / "exam_history.csv"), "CSV (*.csv)")
        if path:
            export_history_csv(self.ctx.exams.list_attempts(), Path(path))
            show_info(self, "Export", f"Saved to {path}")

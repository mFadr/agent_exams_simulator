"""Post-exam results dashboard and per-question review."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..app.application import AppContext
from ..config.settings import AIOperation
from ..exams.exam_engine import SessionState
from ..exams.question_models import ExamConfig, ExamMode, QUESTION_TYPE_LABELS
from ..grading.models import METHOD_LABELS, ExamSummary, GradingMethod, QuestionResult, ResultStatus
from ..reports.export_service import export_report_csv, export_report_pdf
from ..reports.report_generator import ExamReport, build_report
from ..security.validation import safe_filename
from .widgets import Banner, StatTile, ai_preflight, button, card, label, run_with_progress, show_error, show_info

STATUS_ICON = {
    ResultStatus.CORRECT: "✅", ResultStatus.PARTIAL: "◐", ResultStatus.INCORRECT: "❌",
    ResultStatus.UNANSWERED: "—", ResultStatus.PENDING_REVIEW: "⌛",
}


class OverrideDialog(QDialog):
    def __init__(self, parent: QWidget, r: QuestionResult) -> None:
        super().__init__(parent)
        self.setWindowTitle("Adjust points")
        lay = QFormLayout(self)
        self.points = QDoubleSpinBox()
        self.points.setDecimals(2)
        self.points.setRange(-r.max_points, r.max_points)
        self.points.setSingleStep(0.5)
        self.points.setValue(r.points)
        self.note = QLineEdit(r.override_note)
        self.note.setPlaceholderText("Reason (e.g. 'equivalent wording accepted')")
        lay.addRow(label(f"Current: {r.points:g} / {r.max_points:g} ({METHOD_LABELS[r.method]})", "muted"))
        lay.addRow("New points:", self.points)
        lay.addRow("Note:", self.note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


class ResultsView(QWidget):
    followup_requested = Signal(object)  # ExamConfig
    changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.state: SessionState | None = None
        self.summary: ExamSummary | None = None
        self.results: list[QuestionResult] = []
        self.report: ExamReport | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        top = QHBoxLayout()
        self.title = label("Results", "h1")
        top.addWidget(self.title, 1)
        self.pdf_btn = button("Export PDF", slot=self._export_pdf)
        self.csv_btn = button("Export CSV", slot=self._export_csv)
        self.follow_btn = button("Follow-up exam on weak areas", "primary", slot=self._followup)
        for b in (self.pdf_btn, self.csv_btn, self.follow_btn):
            top.addWidget(b)
        root.addLayout(top)
        self.subtitle = label("", "muted")
        root.addWidget(self.subtitle)

        tiles = QHBoxLayout()
        self.t_score = StatTile("Score (calculated)")
        self.t_pct = StatTile("Percentage")
        self.t_pass = StatTile("Status")
        self.t_counts = StatTile("Answers")
        for t in (self.t_score, self.t_pct, self.t_pass, self.t_counts):
            tiles.addWidget(t)
        root.addLayout(tiles)
        self.banner = Banner(warn=True)
        root.addWidget(self.banner)
        self.pending_btn = button("Evaluate pending answers with AI", slot=self._ai_grade_pending)
        root.addWidget(self.pending_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split, 1)

        # breakdown tables
        bd = QWidget()
        bdl = QHBoxLayout(bd)
        bdl.setContentsMargins(0, 0, 0, 0)
        self.topic_table = self._table(["Topic", "Points", "%", "Correct"])
        self.type_table = self._table(["Question type", "Points", "%", "Correct"])
        self.diff_table = self._table(["Difficulty", "Points", "%", "Correct"])
        for t, title in ((self.topic_table, "By topic"), (self.type_table, "By question type"), (self.diff_table, "By difficulty")):
            c, cl = card()
            cl.addWidget(label(title, "h2"))
            cl.addWidget(t)
            bdl.addWidget(c)
        split.addWidget(bd)

        # per-question review
        rv = QWidget()
        rvl = QHBoxLayout(rv)
        rvl.setContentsMargins(0, 0, 0, 0)
        left, ll = card()
        frow = QHBoxLayout()
        frow.addWidget(label("Show:", wrap=False))
        self.filter = QComboBox()
        for text, key in (("All questions", "all"), ("Incorrect / partial", "incorrect"), ("Unanswered", "unanswered"),
                          ("Flagged during exam", "flagged"), ("Needs review", "review")):
            self.filter.addItem(text, key)
        self.filter.currentIndexChanged.connect(self._fill_list)
        frow.addWidget(self.filter, 1)
        ll.addLayout(frow)
        self.qlist = QListWidget()
        self.qlist.currentItemChanged.connect(self._show_detail)
        ll.addWidget(self.qlist, 1)
        left.setMaximumWidth(380)
        rvl.addWidget(left)
        right, rl = card()
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        rl.addWidget(self.detail, 1)
        brow = QHBoxLayout()
        self.override_btn = button("Adjust points…", slot=self._override, tooltip="Inspect and override the grading decision")
        self.explain_btn = button("Explain my mistake (AI)", slot=self._explain)
        brow.addWidget(self.override_btn)
        brow.addWidget(self.explain_btn)
        brow.addStretch()
        rl.addLayout(brow)
        rvl.addWidget(right, 1)
        split.addWidget(rv)
        split.setSizes([220, 520])

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        t.setWordWrap(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        return t

    # ------------------------------------------------------------------ loading
    def load_attempt(self, attempt_id: str) -> bool:
        state = self.ctx.exams.load_state(attempt_id)
        res = self.ctx.exams.load_results(attempt_id)
        if not state or not res:
            show_error(self, "Results", "The results for this attempt could not be loaded.")
            return False
        self.load(state, res[0], res[1])
        return True

    def load(self, state: SessionState, summary: ExamSummary, results: list[QuestionResult]) -> None:
        self.state, self.summary, self.results = state, summary, results
        self.report = build_report(state, summary, results)
        rep = self.report
        s = summary
        self.title.setText(f"Results – {rep.title}")
        self.subtitle.setText(f"{rep.subject} · attempt {rep.attempt_number} · started {rep.started_at} · submitted {rep.submitted_at}. {rep.submit_reason}")
        self.t_score.set(f"{s.points:g} / {s.max_points:g}", "points")
        self.t_pct.set(f"{s.percentage:.1f} %", "provisional" if s.provisional else "")
        if s.passed is None:
            self.t_pass.set("Practice", "no pass mark")
        else:
            self.t_pass.set("PASSED" if s.passed else "NOT PASSED", f"threshold {s.pass_threshold:g} %" + (" · provisional" if s.provisional else ""),
                            role="ok" if s.passed else "bad")
        self.t_counts.set(f"{s.correct} ✅  {s.partial} ◐  {s.incorrect} ❌", f"{s.unanswered} unanswered · {s.pending_review} pending review")
        notes = list(rep.notes)
        self.banner.show_text("<br>".join(html.escape(n) for n in notes), warn=True)
        pending = [r for r in results if r.status == ResultStatus.PENDING_REVIEW or (r.needs_review and r.method != GradingMethod.AI_RUBRIC and not r.overridden)]
        self.pending_btn.setVisible(bool(pending))
        for table, buckets in ((self.topic_table, s.by_topic), (self.type_table, s.by_type), (self.diff_table, s.by_difficulty)):
            table.setRowCount(len(buckets))
            for i, b in enumerate(buckets):
                for j, v in enumerate([b.key, f"{b.points:g}/{b.max_points:g}", f"{b.percentage:.0f}%", f"{b.correct}/{b.count}"]):
                    table.setItem(i, j, QTableWidgetItem(v))
        self._fill_list()

    def _fill_list(self) -> None:
        if not self.report:
            return
        current = self.qlist.currentItem().data(Qt.ItemDataRole.UserRole) if self.qlist.currentItem() else None
        self.qlist.clear()
        for row in self.report.filtered(self.filter.currentData()):
            r = row.result
            flag = " ⚑" if row.flagged else ""
            review = " ⚠" if r.needs_review else ""
            it = QListWidgetItem(f"{STATUS_ICON[r.status]} Q{row.number}{flag}{review}  {r.points:g}/{r.max_points:g} – {row.question.text[:60]}")
            it.setData(Qt.ItemDataRole.UserRole, row.number)
            self.qlist.addItem(it)
            if row.number == current:
                self.qlist.setCurrentItem(it)
        if self.qlist.currentItem() is None and self.qlist.count():
            self.qlist.setCurrentRow(0)
        if not self.qlist.count():
            self.detail.setHtml("<p>No questions match this filter.</p>")

    def _current_row(self):
        it = self.qlist.currentItem()
        if not it or not self.report:
            return None
        n = it.data(Qt.ItemDataRole.UserRole)
        return next((r for r in self.report.rows if r.number == n), None)

    def _show_detail(self, *_: object) -> None:
        row = self._current_row()
        if row is None:
            return
        q, r = row.question, row.result
        e = html.escape
        parts = [
            f"<h3>Question {row.number} <span style='font-weight:normal'>· {e(q.topic)} · {QUESTION_TYPE_LABELS[q.type]} · {q.difficulty.value}</span></h3>",
            f"<p>{e(q.text).replace(chr(10), '<br>')}</p>",
        ]
        if q.code_context:
            parts.append(f"<pre>{e(q.code_context)}</pre>")
        if q.options and q.type.value != "true_false":
            parts.append("<p><b>Options:</b> " + " | ".join(e(o.text) for o in q.options) + "</p>")
        parts.append(f"<p><b>Your answer:</b><br><span style='white-space:pre-wrap'>{e(row.answer_text)}</span></p>")
        parts.append(f"<p><b>Correct answer / reference:</b> {e(q.correct_answer_display())}</p>")
        parts.append(f"<p><b>Points:</b> {r.points:g} / {r.max_points:g} &nbsp; <b>Status:</b> {STATUS_ICON[r.status]} {r.status.value} "
                     f"&nbsp; <b>Graded by:</b> {METHOD_LABELS[r.method]}"
                     + (f" ({e(r.provider)})" if r.provider else "")
                     + (f" &nbsp; <b>Confidence:</b> {r.confidence:.0%}" if r.confidence is not None else "") + "</p>")
        if r.needs_review:
            parts.append("<p style='color:#b26a00'><b>⚠ Uncertain grading - please review and adjust if needed.</b></p>")
        if r.overridden:
            parts.append(f"<p><b>Manually adjusted:</b> {e(r.override_note) or '(no note)'}</p>")
        parts.append(f"<p><b>Why this score:</b><br>{e(r.feedback).replace(chr(10), '<br>')}</p>")
        if r.criteria:
            rows = "".join(
                f"<tr><td>{'✔' if c.satisfied else '✘'}</td><td>{e(c.description)}</td><td>{c.awarded:g}/{c.max_points:g}</td><td>{e(c.comment)}</td></tr>"
                for c in r.criteria)
            parts.append("<p><b>Rubric criteria</b></p><table border='1' cellpadding='4' cellspacing='0' width='100%'>"
                         "<tr><th></th><th>Criterion</th><th>Points</th><th>Comment</th></tr>" + rows + "</table>")
        if r.error_tags:
            parts.append(f"<p><b>Error type(s):</b> {e(', '.join(r.error_tags))}</p>")
        if r.misconception:
            parts.append(f"<p><b>Likely misconception:</b> {e(r.misconception)}</p>")
        if q.explanation:
            parts.append(f"<p><b>Explanation of the expected reasoning:</b><br>{e(q.explanation)}</p>")
        if r.recommendation:
            parts.append(f"<p><b>Recommended action:</b> {e(r.recommendation)}</p>")
        elif r.status != ResultStatus.CORRECT:
            parts.append(f"<p><b>Recommended action:</b> Review '{e(q.topic)}'"
                         + (f" – focus on: {e(', '.join(q.skills))}" if q.skills else "") + ", then retry a similar question in practice mode.</p>")
        if q.source_ref:
            parts.append(f"<p><b>Source:</b> {e(q.source_ref.label())}</p>")
        if r.ai_explanation:
            parts.append(f"<div style='border-left:3px solid #2f6fdf;padding-left:8px'><b>AI tutor explanation</b> "
                         f"<i>(AI-generated, not part of the score; may contain errors)</i><br>{e(r.ai_explanation).replace(chr(10), '<br>')}</div>")
        parts.append(f"<p style='color:gray'>Time on question: {row.time_spent_s:.0f}s · Provenance: {q.provenance.value} · Review status: {q.review_status.value}</p>")
        self.detail.setHtml("".join(parts))
        self.explain_btn.setEnabled(r.status != ResultStatus.CORRECT)

    # ------------------------------------------------------------------ actions
    def _replace_result(self, new: QuestionResult) -> None:
        assert self.state
        self.results = [new if r.question_id == new.question_id else r for r in self.results]
        summary = self.ctx.update_result(self.state, self.results)
        self.load(self.state, summary, self.results)
        self.changed.emit()

    def _override(self) -> None:
        row = self._current_row()
        if not row:
            return
        dlg = OverrideDialog(self, row.result)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._replace_result(self.ctx.grading.override(row.result, dlg.points.value(), dlg.note.text()))

    def _explain(self) -> None:
        row = self._current_row()
        if not row:
            return
        decision = self.ctx.router.plan(AIOperation.EXPLAIN)
        consent = ai_preflight(self, decision, "this question, the correct answer and your answer")
        if consent is None:
            return
        q, r = row.question, row.result

        def work(cancel, progress):
            return self.ctx.evaluator.explain(q, row.answer_text, q.correct_answer_display(), r.feedback,
                                              cloud_consent=consent, cancel=cancel)

        def done(out: dict) -> None:
            new = r.model_copy(deep=True)
            new.ai_explanation = out["explanation"] + f"\n[{out['provider']}]"
            if out.get("misconception") and not new.misconception:
                new.misconception = out["misconception"]
            if out.get("recommendation"):
                new.recommendation = out["recommendation"]
            self._replace_result(new)

        run_with_progress(self, "AI explanation", work, on_done=done, text="Generating an explanation…")

    def _ai_grade_pending(self) -> None:
        if not self.state:
            return
        qmap = {q.id: q for q in self.state.questions}
        todo = [r for r in self.results if (r.status == ResultStatus.PENDING_REVIEW or (r.needs_review and r.method != GradingMethod.AI_RUBRIC)) and not r.overridden]
        if not todo:
            return
        decision = self.ctx.evaluator.plan(qmap[todo[0].question_id])
        consent = ai_preflight(self, decision, f"{len(todo)} question(s) with rubrics and your written answers")
        if consent is None:
            return
        state = self.state

        def work(cancel, progress):
            out = []
            for i, r in enumerate(todo):
                cancel.raise_if_cancelled()
                progress(f"Evaluating answer {i + 1}/{len(todo)}…", i, len(todo))
                q = qmap[r.question_id]
                ans = state.answers.get(q.id)
                out.append(self.ctx.grading.grade_with_ai(q, ans.value if ans else "", r, cloud_consent=consent, cancel=cancel))
            return out

        def done(new_results: list[QuestionResult]) -> None:
            by = {r.question_id: r for r in new_results}
            self.results = [by.get(r.question_id, r) for r in self.results]
            summary = self.ctx.update_result(state, self.results)
            self.load(state, summary, self.results)
            self.changed.emit()

        run_with_progress(self, "AI grading", work, on_done=done, text="Evaluating answers…")

    def _export_pdf(self) -> None:
        if not self.report:
            return
        default = str(self.ctx.paths.exports_dir / (safe_filename(self.report.title) + ".pdf"))
        path, _ = QFileDialog.getSaveFileName(self, "Export PDF", default, "PDF (*.pdf)")
        if not path:
            return
        rep = self.report
        run_with_progress(self, "Export PDF", lambda: export_report_pdf(rep, Path(path)), pass_control=False,
                          on_done=lambda p: show_info(self, "Export", f"Saved to {p}"), cancellable=False, text="Creating PDF…")

    def _export_csv(self) -> None:
        if not self.report:
            return
        default = str(self.ctx.paths.exports_dir / (safe_filename(self.report.title) + ".csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV (*.csv)")
        if not path:
            return
        try:
            export_report_csv(self.report, Path(path))
        except OSError as exc:
            show_error(self, "Export", f"Could not write the file: {exc}")
            return
        show_info(self, "Export", f"Saved to {path}")

    def _followup(self) -> None:
        if not self.state or not self.summary:
            return
        weak = [b.key for b in self.summary.by_topic if b.percentage < 70] or [b.key for b in self.summary.by_topic]
        base: ExamConfig = self.state.config
        cfg = base.model_copy(deep=True)
        cfg.title = f"Follow-up: {base.title}"[:200]
        cfg.mode = ExamMode.WEAKNESS
        cfg.topics = weak
        cfg.attempts_allowed = 0
        self.followup_requested.emit(cfg)

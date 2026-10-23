"""Question bank management: search, edit, collections, import/export, regeneration."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..ai.question_generator import GenerationRequest
from ..app.application import AppContext
from ..documents.question_bank_io import export_csv, export_json, import_json
from ..exams.question_models import QUESTION_TYPE_LABELS, Difficulty, Provenance, Question, QuestionType, ReviewStatus
from .question_editor import QuestionEditor
from .widgets import ai_preflight, button, confirm, label, run_with_progress, show_error, show_info


class HistoryDialog(QDialog):
    def __init__(self, parent: QWidget, ctx: AppContext, q: Question) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Version history – v{q.version}")
        self.resize(720, 480)
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.view = QTextBrowser()
        lay.addWidget(self.list, 1)
        lay.addWidget(self.view, 2)
        self.versions = [(q.version, "current", q)] + ctx.questions.versions(q.id)
        for v, at, _ in self.versions:
            self.list.addItem(f"v{v} – {at[:19]}")
        self.list.currentRowChanged.connect(self._show)
        self.list.setCurrentRow(0)

    def _show(self, row: int) -> None:
        if row < 0:
            return
        q = self.versions[row][2]
        if q is None:
            self.view.setPlainText("(unreadable version)")
            return
        self.view.setPlainText(f"{q.text}\n\nAnswer: {q.correct_answer_display()}\n\nExplanation: {q.explanation}\n\n"
                               f"Topic: {q.topic} · {q.difficulty.value} · {q.max_points:g} pts · {q.review_status.value}")


class QuestionBankView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.page = 0
        self.total = 0
        self.rows: list[Question] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.addWidget(label("Question bank", "h1"))

        f = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search question text, topic, tags…")
        self.subject = QComboBox()
        self.qtype = QComboBox()
        self.qtype.addItem("All types", "")
        for t in QuestionType:
            self.qtype.addItem(QUESTION_TYPE_LABELS[t], t.value)
        self.diff = QComboBox()
        self.diff.addItem("All difficulties", "")
        for d in Difficulty:
            self.diff.addItem(d.value, d.value)
        self.prov = QComboBox()
        self.prov.addItem("Any origin", "")
        for p in Provenance:
            self.prov.addItem(p.value.replace("_", " "), p.value)
        self.review = QComboBox()
        self.review.addItem("Any review status", "")
        for r in ReviewStatus:
            self.review.addItem(r.value.replace("_", " "), r.value)
        self.collection = QComboBox()
        f.addWidget(self.search, 2)
        for w in (self.subject, self.qtype, self.diff, self.prov, self.review, self.collection):
            f.addWidget(w)
            w.currentIndexChanged.connect(self._reset_page)
        self.search.textChanged.connect(self._reset_page)
        root.addLayout(f)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Subject", "Topic", "Type", "Difficulty", "Origin", "Review", "Question"])
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(lambda _: self._edit())
        root.addWidget(self.table, 1)

        pg = QHBoxLayout()
        self.count_lbl = label("", "muted")
        pg.addWidget(self.count_lbl, 1)
        self.prev_btn = button("◀", slot=lambda: self._page(-1))
        self.next_btn = button("▶", slot=lambda: self._page(1))
        pg.addWidget(self.prev_btn)
        pg.addWidget(self.next_btn)
        root.addLayout(pg)

        a = QHBoxLayout()
        a.addWidget(button("New question…", "primary", slot=self._new))
        a.addWidget(button("Edit…", slot=self._edit))
        a.addWidget(button("History…", slot=self._history))
        a.addWidget(button("Regenerate (AI)", slot=self._regenerate))
        a.addWidget(button("Verify (AI)", slot=self._verify, tooltip="Independent second-pass check of answer keys"))
        a.addWidget(button("Delete", "danger", slot=self._delete))
        a.addStretch()
        a.addWidget(button("Add to collection…", slot=self._add_to_collection))
        a.addWidget(button("New collection…", slot=self._new_collection))
        a.addWidget(button("Import JSON…", slot=self._import))
        a.addWidget(button("Export…", slot=self._export))
        root.addLayout(a)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh_filters()
        self.refresh()
        super().showEvent(e)

    def refresh_filters(self) -> None:
        for combo, items, first in ((self.subject, [(s, s) for s in self.ctx.questions.subjects()], "All subjects"),
                                    (self.collection, [(f"{c['name']} ({c['n']})", c["id"]) for c in self.ctx.questions.list_collections()], "All questions")):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first, "")
            for text, data in items:
                combo.addItem(text, data)
            idx = combo.findData(cur)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)

    def _reset_page(self, *_: object) -> None:
        self.page = 0
        self.refresh()

    def _page(self, d: int) -> None:
        self.page = max(0, self.page + d)
        self.refresh()

    def refresh(self) -> None:
        size = self.ctx.settings.settings.app.page_size
        self.rows, self.total = self.ctx.questions.search(
            text=self.search.text().strip(), subject=self.subject.currentData() or "", qtype=self.qtype.currentData() or "",
            difficulty=self.diff.currentData() or "", provenance=self.prov.currentData() or "",
            review_status=self.review.currentData() or "", collection_id=self.collection.currentData() or "",
            limit=size, offset=self.page * size,
        )
        self.table.setRowCount(len(self.rows))
        for i, q in enumerate(self.rows):
            for j, v in enumerate([q.subject, q.topic, QUESTION_TYPE_LABELS[q.type], q.difficulty.value,
                                   q.provenance.value.replace("_", " "), q.review_status.value.replace("_", " "), q.text[:220]]):
                it = QTableWidgetItem(v)
                if j == 6 and q.validation_notes:
                    it.setToolTip("\n".join(q.validation_notes))
                self.table.setItem(i, j, it)
        pages = max(1, -(-self.total // size))
        self.count_lbl.setText(f"{self.total} question(s) · page {self.page + 1} of {pages}" if self.total else
                               "No questions match. Create one, import a bank, or generate questions in the exam wizard.")
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(self.page + 1 < pages)

    def _selected(self) -> list[Question]:
        return [self.rows[r.row()] for r in self.table.selectionModel().selectedRows()]

    def _new(self) -> None:
        dlg = QuestionEditor(self, None, self.subject.currentData() or "")
        if dlg.exec() and dlg.result:
            self.ctx.questions.save(dlg.result)
            self.refresh_filters()
            self.refresh()

    def _edit(self) -> None:
        sel = self._selected()
        if not sel:
            return
        dlg = QuestionEditor(self, sel[0])
        if dlg.exec() and dlg.result:
            self.ctx.questions.save(dlg.result, record_history=True)
            self.refresh()

    def _history(self) -> None:
        sel = self._selected()
        if sel:
            HistoryDialog(self, self.ctx, sel[0]).exec()

    def _delete(self) -> None:
        sel = self._selected()
        if not sel or not confirm(self, "Delete", f"Remove {len(sel)} question(s) from the bank?\n"
                                                  "Completed exams keep their own copies, so results are not affected."):
            return
        for q in sel:
            self.ctx.questions.delete(q.id)
        self.refresh()

    def _regenerate(self) -> None:
        sel = self._selected()
        if not sel:
            return
        old = sel[0]
        req = GenerationRequest(subject=old.subject, topics=[old.topic], difficulty=old.difficulty, count=1, types=[old.type],
                                avoid_texts=[old.text], focus_notes="Replace the existing question with a better one on the same skill.")
        consent = ai_preflight(self, self.ctx.generator.plan(req), "subject, topic and the current question text")
        if consent is None:
            return

        def done(res) -> None:
            if not res.questions:
                show_error(self, "Regenerate", res.error or "No valid question was generated.")
                return
            new = res.questions[0].model_copy(update={"id": old.id, "tags": old.tags})
            self.ctx.questions.save(new, record_history=True)
            self.refresh()
            show_info(self, "Regenerate", "Question replaced. The previous version is kept in its history.")

        run_with_progress(self, "Regenerate", lambda cancel, progress: self.ctx.generator.generate(
            req, cloud_consent=consent, cancel=cancel, progress=progress), on_done=done, text="Generating…")

    def _verify(self) -> None:
        sel = self._selected()
        if not sel:
            return
        from ..config.settings import AIOperation

        consent = ai_preflight(self, self.ctx.router.plan(AIOperation.VERIFY_QUESTIONS), f"the text and options of {len(sel)} question(s) (without answer keys)")
        if consent is None:
            return
        qs = [q.model_copy(deep=True) for q in sel]
        for q in qs:
            if q.review_status == ReviewStatus.FLAGGED:
                q.review_status = ReviewStatus.UNVERIFIED

        def work(cancel, progress):
            self.ctx.generator.verify(qs, cloud_consent=consent, cancel=cancel, progress=progress)
            return qs

        def done(out: list[Question]) -> None:
            for q in out:
                self.ctx.questions.save(q, record_history=False)
            self.refresh()
            summary = {s: sum(1 for q in out if q.review_status == s) for s in ReviewStatus}
            show_info(self, "Verification", "\n".join(f"{k.value}: {v}" for k, v in summary.items() if v)
                      + "\n\nHover the question text to see verification notes.")

        run_with_progress(self, "Verify questions", work, on_done=done, text="Checking questions…")

    def _new_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "New collection", "Collection name:")
        if ok and name.strip():
            try:
                cid = self.ctx.questions.create_collection(name.strip())
            except Exception as exc:
                show_error(self, "Collection", f"Could not create collection: {exc}")
                return
            sel = self._selected()
            if sel:
                self.ctx.questions.add_to_collection(cid, [q.id for q in sel])
            self.refresh_filters()

    def _add_to_collection(self) -> None:
        sel = self._selected()
        cols = self.ctx.questions.list_collections()
        if not sel or not cols:
            show_info(self, "Collections", "Select questions and create a collection first.")
            return
        names = [c["name"] for c in cols]
        name, ok = QInputDialog.getItem(self, "Add to collection", "Collection:", names, 0, False)
        if ok:
            cid = cols[names.index(name)]["id"]
            self.ctx.questions.add_to_collection(cid, [q.id for q in sel])
            self.refresh_filters()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import question bank", "", "Question bank (*.json)")
        if not path:
            return
        res = import_json(path)
        if res.questions:
            self.ctx.questions.save_many(res.questions)
        msg = f"Imported {len(res.questions)} question(s)."
        if res.errors:
            msg += f"\n\n{len(res.errors)} entr(y/ies) rejected:\n" + "\n".join(res.errors[:10])
        if res.warnings:
            msg += f"\n\n{len(res.warnings)} warning(s):\n" + "\n".join(res.warnings[:5])
        show_info(self, "Import", msg)
        self.refresh_filters()
        self.refresh()

    def _export(self) -> None:
        sel = self._selected()
        if sel:
            qs = sel
        else:
            qs, _ = self.ctx.questions.search(subject=self.subject.currentData() or "", collection_id=self.collection.currentData() or "", limit=100000)
        if not qs:
            return
        path, flt = QFileDialog.getSaveFileName(self, f"Export {len(qs)} question(s)", str(self.ctx.paths.exports_dir / "questions.json"),
                                                "Question bank JSON (*.json);;CSV (*.csv)")
        if not path:
            return
        try:
            (export_csv if path.lower().endswith(".csv") else export_json)(qs, Path(path))
        except OSError as exc:
            show_error(self, "Export", str(exc))
            return
        show_info(self, "Export", f"Exported {len(qs)} question(s) to {path}")

"""Study materials: import, preview, section selection and source-based exams."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.json_utils import as_object, extract_json
from ..ai import prompt_templates as pt
from ..app.application import AppContext
from ..config.defaults import MAX_SOURCE_CHARS_PER_REQUEST
from ..config.settings import AIOperation
from ..documents.source_retrieval import keyword_topics
from ..documents.text_extraction import ocr_available
from ..exams.question_models import ExamConfig, QuestionType
from .widgets import Banner, ai_preflight, button, card, confirm, label, run_with_progress, show_error, show_info


class PasteDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paste study text")
        self.resize(700, 520)
        lay = QVBoxLayout(self)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Title")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Paste your notes here. Markdown headings (#, ##) become sections.")
        lay.addWidget(self.title)
        lay.addWidget(self.text, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class MaterialsView(QWidget):
    create_exam = Signal(object)  # ExamConfig

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.docs: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.addWidget(label("Study materials", "h1"))
        root.addWidget(label("Files are processed locally and stay on this computer. Only selected, relevant excerpts are "
                             "ever sent to an AI model - to a cloud model only with your consent and if allowed in AI Settings.", "muted"))
        row = QHBoxLayout()
        row.addWidget(button("Import files…", "primary", slot=self._import))
        row.addWidget(button("Paste text…", slot=self._paste))
        self.ocr = QCheckBox("Use OCR for scanned PDF pages")
        avail = ocr_available()
        self.ocr.setEnabled(avail)
        self.ocr.setToolTip("Available" if avail else "Install Tesseract OCR and the 'pytesseract' package to enable OCR.")
        row.addWidget(self.ocr)
        row.addStretch()
        root.addLayout(row)
        self.banner = Banner()
        root.addWidget(self.banner)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Name", "Format", "Status", "Characters", "Pages", "Subject", "Source file"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_doc)
        split.addWidget(self.table)

        detail = QWidget()
        dl = QHBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        left, ll = card()
        ll.addWidget(label("Sections (tick to include)", "h2"))
        self.sections = QListWidget()
        self.sections.itemChanged.connect(self._section_toggled)
        self.sections.currentItemChanged.connect(self._preview_section)
        ll.addWidget(self.sections, 1)
        left.setMaximumWidth(380)
        dl.addWidget(left)
        right, rl = card()
        self.warn = Banner(warn=True)
        rl.addWidget(self.warn)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        rl.addWidget(self.preview, 1)
        self.topics_lbl = label("", "muted")
        rl.addWidget(self.topics_lbl)
        br = QHBoxLayout()
        br.addWidget(button("Set subject…", slot=self._set_subject))
        br.addWidget(button("Detect topics", slot=self._detect_topics))
        br.addWidget(button("Delete", "danger", slot=self._delete))
        br.addStretch()
        br.addWidget(button("Create exam from this material", "primary", slot=self._create_exam))
        rl.addLayout(br)
        dl.addWidget(right, 1)
        split.addWidget(detail)
        split.setSizes([200, 480])
        root.addWidget(split, 1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        self.docs = self.ctx.documents.list()
        self.table.setRowCount(len(self.docs))
        for i, d in enumerate(self.docs):
            state = self.ctx.importer.source_file_state(d)
            state_txt = {"ok": "present", "missing": "missing (text kept)", "changed": "changed since import", "n/a": "pasted"}[state]
            vals = [d["name"], d["format"], d["status"], f"{d['char_count']:,}", str(d["page_count"] or ""), d["subject"], state_txt]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(v))
        if not self.docs:
            self.preview.setPlainText("No study materials yet. Import PDF, DOCX, TXT or Markdown files, or paste text.")
            self.sections.clear()

    def _current(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        return self.docs[rows[0].row()] if rows else None

    def _show_doc(self) -> None:
        d = self._current()
        self.sections.blockSignals(True)
        self.sections.clear()
        if not d:
            self.sections.blockSignals(False)
            return
        for s in self.ctx.documents.sections(d["id"]):
            pages = f" (p. {s['page_start']}-{s['page_end']})" if s["page_start"] else ""
            it = QListWidgetItem(f"{s['title']}{pages} · {len(s['text']):,} chars")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if s["included"] else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, (s["id"], s["text"]))
            self.sections.addItem(it)
        self.sections.blockSignals(False)
        state = self.ctx.importer.source_file_state(d)
        warns = list(d["warnings"])
        if state == "missing":
            warns.append("The original file is no longer at its imported location. The extracted text is still available.")
        elif state == "changed":
            warns.append("The original file has changed since import. Re-import it to use the new content.")
        self.warn.show_text("<br>".join(html.escape(w) for w in warns))
        if self.sections.count():
            self.sections.setCurrentRow(0)
        self.topics_lbl.setText("")

    def _preview_section(self, it: QListWidgetItem | None, _prev=None) -> None:
        if it:
            self.preview.setPlainText(it.data(Qt.ItemDataRole.UserRole)[1])

    def _section_toggled(self, it: QListWidgetItem) -> None:
        sid, _ = it.data(Qt.ItemDataRole.UserRole)
        self.ctx.documents.set_section_included(sid, it.checkState() == Qt.CheckState.Checked)

    # ------------------------------------------------------------------ actions
    def _import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Import study materials", "", "Documents (*.pdf *.docx *.txt *.md *.markdown)")
        if not paths:
            return
        use_ocr = self.ocr.isChecked()

        def work(cancel, progress):
            out = []
            for i, p in enumerate(paths):
                cancel.raise_if_cancelled()
                progress(f"Processing {p.split('/')[-1]}…", i, len(paths))
                out.append(self.ctx.importer.import_file(p, use_ocr=use_ocr))
            return out

        def done(outcomes) -> None:
            lines = []
            for o in outcomes:
                icon = {"ok": "✅", "partial": "⚠", "needs_ocr": "⚠", "duplicate": "ℹ", "error": "❌"}[o.status]
                lines.append(f"{icon} <b>{html.escape(o.name)}</b>: {html.escape(o.message)}")
            self.banner.show_text("<br>".join(lines), warn=any(o.status in ("error", "needs_ocr", "partial") for o in outcomes))
            self.refresh()

        run_with_progress(self, "Import", work, on_done=done, text="Extracting text…")

    def _paste(self) -> None:
        dlg = PasteDialog(self)
        if dlg.exec():
            o = self.ctx.importer.import_text(dlg.text.toPlainText(), dlg.title.text().strip() or "Pasted text")
            self.banner.show_text(html.escape(f"{o.name}: {o.message}"), warn=o.status == "error")
            self.refresh()

    def _set_subject(self) -> None:
        d = self._current()
        if not d:
            return
        s, ok = QInputDialog.getText(self, "Subject", "Subject for this material:", text=d["subject"])
        if ok:
            self.ctx.documents.set_subject(d["id"], s.strip())
            self.refresh()

    def _delete(self) -> None:
        d = self._current()
        if d and confirm(self, "Delete", f"Remove '{d['name']}' from the library? (The original file is not touched.)"):
            self.ctx.documents.delete(d["id"])
            self.refresh()

    def _detect_topics(self) -> None:
        d = self._current()
        if not d:
            return
        secs = self.ctx.documents.sections(d["id"], included_only=True)
        text = "\n\n".join(s["text"] for s in secs)
        local = keyword_topics(text)
        self.topics_lbl.setText("Frequent terms (local analysis): " + ", ".join(local))
        decision = self.ctx.router.plan(AIOperation.ANALYZE_TOPICS, includes_source_material=True)
        if not decision.possible:
            return
        consent = ai_preflight(self, decision, f"the first {MAX_SOURCE_CHARS_PER_REQUEST:,} characters of the selected sections")
        if consent is None:
            return
        excerpt = text[:MAX_SOURCE_CHARS_PER_REQUEST]

        def work(cancel, progress):
            resp = self.ctx.router.execute(AIOperation.ANALYZE_TOPICS,
                                           [{"role": "system", "content": pt.TUTOR_SYSTEM},
                                            {"role": "user", "content": pt.topic_analysis_prompt(excerpt)}],
                                           decision=decision, cloud_consent=consent, json_mode=True, cancel=cancel)
            return as_object(extract_json(resp.text)), resp.model

        def done(out) -> None:
            data, model = out
            topics = [str(t) for t in (data.get("topics") or [])][:12]
            objectives = [str(t) for t in (data.get("objectives") or [])][:6]
            self.topics_lbl.setText(
                f"Topics (AI, {model}): {', '.join(topics) or '—'}\nLearning objectives: {'; '.join(objectives) or '—'}\n"
                f"Frequent terms (local): {', '.join(local)}")
            subj = str(data.get("subject") or "").strip()
            if subj and not d["subject"]:
                self.ctx.documents.set_subject(d["id"], subj[:80])
                self.refresh()
            self._detected_topics = topics

        run_with_progress(self, "Detect topics", work, on_done=done, text="Analysing the material…")

    def _create_exam(self) -> None:
        d = self._current()
        if not d:
            show_info(self, "Create exam", "Select a document first.")
            return
        topics = getattr(self, "_detected_topics", [])[:6]
        cfg = ExamConfig(
            title=f"Exam: {d['name']}"[:200], subject=d["subject"] or "General", topics=topics, num_questions=10,
            question_types=[QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE, QuestionType.SHORT_ANSWER],
            source_document_ids=[d["id"]],
        )
        self.create_exam.emit(cfg)

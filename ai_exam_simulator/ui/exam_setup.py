"""Exam creation wizard: template → subject/topics → settings → question set → start."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.question_generator import GenerationRequest, GenerationResult
from ..app.application import AppContext
from ..config.defaults import LANGUAGES, SUBJECTS
from ..exams.exam_builder import BuildPlan
from ..exams.question_models import (
    EXAM_MODE_LABELS,
    QUESTION_TYPE_LABELS,
    Difficulty,
    ExamConfig,
    ExamMode,
    FeedbackMode,
    MultiSelectScoring,
    PermittedTools,
    Question,
    QuestionType,
    ReviewStatus,
    ScoringRules,
)
from ..exams.validation import validate_exam_questions
from .question_editor import QuestionEditor
from .widgets import Banner, ai_preflight, button, card, confirm, label, run_with_progress, show_error, show_info

STEPS = ["1. Template & mode", "2. Subject & topics", "3. Exam settings", "4. Questions"]


class BankPicker(QDialog):
    def __init__(self, parent: QWidget, ctx: AppContext, subject: str, exclude: set[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add questions from the bank")
        self.resize(760, 520)
        self.ctx = ctx
        self.exclude = exclude
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search text, topic or tag…")
        self.subject = QComboBox()
        self.subject.addItem("All subjects", "")
        for s in ctx.questions.subjects():
            self.subject.addItem(s, s)
        idx = self.subject.findData(subject)
        if idx >= 0:
            self.subject.setCurrentIndex(idx)
        row.addWidget(self.search, 1)
        row.addWidget(self.subject)
        lay.addLayout(row)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.search.textChanged.connect(self._fill)
        self.subject.currentIndexChanged.connect(self._fill)
        self._fill()

    def _fill(self) -> None:
        qs, _ = self.ctx.questions.search(text=self.search.text().strip(), subject=self.subject.currentData(), limit=300)
        self.list.clear()
        for q in qs:
            if q.id in self.exclude:
                continue
            it = QListWidgetItem(f"[{q.topic} · {QUESTION_TYPE_LABELS[q.type]} · {q.difficulty.value}] {q.text[:110]}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, q.id)
            self.list.addItem(it)

    def selected_ids(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class ExamSetupView(QWidget):
    start_requested = Signal(object, list)  # ExamConfig, list[Question]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.questions: list[Question] = []
        self.build_plan: BuildPlan | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.addWidget(label("Create exam", "h1"))
        self.step_label = label("", "muted")
        root.addWidget(self.step_label)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.back_btn = button("◀ Back", slot=lambda: self._go(self.stack.currentIndex() - 1))
        self.next_btn = button("Next ▶", "primary", slot=lambda: self._go(self.stack.currentIndex() + 1))
        self.save_tpl_btn = button("Save as template…", slot=self._save_template)
        self.start_btn = button("Start exam", "primary", slot=self._start)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.save_tpl_btn)
        nav.addStretch()
        nav.addWidget(self.next_btn)
        nav.addWidget(self.start_btn)
        root.addLayout(nav)

        self.stack.addWidget(self._page_template())
        self.stack.addWidget(self._page_subject())
        self.stack.addWidget(self._scroll(self._page_settings()))
        self.stack.addWidget(self._page_questions())
        self.reset()

    @staticmethod
    def _scroll(w: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(w)
        return sa

    # ------------------------------------------------------------------ page 1
    def _page_template(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        c1, l1 = card()
        l1.addWidget(label("Start from a template", "h2"))
        self.tpl_list = QListWidget()
        self.tpl_list.currentItemChanged.connect(self._template_chosen)
        l1.addWidget(self.tpl_list, 1)
        self.del_tpl_btn = button("Delete template", "danger", slot=self._delete_template)
        l1.addWidget(self.del_tpl_btn)
        lay.addWidget(c1, 1)
        c2, l2 = card()
        l2.addWidget(label("Exam mode", "h2"))
        self.mode_group = QButtonGroup(self)
        descriptions = {
            ExamMode.FIXED: "Uses exactly the topics, difficulty and settings you choose.",
            ExamMode.ADAPTIVE: "Adjusts difficulty and topic mix from your history, within the limits you set.",
            ExamMode.WEAKNESS: "Focuses on topics and formats where you have made repeated mistakes.",
            ExamMode.REVISION: "Re-tests questions you previously answered incorrectly.",
            ExamMode.MIXED_MOCK: "Combines previously studied topics, mixed formats and difficulties.",
            ExamMode.PRACTICE: "Learning mode: immediate feedback, hints, retries and similar questions. Not counted in statistics.",
        }
        self.mode_buttons: dict[ExamMode, QRadioButton] = {}
        for m in ExamMode:
            rb = QRadioButton(EXAM_MODE_LABELS[m])
            self.mode_group.addButton(rb)
            self.mode_buttons[m] = rb
            l2.addWidget(rb)
            l2.addWidget(label(descriptions[m], "muted"))
        self.mode_buttons[ExamMode.FIXED].setChecked(True)
        l2.addStretch()
        lay.addWidget(c2, 1)
        return w

    def _fill_templates(self) -> None:
        self.tpl_list.blockSignals(True)
        self.tpl_list.clear()
        for tid, name, cfg, builtin in self.ctx.templates.list():
            it = QListWidgetItem(("★ " if builtin else "") + name)
            it.setData(Qt.ItemDataRole.UserRole, (tid, cfg, builtin))
            it.setToolTip(f"{cfg.num_questions} questions · {cfg.duration_minutes or 'untimed'} min · {cfg.difficulty.value} · "
                          + ", ".join(QUESTION_TYPE_LABELS[t] for t in cfg.question_types))
            self.tpl_list.addItem(it)
        self.tpl_list.blockSignals(False)

    def _template_chosen(self, it: QListWidgetItem | None, _prev=None) -> None:
        if not it:
            return
        _, cfg, builtin = it.data(Qt.ItemDataRole.UserRole)
        subject = self.subject.currentText().strip()
        topics = self._selected_topics()
        self.apply_config(cfg)
        if subject and builtin:  # presets don't override the chosen subject
            self.subject.setCurrentText(subject)
            self._set_topics(topics)
        self.del_tpl_btn.setEnabled(not builtin)

    def _delete_template(self) -> None:
        it = self.tpl_list.currentItem()
        if not it:
            return
        tid, _, builtin = it.data(Qt.ItemDataRole.UserRole)
        if not builtin and confirm(self, "Delete template", f"Delete template '{it.text()}'?"):
            self.ctx.templates.delete(tid)
            self._fill_templates()

    # ------------------------------------------------------------------ page 2
    def _page_subject(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        c1, l1 = card()
        l1.addWidget(label("Subject", "h2"))
        self.subject = QComboBox()
        self.subject.setEditable(True)
        self.subject.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.subject.currentTextChanged.connect(self._subject_changed)
        l1.addWidget(self.subject)
        l1.addWidget(label("Type any subject - the list is only a suggestion.", "muted"))
        l1.addWidget(label("Topics", "h2"))
        self.topic_list = QListWidget()
        l1.addWidget(self.topic_list, 1)
        row = QHBoxLayout()
        self.new_topic = QLineEdit()
        self.new_topic.setPlaceholderText("Add a custom topic…")
        self.new_topic.returnPressed.connect(self._add_topic)
        row.addWidget(self.new_topic, 1)
        row.addWidget(button("Add", slot=self._add_topic))
        l1.addLayout(row)
        l1.addWidget(label("No topics ticked = all topics of the subject.", "muted"))
        lay.addWidget(c1, 1)
        c2, l2 = card()
        l2.addWidget(label("Study materials (optional)", "h2"))
        l2.addWidget(label("Tick imported documents to generate questions from them. Only the most relevant excerpts "
                           "are sent to the AI model, and cloud use requires your consent.", "muted"))
        self.doc_list = QListWidget()
        l2.addWidget(self.doc_list, 1)
        lay.addWidget(c2, 1)
        return w

    def _fill_subjects(self) -> None:
        current = self.subject.currentText()
        subjects = sorted(set(SUBJECTS) | set(self.ctx.questions.subjects()))
        self.subject.blockSignals(True)
        self.subject.clear()
        self.subject.addItems(subjects)
        self.subject.setCurrentText(current or "Mathematics")
        self.subject.blockSignals(False)
        self._subject_changed(self.subject.currentText())

    def _subject_changed(self, subject: str) -> None:
        checked = set(self._selected_topics())
        topics = list(dict.fromkeys(SUBJECTS.get(subject, []) + self.ctx.questions.topics(subject)))
        self.topic_list.clear()
        for t in topics:
            self._topic_item(t, t in checked)

    def _topic_item(self, t: str, checked: bool) -> None:
        it = QListWidgetItem(t)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.topic_list.addItem(it)

    def _add_topic(self) -> None:
        t = self.new_topic.text().strip()
        if t:
            existing = [self.topic_list.item(i).text() for i in range(self.topic_list.count())]
            if t not in existing:
                self._topic_item(t, True)
            self.new_topic.clear()

    def _selected_topics(self) -> list[str]:
        return [self.topic_list.item(i).text() for i in range(self.topic_list.count())
                if self.topic_list.item(i).checkState() == Qt.CheckState.Checked]

    def _set_topics(self, topics: list[str]) -> None:
        existing = {self.topic_list.item(i).text(): self.topic_list.item(i) for i in range(self.topic_list.count())}
        for it in existing.values():
            it.setCheckState(Qt.CheckState.Unchecked)
        for t in topics:
            if t in existing:
                existing[t].setCheckState(Qt.CheckState.Checked)
            else:
                self._topic_item(t, True)

    def _fill_docs(self, selected: list[str] | None = None) -> None:
        sel = set(selected or [])
        self.doc_list.clear()
        for d in self.ctx.documents.list():
            it = QListWidgetItem(f"{d['name']} ({d['char_count']:,} chars{', ' + d['subject'] if d['subject'] else ''})")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if d["id"] in sel else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, d["id"])
            self.doc_list.addItem(it)
        if not self.doc_list.count():
            it = QListWidgetItem("No study materials imported yet (see 'Study Materials').")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.doc_list.addItem(it)

    def _selected_docs(self) -> list[str]:
        return [self.doc_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.doc_list.count())
                if self.doc_list.item(i).checkState() == Qt.CheckState.Checked and self.doc_list.item(i).data(Qt.ItemDataRole.UserRole)]

    # ------------------------------------------------------------------ page 3
    def _page_settings(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        g1 = QGroupBox("General")
        f1 = QFormLayout(g1)
        self.title = QLineEdit()
        self.num = QSpinBox()
        self.num.setRange(1, 200)
        self.duration = QSpinBox()
        self.duration.setRange(0, 600)
        self.duration.setSuffix(" min")
        self.duration.setSpecialValueText("untimed")
        self.difficulty = QComboBox()
        self.min_diff = QComboBox()
        self.max_diff = QComboBox()
        for d in Difficulty:
            self.difficulty.addItem(d.value, d)
            self.min_diff.addItem(d.value, d)
            self.max_diff.addItem(d.value, d)
        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.addItems(LANGUAGES)
        self.attempts = QSpinBox()
        self.attempts.setRange(0, 100)
        self.attempts.setSpecialValueText("unlimited")
        f1.addRow("Exam title:", self.title)
        f1.addRow("Number of questions:", self.num)
        f1.addRow("Duration:", self.duration)
        f1.addRow("Difficulty:", self.difficulty)
        dr = QHBoxLayout()
        dr.addWidget(self.min_diff)
        dr.addWidget(label("to", wrap=False))
        dr.addWidget(self.max_diff)
        f1.addRow("Adaptive range:", dr)
        f1.addRow("Language:", self.language)
        f1.addRow("Attempts allowed:", self.attempts)
        grid.addWidget(g1, 0, 0)

        g2 = QGroupBox("Scoring")
        f2 = QFormLayout(g2)
        self.pass_threshold = QDoubleSpinBox()
        self.pass_threshold.setRange(0, 100)
        self.pass_threshold.setSuffix(" %")
        self.partial = QCheckBox("Allow partial credit")
        self.negative = QCheckBox("Negative marking for wrong objective answers")
        self.neg_fraction = QDoubleSpinBox()
        self.neg_fraction.setRange(0, 1)
        self.neg_fraction.setSingleStep(0.05)
        self.neg_fraction.setPrefix("− ")
        self.neg_fraction.setSuffix(" × question points")
        self.multi_mode = QComboBox()
        self.multi_mode.addItem("Partial (right − wrong selections)", MultiSelectScoring.PARTIAL)
        self.multi_mode.addItem("All or nothing", MultiSelectScoring.ALL_OR_NOTHING)
        f2.addRow("Pass threshold:", self.pass_threshold)
        f2.addRow(self.partial)
        f2.addRow(self.negative)
        f2.addRow("Penalty:", self.neg_fraction)
        f2.addRow("Multiple select:", self.multi_mode)
        grid.addWidget(g2, 0, 1)

        g3 = QGroupBox("Question types")
        l3 = QGridLayout(g3)
        self.type_boxes: dict[QuestionType, QCheckBox] = {}
        for i, t in enumerate(QuestionType):
            cb = QCheckBox(QUESTION_TYPE_LABELS[t])
            self.type_boxes[t] = cb
            l3.addWidget(cb, i // 3, i % 3)
        grid.addWidget(g3, 1, 0)

        g4 = QGroupBox("Exam rules")
        f4 = QFormLayout(g4)
        self.shuffle_q = QCheckBox("Randomise question order")
        self.shuffle_o = QCheckBox("Randomise answer options")
        self.revisit = QCheckBox("Allow going back to earlier questions")
        self.unanswered = QCheckBox("Allow submitting with unanswered questions")
        self.feedback = QComboBox()
        self.feedback.addItem("Only after submission (realistic exam)", FeedbackMode.AFTER_SUBMISSION)
        self.feedback.addItem("Immediately after each question", FeedbackMode.IMMEDIATE)
        self.t_calc = QCheckBox("Calculator")
        self.t_notes = QCheckBox("Notes / scratch pad")
        self.t_formula = QCheckBox("Formula sheet")
        for cb in (self.shuffle_q, self.shuffle_o, self.revisit, self.unanswered):
            f4.addRow(cb)
        f4.addRow("Feedback:", self.feedback)
        tr = QHBoxLayout()
        for cb in (self.t_calc, self.t_notes, self.t_formula):
            tr.addWidget(cb)
        f4.addRow("Permitted tools:", tr)
        self.formula_text = QPlainTextEdit()
        self.formula_text.setPlaceholderText("Formula sheet text (shown when the formula sheet is permitted)")
        self.formula_text.setMaximumHeight(80)
        f4.addRow(self.formula_text)
        grid.addWidget(g4, 1, 1)
        return w

    # ------------------------------------------------------------------ page 4
    def _page_questions(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top, tl = card()
        tl.addWidget(label("Question source", "h2"))
        self.src_group = QButtonGroup(self)
        self.src_bank_ai = QRadioButton("Question bank first, generate the rest with AI (recommended)")
        self.src_ai = QRadioButton("Generate all questions with AI")
        self.src_bank = QRadioButton("Question bank only (no AI)")
        self.src_manual = QRadioButton("Empty - I will add questions myself")
        for rb in (self.src_bank_ai, self.src_ai, self.src_bank, self.src_manual):
            self.src_group.addButton(rb)
            tl.addWidget(rb)
        self.src_bank_ai.setChecked(True)
        br = QHBoxLayout()
        br.addWidget(button("Build question set", "primary", slot=self._build))
        self.ai_status = label("", "muted")
        br.addWidget(self.ai_status, 1)
        tl.addLayout(br)
        lay.addWidget(top)
        self.plan_banner = Banner()
        lay.addWidget(self.plan_banner)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Type", "Topic", "Difficulty", "Status", "Question"])
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self._edit())
        lay.addWidget(self.table, 1)
        self.issues = label("", "muted")
        lay.addWidget(self.issues)
        row = QHBoxLayout()
        row.addWidget(button("Edit", slot=self._edit))
        row.addWidget(button("Regenerate (AI)", slot=self._regenerate))
        row.addWidget(button("Remove", "danger", slot=self._remove))
        row.addWidget(button("Add from bank…", slot=self._add_from_bank))
        row.addWidget(button("Write a question…", slot=self._add_manual))
        row.addStretch()
        lay.addLayout(row)
        return w

    # ------------------------------------------------------------------ config <-> form
    def reset(self) -> None:
        self._fill_templates()
        self._fill_subjects()
        self._fill_docs()
        self.questions = []
        self._refresh_table()
        self.plan_banner.show_text("")
        if self.tpl_list.count():
            self.tpl_list.setCurrentRow(1 if self.tpl_list.count() > 1 else 0)
        self._go(0)

    def apply_config(self, cfg: ExamConfig, *, keep_questions: bool = True) -> None:
        self.mode_buttons[cfg.mode].setChecked(True)
        self.subject.setCurrentText(cfg.subject)
        self._subject_changed(cfg.subject)
        self._set_topics(cfg.topics)
        self._fill_docs(cfg.source_document_ids)
        self.title.setText(cfg.title)
        self.num.setValue(cfg.num_questions)
        self.duration.setValue(cfg.duration_minutes)
        self.difficulty.setCurrentIndex(self.difficulty.findData(cfg.difficulty.value))
        self.min_diff.setCurrentIndex(self.min_diff.findData(cfg.min_difficulty.value))
        self.max_diff.setCurrentIndex(self.max_diff.findData(cfg.max_difficulty.value))
        self.language.setCurrentText(cfg.language)
        self.attempts.setValue(cfg.attempts_allowed)
        self.pass_threshold.setValue(cfg.pass_threshold)
        self.partial.setChecked(cfg.scoring.partial_credit)
        self.negative.setChecked(cfg.scoring.negative_marking)
        self.neg_fraction.setValue(cfg.scoring.negative_fraction)
        self.multi_mode.setCurrentIndex(self.multi_mode.findData(cfg.scoring.multi_select.value))
        for t, cb in self.type_boxes.items():
            cb.setChecked(t in cfg.question_types)
        self.shuffle_q.setChecked(cfg.shuffle_questions)
        self.shuffle_o.setChecked(cfg.shuffle_options)
        self.revisit.setChecked(cfg.allow_revisit)
        self.unanswered.setChecked(cfg.allow_unanswered)
        self.feedback.setCurrentIndex(self.feedback.findData(cfg.feedback.value))
        self.t_calc.setChecked(cfg.tools.calculator)
        self.t_notes.setChecked(cfg.tools.notes)
        self.t_formula.setChecked(cfg.tools.formula_sheet)
        self.formula_text.setPlainText(cfg.formula_sheet_text)

    def current_config(self) -> ExamConfig:
        mode = next(m for m, rb in self.mode_buttons.items() if rb.isChecked())
        types = [t for t, cb in self.type_boxes.items() if cb.isChecked()]
        lo, hi = Difficulty(self.min_diff.currentData()), Difficulty(self.max_diff.currentData())
        if lo.level > hi.level:
            lo, hi = hi, lo
        return ExamConfig(
            title=self.title.text().strip() or "Untitled exam", subject=self.subject.currentText().strip() or "General",
            topics=self._selected_topics(), num_questions=self.num.value(), difficulty=Difficulty(self.difficulty.currentData()),
            duration_minutes=self.duration.value(),
            scoring=ScoringRules(negative_marking=self.negative.isChecked(), negative_fraction=self.neg_fraction.value(),
                                 partial_credit=self.partial.isChecked(), multi_select=MultiSelectScoring(self.multi_mode.currentData())),
            pass_threshold=self.pass_threshold.value(), question_types=types, language=self.language.currentText().strip() or "English",
            attempts_allowed=self.attempts.value(), shuffle_questions=self.shuffle_q.isChecked(),
            shuffle_options=self.shuffle_o.isChecked(), allow_revisit=self.revisit.isChecked(),
            allow_unanswered=self.unanswered.isChecked(),
            tools=PermittedTools(calculator=self.t_calc.isChecked(), notes=self.t_notes.isChecked(), formula_sheet=self.t_formula.isChecked()),
            formula_sheet_text=self.formula_text.toPlainText(), feedback=FeedbackMode(self.feedback.currentData()), mode=mode,
            min_difficulty=lo, max_difficulty=hi, source_document_ids=self._selected_docs(),
        )

    def prefill(self, cfg: ExamConfig) -> None:
        self.reset()
        self.apply_config(cfg)
        self._go(1)

    # ------------------------------------------------------------------ navigation
    def _go(self, idx: int) -> None:
        idx = max(0, min(len(STEPS) - 1, idx))
        if idx > self.stack.currentIndex() and self.stack.currentIndex() == 2:
            try:
                self.current_config()
            except Exception as exc:
                show_error(self, "Invalid settings", _err(exc))
                return
        self.stack.setCurrentIndex(idx)
        self.step_label.setText("   ›   ".join(f"<b>{s}</b>" if i == idx else s for i, s in enumerate(STEPS)))
        self.step_label.setTextFormat(Qt.TextFormat.RichText)
        self.back_btn.setEnabled(idx > 0)
        self.next_btn.setVisible(idx < len(STEPS) - 1)
        self.start_btn.setVisible(idx == len(STEPS) - 1)
        if idx == 3:
            self.ai_status.setText("AI: " + self.ctx.router.status_line())

    def _save_template(self) -> None:
        try:
            cfg = self.current_config()
        except Exception as exc:
            show_error(self, "Invalid settings", _err(exc))
            return
        name, ok = QInputDialog.getText(self, "Save template", "Template name:", text=cfg.title)
        if ok and name.strip():
            self.ctx.templates.save(name.strip(), cfg)
            self._fill_templates()
            show_info(self, "Template saved", f"Template '{name.strip()}' saved.")

    # ------------------------------------------------------------------ building the question set
    def _build(self) -> None:
        try:
            cfg = self.current_config()
        except Exception as exc:
            show_error(self, "Invalid settings", _err(exc))
            return
        if self.src_manual.isChecked():
            self.questions = []
            self.plan_banner.show_text("Add questions with 'Write a question…' or 'Add from bank…'.")
            self._refresh_table()
            return
        use_bank = not self.src_ai.isChecked()
        generate = not self.src_bank.isChecked()
        bp = self.ctx.builder.build_plan(cfg, use_bank=use_bank, generate_missing=generate)
        self.build_plan = bp
        self.questions = list(bp.from_bank)
        info = [html.escape(r) for r in bp.plan.reasons + bp.notes]
        info.append(f"{len(bp.from_bank)} question(s) taken from the bank.")
        if bp.missing and not generate:
            info.append(f"<b>{bp.missing} question(s) could not be filled from the bank.</b> Choose an AI option, add questions manually, or reduce the number of questions.")
        self.plan_banner.show_text("<br>".join(info))
        self._refresh_table()
        if bp.generation_requests:
            self._generate(bp.generation_requests, cfg)

    def _generate(self, requests: list[GenerationRequest], cfg: ExamConfig) -> None:
        decision = self.ctx.generator.plan(requests[0])
        sends = "subject, topics, difficulty and texts of existing questions (to avoid duplicates)"
        if requests[0].excerpts:
            sends += f", plus {len(requests[0].excerpts)} short excerpt(s) from your study materials"
        consent = ai_preflight(self, decision, sends)
        if consent is None:
            return
        existing = list(self.questions)

        def work(cancel, progress):
            out = GenerationResult()
            pool = list(existing)
            for r in requests:
                res = self.ctx.generator.generate(r, cloud_consent=consent, cancel=cancel, progress=progress, existing=pool)
                out.questions += res.questions
                pool += res.questions
                out.rejected += res.rejected
                out.notes += [n for n in res.notes if n not in out.notes]
                out.providers_used |= res.providers_used
                if res.error:
                    out.error = res.error
                    if cancel.cancelled:
                        break
            return out

        def done(res: GenerationResult) -> None:
            self.questions += res.questions
            self._refresh_table()
            msg = [f"Generated {len(res.questions)} question(s) using {', '.join(sorted(res.providers_used)) or 'no provider'}."]
            if res.notes:
                msg += [f"⚠ {html.escape(n)}" for n in res.notes]
            if res.rejected:
                msg.append(f"{len(res.rejected)} generated item(s) were rejected by validation (e.g. {html.escape(res.rejected[0][:140])}).")
            if res.error:
                msg.append(f"<b>{html.escape(res.error)}</b>")
            flagged = sum(1 for q in res.questions if q.review_status == ReviewStatus.FLAGGED)
            if flagged:
                msg.append(f"<b>{flagged} question(s) are flagged for review</b> – check them before starting (double-click to edit).")
            self.plan_banner.show_text(self.plan_banner.text() + "<br>" + "<br>".join(msg), warn=bool(res.error or flagged))

        run_with_progress(self, "Generating questions", work, on_done=done,
                          text="Generating questions… Local models can take a minute or more per batch.")

    # ------------------------------------------------------------------ table
    def _refresh_table(self) -> None:
        reports = validate_exam_questions(self.questions)
        self.table.setRowCount(len(self.questions))
        problems = 0
        for i, q in enumerate(self.questions):
            rep = reports[q.id]
            status = q.review_status.value
            if rep.errors:
                status = "INVALID"
                problems += 1
            elif rep.warnings:
                status += " ⚠"
            vals = [str(i + 1), QUESTION_TYPE_LABELS[q.type], q.topic, q.difficulty.value, status, q.text[:200]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                tip = "\n".join(rep.errors + rep.warnings + q.validation_notes)
                if tip:
                    it.setToolTip(tip)
                self.table.setItem(i, j, it)
        cfg_n = self.num.value()
        self.issues.setText(f"{len(self.questions)} question(s) (target {cfg_n}). "
                            f"{problems} invalid. Hover a row for validation notes; double-click to edit."
                            if self.questions else "No questions yet.")

    def _selected_index(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _edit(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        dlg = QuestionEditor(self, self.questions[i])
        if dlg.exec() and dlg.result:
            self.questions[i] = dlg.result
            self._refresh_table()

    def _remove(self) -> None:
        i = self._selected_index()
        if i is not None:
            del self.questions[i]
            self._refresh_table()

    def _add_manual(self) -> None:
        topics = self._selected_topics()
        dlg = QuestionEditor(self, None, self.subject.currentText(), topics[0] if topics else "")
        if dlg.exec() and dlg.result:
            self.questions.append(dlg.result)
            self._refresh_table()

    def _add_from_bank(self) -> None:
        dlg = BankPicker(self, self.ctx, self.subject.currentText(), {q.id for q in self.questions})
        if dlg.exec():
            self.questions += self.ctx.questions.get_many(dlg.selected_ids())
            self._refresh_table()

    def _regenerate(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        old = self.questions[i]
        try:
            cfg = self.current_config()
        except Exception as exc:
            show_error(self, "Invalid settings", _err(exc))
            return
        excerpts = self.ctx.builder.excerpts_for(cfg, [old.topic]) if cfg.source_document_ids else []
        req = GenerationRequest(subject=old.subject, topics=[old.topic], difficulty=old.difficulty, count=1, types=[old.type],
                                language=cfg.language, avoid_texts=[q.text for q in self.questions], excerpts=excerpts)
        consent = ai_preflight(self, self.ctx.generator.plan(req), "subject, topic and the texts of the current questions")
        if consent is None:
            return

        def done(res: GenerationResult) -> None:
            if not res.questions:
                show_error(self, "Regenerate", res.error or "No valid replacement was generated.")
                return
            self.questions[i] = res.questions[0]
            self._refresh_table()

        run_with_progress(self, "Regenerate question",
                          lambda cancel, progress: self.ctx.generator.generate(req, cloud_consent=consent, cancel=cancel, progress=progress),
                          on_done=done, text="Generating a replacement question…")

    # ------------------------------------------------------------------ start
    def _start(self) -> None:
        try:
            cfg = self.current_config()
        except Exception as exc:
            show_error(self, "Invalid settings", _err(exc))
            return
        if not self.questions:
            show_error(self, "No questions", "Build or add at least one question first.")
            return
        reports = validate_exam_questions(self.questions)
        invalid = [i + 1 for i, q in enumerate(self.questions) if reports[q.id].errors]
        if invalid:
            show_error(self, "Invalid questions", f"Fix or remove invalid question(s): {', '.join(map(str, invalid))}.")
            return
        flagged = [i + 1 for i, q in enumerate(self.questions) if q.review_status == ReviewStatus.FLAGGED]
        if flagged and not confirm(self, "Unreviewed questions",
                                   f"Question(s) {', '.join(map(str, flagged))} are flagged as possibly problematic "
                                   "(e.g. the AI verification disagreed with the answer key). Start anyway?", yes="Start anyway"):
            return
        if len(self.questions) < cfg.num_questions and not confirm(
                self, "Fewer questions", f"The exam has {len(self.questions)} of {cfg.num_questions} planned questions. Start anyway?"):
            return
        cfg.num_questions = len(self.questions)
        for q in self.questions:  # persist generated/edited questions (provenance + edit history kept)
            self.ctx.questions.upsert_if_changed(q)
        self.start_requested.emit(cfg, list(self.questions))


def _err(exc: Exception) -> str:
    if hasattr(exc, "errors"):
        try:
            e = exc.errors()[0]  # type: ignore[attr-defined]
            return f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}"
        except Exception:
            pass
    return str(exc)

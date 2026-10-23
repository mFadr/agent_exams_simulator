"""Dialog for creating and editing questions of every supported format."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..exams.question_models import (
    BLANK_MARKER,
    CodeTestCase,
    Difficulty,
    MatchPair,
    NumericalAnswer,
    Option,
    Provenance,
    Question,
    QUESTION_TYPE_LABELS,
    QuestionType,
    ReviewStatus,
    Rubric,
    RubricCriterion,
)
from ..exams.validation import validate_question
from .widgets import button, label, show_error

QT = QuestionType


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


class RubricEditor(QWidget):
    """Rubric lines: 'points | description | concept1, concept2'."""

    def __init__(self) -> None:
        super().__init__()
        lay = QFormLayout(self)
        self.criteria = QPlainTextEdit()
        self.criteria.setPlaceholderText("One criterion per line:  points | description | expected concepts (comma separated)\n"
                                         "e.g.  2 | States the law correctly | equal, opposite")
        self.criteria.setMaximumHeight(120)
        self.misconceptions = QPlainTextEdit()
        self.misconceptions.setPlaceholderText("One common misconception per line (optional)")
        self.misconceptions.setMaximumHeight(60)
        self.alternatives = QPlainTextEdit()
        self.alternatives.setPlaceholderText("Acceptable alternative answers/approaches, one per line (optional)")
        self.alternatives.setMaximumHeight(60)
        self.partial = QLineEdit()
        self.partial.setPlaceholderText("Partial credit policy (optional)")
        lay.addRow("Rubric criteria:", self.criteria)
        lay.addRow("Misconceptions:", self.misconceptions)
        lay.addRow("Alternatives:", self.alternatives)
        lay.addRow("Partial credit:", self.partial)

    def set_rubric(self, r: Rubric | None) -> None:
        if not r:
            return
        self.criteria.setPlainText("\n".join(f"{c.points:g} | {c.description} | {', '.join(c.expected_concepts)}" for c in r.criteria))
        self.misconceptions.setPlainText("\n".join(r.common_misconceptions))
        self.alternatives.setPlainText("\n".join(r.acceptable_alternatives))
        self.partial.setText(r.partial_credit_notes)

    def rubric(self) -> Rubric | None:
        crit = []
        for i, ln in enumerate(_lines(self.criteria.toPlainText())):
            parts = [p.strip() for p in ln.split("|")]
            try:
                pts = float(parts[0].replace(",", "."))
            except ValueError:
                raise ValueError(f"Rubric line {i + 1}: must start with the number of points.")
            if len(parts) < 2 or not parts[1]:
                raise ValueError(f"Rubric line {i + 1}: description missing.")
            concepts = [c.strip() for c in parts[2].split(",")] if len(parts) > 2 and parts[2] else []
            crit.append(RubricCriterion(id=f"c{i + 1}", description=parts[1], points=pts, expected_concepts=concepts))
        if not crit:
            return None
        return Rubric(criteria=crit, common_misconceptions=_lines(self.misconceptions.toPlainText()),
                      acceptable_alternatives=_lines(self.alternatives.toPlainText()), partial_credit_notes=self.partial.text().strip())


class QuestionEditor(QDialog):
    def __init__(self, parent: QWidget | None, question: Question | None = None, subject: str = "", topic: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit question" if question else "New question")
        self.resize(820, 760)
        self.original = question
        self.result: Question | None = None

        outer = QVBoxLayout(self)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        body = QWidget()
        sa.setWidget(body)
        outer.addWidget(sa, 1)
        lay = QVBoxLayout(body)

        form = QFormLayout()
        self.subject = QLineEdit(subject)
        self.topic = QLineEdit(topic)
        self.type = QComboBox()
        for t in QT:
            self.type.addItem(QUESTION_TYPE_LABELS[t], t)
        self.difficulty = QComboBox()
        for d in Difficulty:
            self.difficulty.addItem(d.value, d)
        self.points = QDoubleSpinBox()
        self.points.setRange(0.5, 100)
        self.points.setValue(1)
        self.points.setSingleStep(0.5)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(f"Question text. For fill-in-the-blank use {BLANK_MARKER} for each blank.")
        self.text.setMinimumHeight(90)
        self.code = QPlainTextEdit()
        self.code.setFont(QFont("Consolas", 10))
        self.code.setPlaceholderText("Optional code snippet / formula / data shown with the question")
        self.code.setMaximumHeight(110)
        form.addRow("Subject:", self.subject)
        form.addRow("Topic:", self.topic)
        row = QHBoxLayout()
        row.addWidget(self.type, 2)
        row.addWidget(label("Difficulty:", wrap=False))
        row.addWidget(self.difficulty, 1)
        row.addWidget(label("Points:", wrap=False))
        row.addWidget(self.points)
        form.addRow("Type:", row)
        form.addRow("Question:", self.text)
        form.addRow("Context / code:", self.code)
        lay.addLayout(form)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack)
        self.pages: dict[str, QWidget] = {}
        self._build_pages()

        form2 = QFormLayout()
        self.model_answer = QPlainTextEdit()
        self.model_answer.setPlaceholderText("Model / reference answer (used for review and AI grading)")
        self.model_answer.setMaximumHeight(80)
        self.explanation = QPlainTextEdit()
        self.explanation.setPlaceholderText("Explanation of the correct answer (shown after the exam)")
        self.explanation.setMaximumHeight(80)
        self.hint = QLineEdit()
        self.skills = QLineEdit()
        self.skills.setPlaceholderText("comma separated")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma separated")
        self.verified = QCheckBox("I have reviewed this question and its answer key")
        form2.addRow("Model answer:", self.model_answer)
        form2.addRow("Explanation:", self.explanation)
        form2.addRow("Hint:", self.hint)
        form2.addRow("Skills:", self.skills)
        form2.addRow("Tags:", self.tags)
        form2.addRow("", self.verified)
        lay.addLayout(form2)
        self.notes = label("", "muted")
        lay.addWidget(self.notes)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self.type.currentIndexChanged.connect(self._type_changed)
        if question:
            self._load(question)
        self._type_changed()

    # ------------------------------------------------------------------ pages
    def _build_pages(self) -> None:
        # choice
        w = QWidget()
        l = QVBoxLayout(w)
        l.addWidget(label("Options – tick the correct one(s):", "muted"))
        self.opt_table = QTableWidget(4, 2)
        self.opt_table.setHorizontalHeaderLabels(["Correct", "Option text"])
        self.opt_table.horizontalHeader().setStretchLastSection(True)
        self.opt_table.verticalHeader().setVisible(False)
        self._reset_options(4)
        l.addWidget(self.opt_table)
        r = QHBoxLayout()
        r.addWidget(button("Add option", slot=lambda: self._add_option()))
        r.addWidget(button("Remove last", slot=lambda: self.opt_table.setRowCount(max(2, self.opt_table.rowCount() - 1))))
        r.addStretch()
        l.addLayout(r)
        self.pages["choice"] = w
        # true/false
        w = QWidget()
        l = QFormLayout(w)
        self.tf = QComboBox()
        self.tf.addItems(["True", "False"])
        l.addRow("The statement is:", self.tf)
        self.pages["tf"] = w
        # short answer
        w = QWidget()
        l = QFormLayout(w)
        self.accepted = QPlainTextEdit()
        self.accepted.setPlaceholderText("Accepted answers, one per line")
        self.accepted.setMaximumHeight(90)
        self.case = QCheckBox("Case sensitive")
        l.addRow("Accepted answers:", self.accepted)
        l.addRow("", self.case)
        self.short_rubric = RubricEditor()
        l.addRow(label("Optional rubric for AI grading of free wording:", "muted"))
        l.addRow(self.short_rubric)
        self.pages["short"] = w
        # fill blank
        w = QWidget()
        l = QFormLayout(w)
        self.blanks = QPlainTextEdit()
        self.blanks.setPlaceholderText("One line per blank (in order); alternatives separated by |\ne.g.  180\n       went | had gone")
        l.addRow("Blank answers:", self.blanks)
        self.pages["fill"] = w
        # numerical
        w = QWidget()
        l = QFormLayout(w)
        self.num_value = QLineEdit()
        self.num_unit = QLineEdit()
        self.num_abs = QDoubleSpinBox()
        self.num_abs.setDecimals(6)
        self.num_abs.setRange(0, 1e9)
        self.num_rel = QDoubleSpinBox()
        self.num_rel.setRange(0, 50)
        self.num_rel.setValue(1)
        self.num_rel.setSuffix(" %")
        self.num_expr = QLineEdit()
        self.num_expr.setPlaceholderText("Optional expression that evaluates to the value, e.g. sqrt(5^2-1.4^2)")
        l.addRow("Correct value:", self.num_value)
        l.addRow("Unit (optional):", self.num_unit)
        l.addRow("Absolute tolerance:", self.num_abs)
        l.addRow("Relative tolerance:", self.num_rel)
        l.addRow("Check expression:", self.num_expr)
        self.pages["numerical"] = w
        # math
        w = QWidget()
        l = QFormLayout(w)
        self.math_expr = QLineEdit()
        self.math_expr.setPlaceholderText("Final answer as an expression, e.g. 3*x^2*sin(x) + x^3*cos(x)")
        l.addRow("Expected expression:", self.math_expr)
        self.math_rubric = RubricEditor()
        l.addRow(label("Optional rubric for partial credit on working steps:", "muted"))
        l.addRow(self.math_rubric)
        self.pages["math"] = w
        # matching
        w = QWidget()
        l = QFormLayout(w)
        self.pairs = QPlainTextEdit()
        self.pairs.setPlaceholderText("One pair per line:  left => right")
        l.addRow("Pairs:", self.pairs)
        self.pages["matching"] = w
        # ordering
        w = QWidget()
        l = QFormLayout(w)
        self.order = QPlainTextEdit()
        self.order.setPlaceholderText("Items in the CORRECT order, one per line (they are shuffled for the student)")
        l.addRow("Correct order:", self.order)
        self.pages["ordering"] = w
        # programming
        w = QWidget()
        l = QFormLayout(w)
        self.func = QLineEdit()
        self.tests = QPlainTextEdit()
        self.tests.setFont(QFont("Consolas", 10))
        self.tests.setPlaceholderText("One test per line:  call => expected   (prefix with 'hidden:' to hide)\n"
                                      "e.g.  add(2, 3) => 5")
        self.tests.setMaximumHeight(110)
        l.addRow("Function name:", self.func)
        l.addRow("Test cases:", self.tests)
        self.code_rubric = RubricEditor()
        l.addRow(label("Optional rubric (used when tests cannot run, or for code quality):", "muted"))
        l.addRow(self.code_rubric)
        self.pages["programming"] = w
        # essay / case
        self.essay_rubric = RubricEditor()
        self.pages["essay"] = self.essay_rubric
        for p in self.pages.values():
            self.stack.addWidget(p)

    def _reset_options(self, n: int) -> None:
        self.opt_table.setRowCount(0)
        for _ in range(n):
            self._add_option()

    def _add_option(self, text: str = "", correct: bool = False) -> None:
        r = self.opt_table.rowCount()
        self.opt_table.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked if correct else Qt.CheckState.Unchecked)
        self.opt_table.setItem(r, 0, chk)
        self.opt_table.setItem(r, 1, QTableWidgetItem(text))

    def _page_key(self, t: QuestionType) -> str:
        return {QT.SINGLE_CHOICE: "choice", QT.MULTIPLE_SELECT: "choice", QT.TRUE_FALSE: "tf", QT.SHORT_ANSWER: "short",
                QT.FILL_BLANK: "fill", QT.NUMERICAL: "numerical", QT.MATH_PROBLEM: "math", QT.MATCHING: "matching",
                QT.ORDERING: "ordering", QT.PROGRAMMING: "programming", QT.ESSAY: "essay", QT.CASE_STUDY: "essay"}[t]

    def _type_changed(self) -> None:
        self.stack.setCurrentWidget(self.pages[self._page_key(QuestionType(self.type.currentData()))])

    # ------------------------------------------------------------------ load / save
    def _load(self, q: Question) -> None:
        self.subject.setText(q.subject)
        self.topic.setText(q.topic)
        self.type.setCurrentIndex(self.type.findData(q.type.value))
        self.difficulty.setCurrentIndex(self.difficulty.findData(q.difficulty.value))
        self.points.setValue(q.max_points)
        self.text.setPlainText(q.text)
        self.code.setPlainText(q.code_context)
        self.model_answer.setPlainText(q.model_answer)
        self.explanation.setPlainText(q.explanation)
        self.hint.setText(q.hint)
        self.skills.setText(", ".join(q.skills))
        self.tags.setText(", ".join(q.tags))
        self.verified.setChecked(q.review_status == ReviewStatus.USER_VERIFIED)
        if q.type in (QT.SINGLE_CHOICE, QT.MULTIPLE_SELECT):
            self.opt_table.setRowCount(0)
            for o in q.options:
                self._add_option(o.text, o.id in q.correct_option_ids)
        elif q.type == QT.TRUE_FALSE:
            self.tf.setCurrentIndex(0 if "true" in q.correct_option_ids else 1)
        elif q.type == QT.SHORT_ANSWER:
            self.accepted.setPlainText("\n".join(q.accepted_answers))
            self.case.setChecked(q.case_sensitive)
            self.short_rubric.set_rubric(q.rubric)
        elif q.type == QT.FILL_BLANK:
            self.blanks.setPlainText("\n".join(" | ".join(b) for b in q.blank_answers))
        elif q.type == QT.NUMERICAL and q.numerical:
            n = q.numerical
            self.num_value.setText(f"{n.value:g}")
            self.num_unit.setText(n.unit or "")
            self.num_abs.setValue(n.abs_tolerance)
            self.num_rel.setValue(n.rel_tolerance * 100)
            self.num_expr.setText(n.solution_expression or "")
        elif q.type == QT.MATH_PROBLEM:
            self.math_expr.setText(q.expected_expression or "")
            self.math_rubric.set_rubric(q.rubric)
        elif q.type == QT.MATCHING:
            self.pairs.setPlainText("\n".join(f"{p.left} => {p.right}" for p in q.match_pairs))
        elif q.type == QT.ORDERING:
            self.order.setPlainText("\n".join(q.order_items))
        elif q.type == QT.PROGRAMMING:
            self.func.setText(q.function_name or "")
            self.tests.setPlainText("\n".join(("hidden: " if t.hidden else "") + f"{t.call} => {t.expected}" for t in q.test_cases))
            self.code_rubric.set_rubric(q.rubric)
        elif q.type in (QT.ESSAY, QT.CASE_STUDY):
            self.essay_rubric.set_rubric(q.rubric)
        if q.validation_notes:
            self.notes.setText("Notes: " + " · ".join(q.validation_notes[-6:]))

    def _build(self) -> Question:
        t = QuestionType(self.type.currentData())
        base = self.original.model_dump() if self.original else {}
        data = dict(
            id=base.get("id") or None,
            subject=self.subject.text().strip(), topic=self.topic.text().strip(), type=t,
            difficulty=Difficulty(self.difficulty.currentData()), max_points=self.points.value(), text=self.text.toPlainText().strip(),
            code_context=self.code.toPlainText(), model_answer=self.model_answer.toPlainText().strip(),
            explanation=self.explanation.toPlainText().strip(), hint=self.hint.text().strip(),
            skills=[s.strip() for s in self.skills.text().split(",") if s.strip()],
            tags=[s.strip() for s in self.tags.text().split(",") if s.strip()],
            provenance=self.original.provenance if self.original else Provenance.USER,
            source_ref=self.original.source_ref if self.original else None,
            created_at=self.original.created_at if self.original else None,
            version=self.original.version if self.original else 1,
        )
        data = {k: v for k, v in data.items() if v is not None}
        if t in (QT.SINGLE_CHOICE, QT.MULTIPLE_SELECT):
            opts, correct = [], []
            for r in range(self.opt_table.rowCount()):
                txt = (self.opt_table.item(r, 1).text() if self.opt_table.item(r, 1) else "").strip()
                if not txt:
                    continue
                oid = f"o{len(opts)}"
                opts.append(Option(id=oid, text=txt))
                if self.opt_table.item(r, 0).checkState() == Qt.CheckState.Checked:
                    correct.append(oid)
            data.update(options=opts, correct_option_ids=correct)
        elif t == QT.TRUE_FALSE:
            data.update(options=[Option(id="true", text="True"), Option(id="false", text="False")],
                        correct_option_ids=["true" if self.tf.currentIndex() == 0 else "false"])
        elif t == QT.SHORT_ANSWER:
            data.update(accepted_answers=_lines(self.accepted.toPlainText()), case_sensitive=self.case.isChecked(),
                        rubric=self.short_rubric.rubric())
        elif t == QT.FILL_BLANK:
            data["blank_answers"] = [[a.strip() for a in ln.split("|") if a.strip()] for ln in _lines(self.blanks.toPlainText())]
        elif t == QT.NUMERICAL:
            try:
                val = float(self.num_value.text().replace(",", "."))
            except ValueError:
                raise ValueError("The correct value must be a number.")
            data["numerical"] = NumericalAnswer(value=val, unit=self.num_unit.text().strip() or None,
                                                abs_tolerance=self.num_abs.value(), rel_tolerance=self.num_rel.value() / 100,
                                                solution_expression=self.num_expr.text().strip() or None)
        elif t == QT.MATH_PROBLEM:
            data.update(expected_expression=self.math_expr.text().strip() or None, rubric=self.math_rubric.rubric())
        elif t == QT.MATCHING:
            pairs = []
            for i, ln in enumerate(_lines(self.pairs.toPlainText())):
                if "=>" not in ln:
                    raise ValueError(f"Pair line {i + 1} needs '=>' between left and right.")
                a, b = ln.split("=>", 1)
                pairs.append(MatchPair(id=f"p{i}", left=a.strip(), right=b.strip()))
            data["match_pairs"] = pairs
        elif t == QT.ORDERING:
            data["order_items"] = _lines(self.order.toPlainText())
        elif t == QT.PROGRAMMING:
            tests = []
            for i, ln in enumerate(_lines(self.tests.toPlainText())):
                hidden = ln.lower().startswith("hidden:")
                if hidden:
                    ln = ln[7:].strip()
                if "=>" not in ln:
                    raise ValueError(f"Test line {i + 1} needs '=>' between call and expected value.")
                c, e = ln.split("=>", 1)
                tests.append(CodeTestCase(call=c.strip(), expected=e.strip(), hidden=hidden))
            data.update(function_name=self.func.text().strip() or None, test_cases=tests, rubric=self.code_rubric.rubric())
        elif t in (QT.ESSAY, QT.CASE_STUDY):
            data["rubric"] = self.essay_rubric.rubric()
        q = Question(**data)
        q.review_status = ReviewStatus.USER_VERIFIED if self.verified.isChecked() else (
            self.original.review_status if self.original and self.original.review_status != ReviewStatus.USER_VERIFIED else ReviewStatus.UNVERIFIED)
        q.validation_notes = list(self.original.validation_notes) if self.original else []
        if self.original and self.original.provenance in (Provenance.AI_GENERATED, Provenance.SOURCE_DERIVED):
            q.validation_notes.append("Edited by the user.")
        return q

    def _save(self) -> None:
        try:
            q = self._build()
        except Exception as exc:
            msg = str(exc)
            if hasattr(exc, "errors"):
                e = exc.errors()[0]  # type: ignore[attr-defined]
                msg = f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}"
            show_error(self, "Invalid question", msg)
            return
        rep = validate_question(q)
        if not rep.ok:
            show_error(self, "Question is not valid", "\n".join(f"• {e}" for e in rep.errors))
            return
        if rep.warnings:
            self.notes.setText("Warnings: " + " · ".join(rep.warnings))
        self.result = q
        self.accept()

"""Answer input widgets, one per question format."""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..exams.question_models import Question, QuestionType
from .widgets import button, label

MONO = QFont("Consolas", 10)


class AnswerWidget(QWidget):
    changed = Signal()
    debounced = False  # text widgets: save after a short pause

    def value(self) -> Any: ...

    def set_value(self, value: Any) -> None: ...

    def set_readonly(self, ro: bool) -> None:
        self.setEnabled(not ro)


class ChoiceAnswer(AnswerWidget):
    def __init__(self, q: Question, order: list[str]) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QRadioButton] = {}
        texts = {o.id: o.text for o in q.options}
        for i, oid in enumerate(order or [o.id for o in q.options]):
            prefix = "" if q.type == QuestionType.TRUE_FALSE else f"{chr(65 + i)})  "
            rb = QRadioButton(prefix + texts[oid])
            rb.setStyleSheet("QRadioButton { padding: 6px; }")
            self.group.addButton(rb)
            self.buttons[oid] = rb
            lay.addWidget(rb)
            rb.toggled.connect(lambda on: on and self.changed.emit())
        lay.addStretch()

    def value(self) -> Any:
        return next((oid for oid, b in self.buttons.items() if b.isChecked()), None)

    def set_value(self, value: Any) -> None:
        self.blockSignals(True)
        for oid, b in self.buttons.items():
            b.setChecked(oid == value)
        self.blockSignals(False)


class MultiAnswer(AnswerWidget):
    def __init__(self, q: Question, order: list[str]) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(label("Select all that apply.", "muted"))
        self.boxes: dict[str, QCheckBox] = {}
        texts = {o.id: o.text for o in q.options}
        for i, oid in enumerate(order or [o.id for o in q.options]):
            cb = QCheckBox(f"{chr(65 + i)})  {texts[oid]}")
            cb.setStyleSheet("QCheckBox { padding: 6px; }")
            self.boxes[oid] = cb
            lay.addWidget(cb)
            cb.toggled.connect(lambda _: self.changed.emit())
        lay.addStretch()

    def value(self) -> Any:
        return [oid for oid, b in self.boxes.items() if b.isChecked()]

    def set_value(self, value: Any) -> None:
        self.blockSignals(True)
        chosen = set(value or [])
        for oid, b in self.boxes.items():
            b.setChecked(oid in chosen)
        self.blockSignals(False)


class LineAnswer(AnswerWidget):
    debounced = True

    def __init__(self, placeholder: str = "Type your answer") -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        lay.addWidget(self.edit)
        lay.addStretch()
        self.edit.textChanged.connect(lambda _: self.changed.emit())

    def value(self) -> Any:
        return self.edit.text()

    def set_value(self, value: Any) -> None:
        self.edit.blockSignals(True)
        self.edit.setText("" if value is None else str(value))
        self.edit.blockSignals(False)

    def set_readonly(self, ro: bool) -> None:
        self.edit.setReadOnly(ro)


class CodeEdit(QPlainTextEdit):
    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Tab:
            self.insertPlainText("    ")
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            line = self.textCursor().block().text()
            indent = re.match(r"\s*", line).group(0)
            if line.rstrip().endswith(":"):
                indent += "    "
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(e)


class TextAnswer(AnswerWidget):
    debounced = True

    def __init__(self, placeholder: str, code: bool = False, min_height: int = 220, note: str = "") -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        if note:
            lay.addWidget(label(note, "muted"))
        self.edit = CodeEdit() if code else QPlainTextEdit()
        if code:
            self.edit.setFont(MONO)
            self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumHeight(min_height)
        lay.addWidget(self.edit)
        self.count = label("", "muted")
        lay.addWidget(self.count)
        self.edit.textChanged.connect(self._changed)

    def _changed(self) -> None:
        words = len(self.edit.toPlainText().split())
        self.count.setText(f"{words} word(s)")
        self.changed.emit()

    def value(self) -> Any:
        return self.edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self.edit.blockSignals(True)
        self.edit.setPlainText("" if value is None else str(value))
        self.edit.blockSignals(False)
        self.count.setText(f"{len(self.edit.toPlainText().split())} word(s)")

    def set_readonly(self, ro: bool) -> None:
        self.edit.setReadOnly(ro)


class FillBlankAnswer(AnswerWidget):
    debounced = True

    def __init__(self, q: Question) -> None:
        super().__init__()
        lay = QFormLayout(self)
        n = len(re.findall(r"_{3,}", q.text))
        self.edits = []
        for i in range(n):
            e = QLineEdit()
            e.setPlaceholderText(f"Blank {i + 1}")
            e.textChanged.connect(lambda _: self.changed.emit())
            lay.addRow(f"Blank {i + 1}:", e)
            self.edits.append(e)

    def value(self) -> Any:
        return [e.text() for e in self.edits]

    def set_value(self, value: Any) -> None:
        vals = list(value or [])
        for i, e in enumerate(self.edits):
            e.blockSignals(True)
            e.setText(str(vals[i]) if i < len(vals) and vals[i] is not None else "")
            e.blockSignals(False)

    def set_readonly(self, ro: bool) -> None:
        for e in self.edits:
            e.setReadOnly(ro)


class MatchingAnswer(AnswerWidget):
    def __init__(self, q: Question, rights: list[str]) -> None:
        super().__init__()
        lay = QFormLayout(self)
        lay.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.combos: dict[str, QComboBox] = {}
        for p in q.match_pairs:
            cb = QComboBox()
            cb.addItem("— select —", "")
            for r in rights or [x.right for x in q.match_pairs]:
                cb.addItem(r, r)
            cb.currentIndexChanged.connect(lambda _: self.changed.emit())
            lab = QLabel(p.left)
            lab.setWordWrap(True)
            lay.addRow(lab, cb)
            self.combos[p.id] = cb

    def value(self) -> Any:
        return {pid: cb.currentData() for pid, cb in self.combos.items() if cb.currentData()}

    def set_value(self, value: Any) -> None:
        value = value or {}
        for pid, cb in self.combos.items():
            cb.blockSignals(True)
            idx = cb.findData(value.get(pid, ""))
            cb.setCurrentIndex(max(0, idx))
            cb.blockSignals(False)


class OrderingAnswer(AnswerWidget):
    def __init__(self, items: list[str]) -> None:
        super().__init__()
        self._initial = list(items)
        self._touched = False
        lay = QVBoxLayout(self)
        lay.addWidget(label("Drag items (or use the buttons) into the correct order, first at the top.", "muted"))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setMinimumHeight(200)
        self.list.model().rowsMoved.connect(self._moved)
        row.addWidget(self.list, 1)
        col = QVBoxLayout()
        col.addWidget(button("▲ Up", slot=lambda: self._shift(-1)))
        col.addWidget(button("▼ Down", slot=lambda: self._shift(1)))
        self.confirm_btn = button("Confirm order", slot=self._confirm, tooltip="Count the current order as your answer")
        col.addWidget(self.confirm_btn)
        col.addStretch()
        row.addLayout(col)
        lay.addLayout(row)
        self._fill(items)

    def _fill(self, items: list[str]) -> None:
        self.list.clear()
        for it in items:
            QListWidgetItem(it, self.list)

    def _moved(self, *args: Any) -> None:
        self._touched = True
        self.changed.emit()

    def _confirm(self) -> None:
        self._touched = True
        self.changed.emit()

    def _shift(self, d: int) -> None:
        r = self.list.currentRow()
        if r < 0 or not 0 <= r + d < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + d, it)
        self.list.setCurrentRow(r + d)
        self._moved()

    def value(self) -> Any:
        if not self._touched:
            return None
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_value(self, value: Any) -> None:
        if value:
            self._touched = True
            self._fill(list(value))
        else:
            self._touched = False
            self._fill(self._initial)

    def set_readonly(self, ro: bool) -> None:
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop if ro else QAbstractItemView.DragDropMode.InternalMove)
        self.list.setEnabled(not ro)
        self.confirm_btn.setEnabled(not ro)


def make_answer_widget(q: Question, option_order: list[str], display_order: list[str]) -> AnswerWidget:
    t = q.type
    if t in (QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE):
        return ChoiceAnswer(q, option_order)
    if t == QuestionType.MULTIPLE_SELECT:
        return MultiAnswer(q, option_order)
    if t == QuestionType.SHORT_ANSWER:
        return LineAnswer("Type a short answer")
    if t == QuestionType.NUMERICAL:
        unit_hint = f" (include the unit, e.g. '{q.numerical.unit}')" if q.numerical and q.numerical.unit else ""
        return LineAnswer("Enter a number, e.g. 9.81, 1/3, 2*sqrt(2)" + unit_hint)
    if t == QuestionType.FILL_BLANK:
        return FillBlankAnswer(q)
    if t == QuestionType.MATH_PROBLEM:
        return TextAnswer("Write your working…\nPut the final answer on the last line, e.g.  f'(x) = 3x^2 + 2",
                          note="Use ^ for powers, sqrt(), sin(), pi … The last line is checked for mathematical equivalence.")
    if t == QuestionType.PROGRAMMING:
        w = TextAnswer(f"def {q.function_name or 'solution'}(...):\n    ...", code=True, min_height=280,
                       note="Python. Tab inserts 4 spaces.")
        return w
    if t == QuestionType.MATCHING:
        return MatchingAnswer(q, display_order)
    if t == QuestionType.ORDERING:
        return OrderingAnswer(display_order or list(q.order_items))
    return TextAnswer("Write your answer…", min_height=260)

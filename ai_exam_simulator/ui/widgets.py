"""Reusable widgets and dialogs."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..ai.hybrid_router import RouteDecision
from ..ai.provider_interface import ProviderKind
from ..config.settings import OPERATION_LABELS, AIOperation
from .workers import Worker, run_in_background


def label(text: str = "", role: str | None = None, wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def set_role(w: QWidget, role: str) -> None:
    w.setProperty("role", role)
    w.style().unpolish(w)
    w.style().polish(w)


def button(text: str, role: str | None = None, slot: Callable | None = None, tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if role:
        b.setProperty("role", role)
    if slot:
        b.clicked.connect(slot)
    if tooltip:
        b.setToolTip(tooltip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def card(layout_cls=QVBoxLayout) -> tuple[QFrame, Any]:
    f = QFrame()
    f.setProperty("role", "card")
    lay = layout_cls(f)
    lay.setContentsMargins(14, 12, 14, 12)
    return f, lay


class StatTile(QFrame):
    def __init__(self, title: str, value: str = "—", sub: str = "") -> None:
        super().__init__()
        self.setProperty("role", "card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.title = label(title, "muted")
        self.value = label(value, "h1")
        self.sub = label(sub, "muted")
        for w in (self.title, self.value, self.sub):
            lay.addWidget(w)

    def set(self, value: str, sub: str = "", role: str | None = None) -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        set_role(self.value, role or "h1")
        if role:
            f = self.value.font()
            f.setPointSize(18)
            self.value.setFont(f)


def show_error(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def show_info(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def confirm(parent: QWidget | None, title: str, text: str, yes: str = "Yes", no: str = "Cancel") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    y = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(no, QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is y


class ProgressDialog(QDialog):
    """Modal progress display with cancellation for a background worker."""

    def __init__(self, parent: QWidget | None, title: str, text: str = "Working…") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        self.text = label(text)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.cancel_btn = button("Cancel")
        self.worker: Worker | None = None
        lay.addWidget(self.text)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.cancel_btn.clicked.connect(self._cancel)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
        self.text.setText("Cancelling… (waiting for the current step to stop)")
        self.cancel_btn.setEnabled(False)

    def update_progress(self, msg: str, done: int, total: int) -> None:
        self.text.setText(msg)
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)

    def reject(self) -> None:  # Esc → cancel instead of closing silently
        self._cancel()


def run_with_progress(
    parent: QWidget | None,
    title: str,
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    text: str = "Working…",
    cancellable: bool = True,
    **kwargs: Any,
) -> Worker:
    dlg = ProgressDialog(parent, title, text)
    dlg.cancel_btn.setVisible(cancellable)

    def done(result: Any) -> None:
        dlg.accept()
        if on_done:
            on_done(result)

    def failed(msg: str) -> None:
        dlg.accept()
        if on_error:
            on_error(msg)
        else:
            show_error(parent, title, msg)

    def cancelled() -> None:
        dlg.accept()
        show_info(parent, title, "The operation was cancelled.")

    w = run_in_background(fn, *args, on_done=done, on_error=failed, on_progress=dlg.update_progress,
                          on_cancel=cancelled, **kwargs)
    dlg.worker = w
    dlg.show()
    return w


# ------------------------------------------------------------------------------------- consent


class ConsentMemory:
    """Remembers 'don't ask again this session' choices per operation (never persisted)."""

    def __init__(self) -> None:
        self.allowed: set[AIOperation] = set()


CONSENT = ConsentMemory()


class CloudConsentDialog(QDialog):
    ALLOW, LOCAL, CANCEL = 1, 2, 0

    def __init__(self, parent: QWidget | None, decision: RouteDecision, what_is_sent: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Send data to a cloud AI provider?")
        self.setMinimumWidth(560)
        self.choice = self.CANCEL
        cloud = next(p for p in decision.chain if p.kind == ProviderKind.CLOUD)
        has_local = any(p.kind == ProviderKind.LOCAL for p in decision.chain)
        lay = QVBoxLayout(self)
        lay.addWidget(label("This operation may send data to a cloud AI service", "h2"))
        host = ""
        try:
            host = urlparse(getattr(cloud, "_url")()).hostname or ""
        except Exception:
            pass
        role = "used first" if decision.cloud_is_primary else "used only if the local model fails"
        lay.addWidget(label(
            f"<b>Operation:</b> {OPERATION_LABELS[decision.operation]}<br>"
            f"<b>Provider:</b> {cloud.describe()}{' · ' + host if host else ''} ({role})<br>"
            f"<b>Data that would be sent:</b> {what_is_sent}<br><br>"
            "The provider's own privacy terms apply to this data and API usage may be billed to your account. "
            "Your API key is sent only to this provider. Exam history and other study materials are not sent."
        ))
        self.remember = QCheckBox("Don't ask again for this operation until the application is closed")
        lay.addWidget(self.remember)
        bb = QDialogButtonBox()
        allow = bb.addButton("Allow cloud", QDialogButtonBox.ButtonRole.AcceptRole)
        allow.setProperty("role", "primary")
        if has_local:
            local = bb.addButton("Use local model only", QDialogButtonBox.ButtonRole.ActionRole)
            local.clicked.connect(lambda: self._set(self.LOCAL))
        cancel = bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        allow.clicked.connect(lambda: self._set(self.ALLOW))
        cancel.clicked.connect(lambda: self._set(self.CANCEL))
        lay.addWidget(bb)

    def _set(self, value: int) -> None:
        self.choice = value
        self.accept()


def ai_preflight(parent: QWidget | None, decision: RouteDecision, what_is_sent: str) -> bool | None:
    """Check provider availability and ask for cloud consent.

    Returns True (cloud allowed), False (local only) or None (cannot/should not proceed).
    """
    if not decision.possible:
        show_error(parent, "AI not available",
                   decision.explain() + "\n\nConfigure a local Ollama model or a cloud provider in AI Settings. "
                   "All non-AI features keep working.")
        return None
    if not decision.may_use_cloud:
        return False
    if decision.operation in CONSENT.allowed:
        return True
    dlg = CloudConsentDialog(parent, decision, what_is_sent)
    dlg.exec()
    if dlg.choice == CloudConsentDialog.ALLOW:
        if dlg.remember.isChecked():
            CONSENT.allowed.add(decision.operation)
        return True
    if dlg.choice == CloudConsentDialog.LOCAL:
        return False
    return None


class Banner(QLabel):
    def __init__(self, text: str = "", warn: bool = False) -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.setProperty("role", "warnbanner" if warn else "banner")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setOpenExternalLinks(True)
        self.setVisible(bool(text))

    def show_text(self, text: str, warn: bool | None = None) -> None:
        if warn is not None:
            set_role(self, "warnbanner" if warn else "banner")
        self.setText(text)
        self.setVisible(bool(text))

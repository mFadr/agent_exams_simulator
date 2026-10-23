"""Background execution for slow operations (AI calls, imports, exports).

Keeps the UI thread responsive; results are delivered back through Qt signals,
which are queued onto the UI thread automatically.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..ai.provider_interface import AIError, CancelToken, OperationCancelled
from ..security.redaction import redact

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class Worker(QRunnable):
    """Runs ``fn(*args, cancel=token, progress=callback, **kwargs)`` in the thread pool."""

    def __init__(self, fn: Callable[..., Any], *args: Any, pass_control: bool = True,
                 deliver_partial: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self.deliver_partial = deliver_partial  # emit finished() with the partial result even if cancelled
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.pass_control = pass_control
        self.token = CancelToken()
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self.token.cancel()

    def _progress(self, msg: str, done: int, total: int) -> None:
        self.signals.progress.emit(msg, done, total)

    def run(self) -> None:
        try:
            if self.pass_control:
                result = self.fn(*self.args, cancel=self.token, progress=self._progress, **self.kwargs)
            else:
                result = self.fn(*self.args, **self.kwargs)
        except OperationCancelled:
            self.signals.cancelled.emit()
            return
        except AIError as exc:
            self.signals.failed.emit(redact(str(exc)))
            return
        except Exception as exc:
            log.error("Background task failed: %s", redact(traceback.format_exc()))
            self.signals.failed.emit(redact(f"{type(exc).__name__}: {exc}"))
            return
        if self.token.cancelled and not self.deliver_partial:
            self.signals.cancelled.emit()
        else:
            self.signals.finished.emit(result)


_active: set[Worker] = set()


def run_in_background(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
    pass_control: bool = True,
    deliver_partial: bool = False,
    **kwargs: Any,
) -> Worker:
    w = Worker(fn, *args, pass_control=pass_control, deliver_partial=deliver_partial, **kwargs)
    _active.add(w)  # keep python-side reference until done

    def _cleanup(*_: Any) -> None:
        _active.discard(w)

    if on_done:
        w.signals.finished.connect(on_done)
    if on_error:
        w.signals.failed.connect(on_error)
    if on_progress:
        w.signals.progress.connect(on_progress)
    if on_cancel:
        w.signals.cancelled.connect(on_cancel)
    for sig in (w.signals.finished, w.signals.failed, w.signals.cancelled):
        sig.connect(_cleanup)
    QThreadPool.globalInstance().start(w)
    return w

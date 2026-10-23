"""AI Exam Simulator - entry point.

Run with:  python main.py
"""

from __future__ import annotations

import logging
import sys
import traceback


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ai_exam_simulator import APP_NAME
    from ai_exam_simulator.app.application import AppContext
    from ai_exam_simulator.security.redaction import redact
    from ai_exam_simulator.storage.database import DatabaseError

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("AIExamSimulator")

    def excepthook(exc_type, exc, tb):  # last-resort handler: log and inform, never crash silently
        text = redact("".join(traceback.format_exception(exc_type, exc, tb)))
        logging.getLogger("unhandled").error(text)
        QMessageBox.critical(None, "Unexpected error",
                             f"An unexpected error occurred:\n{redact(str(exc))}\n\nExam answers are saved automatically. "
                             "Details were written to the log file.")

    sys.excepthook = excepthook

    try:
        ctx = AppContext.create()
    except DatabaseError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"The application could not start: {redact(str(exc))}")
        return 1

    from ai_exam_simulator.ui.main_window import MainWindow

    win = MainWindow(ctx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

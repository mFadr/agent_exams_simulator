"""AI settings (local / cloud / hybrid) and application settings."""

from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.cloud_provider import API_KEY_SECRET_NAME
from ..app.application import AppContext
from ..config.defaults import CLOUD_PROVIDER_PRESETS, RECOMMENDED_LOCAL_MODELS
from ..config.settings import OPERATION_LABELS, AIOperation, RoutingMode
from ..grading.code_grader import find_python_interpreter
from ..reports.export_service import ExportError, create_backup, export_all_data_json, stage_restore
from ..security.secrets import mask_key
from ..security.validation import InputValidationError, validate_cloud_url, validate_http_url
from .widgets import Banner, button, confirm, label, run_with_progress, show_error, show_info

MODE_TEXT = {
    RoutingMode.LOCAL_ONLY: ("Local only", "All AI runs on this computer through Ollama. Nothing leaves your PC. Quality and speed depend on the model and hardware."),
    RoutingMode.CLOUD_ONLY: ("Cloud only", "All AI operations use the configured cloud provider (requires an API key; usage may be billed). You are asked before data is sent."),
    RoutingMode.HYBRID_AUTO: ("Hybrid (automatic)", "Local model for routine generation, explanations and hints; cloud model for nuanced grading of written/code answers and hard questions. "
                              "Falls back to the other provider on failure - never sending data to the cloud without your consent."),
    RoutingMode.MANUAL: ("Manual selection", "You choose one provider for every operation."),
}


def _scroll(w: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(w)
    return sa


class AISettingsView(QWidget):
    saved = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.addWidget(label("AI settings", "h1"))
        self.provider_line = label("", "muted")
        outer.addWidget(self.provider_line)
        body = QWidget()
        grid = QGridLayout(body)
        outer.addWidget(_scroll(body), 1)

        # routing ------------------------------------------------------------
        g = QGroupBox("How AI is used")
        gl = QVBoxLayout(g)
        self.mode_group = QButtonGroup(self)
        self.mode_btns: dict[RoutingMode, QRadioButton] = {}
        for m, (title, desc) in MODE_TEXT.items():
            rb = QRadioButton(title)
            self.mode_group.addButton(rb)
            self.mode_btns[m] = rb
            gl.addWidget(rb)
            gl.addWidget(label(desc, "muted"))
        mr = QHBoxLayout()
        mr.addWidget(label("Manual provider:", wrap=False))
        self.manual = QComboBox()
        self.manual.addItem("Local (Ollama)", "local")
        self.manual.addItem("Cloud", "cloud")
        mr.addWidget(self.manual)
        mr.addStretch()
        gl.addLayout(mr)
        gl.addWidget(label("Objective questions are always scored by deterministic rules, never by AI.", "muted"))
        grid.addWidget(g, 0, 0, 1, 2)

        # local -----------------------------------------------------------------
        g = QGroupBox("Local AI (Ollama)")
        fl = QFormLayout(g)
        self.ollama_url = QLineEdit()
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_timeout = QSpinBox()
        self.ollama_timeout.setRange(10, 1800)
        self.ollama_timeout.setSuffix(" s")
        self.ollama_status = Banner()
        fl.addRow("Endpoint:", self.ollama_url)
        mrow = QHBoxLayout()
        mrow.addWidget(self.ollama_model, 1)
        mrow.addWidget(button("Check Ollama", slot=self._check_ollama))
        fl.addRow("Model:", mrow)
        fl.addRow("Timeout:", self.ollama_timeout)
        fl.addRow(self.ollama_status)
        rec = "<br>".join(f"<code>ollama pull {m}</code> – {html.escape(d)}" for m, d in RECOMMENDED_LOCAL_MODELS)
        fl.addRow(label(f"No model yet? Install Ollama from <a href='https://ollama.com/download'>ollama.com</a>, then run one of:<br>{rec}<br>"
                        "No GPU is required, but responses are much faster with one.", "muted"))
        grid.addWidget(g, 1, 0)

        # cloud -------------------------------------------------------------------
        g = QGroupBox("Cloud AI (OpenAI-compatible API)")
        fc = QFormLayout(g)
        self.preset = QComboBox()
        self.preset.addItems(list(CLOUD_PROVIDER_PRESETS))
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.cloud_url = QLineEdit()
        self.cloud_model = QComboBox()
        self.cloud_model.setEditable(True)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_info = label("", "muted")
        self.cloud_timeout = QSpinBox()
        self.cloud_timeout.setRange(10, 600)
        self.cloud_timeout.setSuffix(" s")
        self.json_mode = QCheckBox("Request JSON mode (disable if your provider rejects 'response_format')")
        self.cloud_status = Banner()
        fc.addRow("Provider:", self.preset)
        fc.addRow("API endpoint:", self.cloud_url)
        crow = QHBoxLayout()
        crow.addWidget(self.cloud_model, 1)
        crow.addWidget(button("Fetch models", slot=self._fetch_models))
        fc.addRow("Model:", crow)
        krow = QHBoxLayout()
        krow.addWidget(self.api_key, 1)
        krow.addWidget(button("Save key", slot=self._save_key))
        krow.addWidget(button("Remove key", "danger", slot=self._delete_key))
        fc.addRow("API key:", krow)
        fc.addRow("", self.key_info)
        fc.addRow("Timeout:", self.cloud_timeout)
        fc.addRow(self.json_mode)
        trow = QHBoxLayout()
        trow.addWidget(button("Test connection", slot=self._test_cloud, tooltip="Sends a one-word test prompt (no personal data)"))
        trow.addStretch()
        fc.addRow(trow)
        fc.addRow(self.cloud_status)
        grid.addWidget(g, 1, 1)

        # permissions -------------------------------------------------------------
        g = QGroupBox("Cloud permissions & quality")
        pl = QVBoxLayout(g)
        pl.addWidget(label("Operations that may use the cloud provider (you are still asked before each operation):", "muted"))
        pg = QGridLayout()
        self.op_boxes: dict[AIOperation, QCheckBox] = {}
        for i, op in enumerate(AIOperation):
            cb = QCheckBox(OPERATION_LABELS[op])
            self.op_boxes[op] = cb
            pg.addWidget(cb, i // 2, i % 2)
        pl.addLayout(pg)
        self.allow_source = QCheckBox("Allow excerpts from my study materials to be sent to the cloud")
        self.verify = QCheckBox("Verify generated questions with an independent second pass (slower, catches wrong answer keys)")
        pl.addWidget(self.allow_source)
        pl.addWidget(self.verify)
        rr = QHBoxLayout()
        rr.addWidget(label("Retries on temporary errors:", wrap=False))
        self.retries = QSpinBox()
        self.retries.setRange(0, 5)
        rr.addWidget(self.retries)
        rr.addWidget(label("Temperature:", wrap=False))
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 1.5)
        self.temperature.setSingleStep(0.1)
        rr.addWidget(self.temperature)
        rr.addStretch()
        pl.addLayout(rr)
        grid.addWidget(g, 2, 0)

        # usage -----------------------------------------------------------------------
        g = QGroupBox("AI usage log (metadata only – no prompts or answers)")
        ul = QVBoxLayout(g)
        self.usage_totals = label("", "muted")
        ul.addWidget(self.usage_totals)
        self.usage = QTableWidget(0, 6)
        self.usage.setHorizontalHeaderLabels(["Time", "Operation", "Provider", "Model", "Result", "Chars to cloud"])
        self.usage.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.usage.verticalHeader().setVisible(False)
        self.usage.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.usage.setMinimumHeight(180)
        ul.addWidget(self.usage)
        ul.addWidget(button("Clear log", slot=self._clear_usage))
        grid.addWidget(g, 2, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(button("Save settings", "primary", slot=self.save))
        outer.addLayout(bottom)
        self.load()

    # ------------------------------------------------------------------ load/save
    def load(self) -> None:
        s = self.ctx.settings.settings.ai
        self.mode_btns[s.routing_mode].setChecked(True)
        self.manual.setCurrentIndex(max(0, self.manual.findData(s.manual_provider)))
        self.ollama_url.setText(s.ollama_url)
        self.ollama_model.setCurrentText(s.ollama_model)
        self.ollama_timeout.setValue(s.ollama_timeout_s)
        self.preset.blockSignals(True)
        self.preset.setCurrentText(s.cloud_provider_name if s.cloud_provider_name in CLOUD_PROVIDER_PRESETS else "Custom (OpenAI-compatible)")
        self.preset.blockSignals(False)
        self.cloud_url.setText(s.cloud_base_url)
        self.cloud_model.setCurrentText(s.cloud_model)
        self.cloud_timeout.setValue(s.cloud_timeout_s)
        self.json_mode.setChecked(s.cloud_json_mode)
        for op, cb in self.op_boxes.items():
            cb.setChecked(op in s.cloud_allowed_operations)
        self.allow_source.setChecked(s.allow_source_material_to_cloud)
        self.verify.setChecked(s.verify_generated_questions)
        self.retries.setValue(s.max_retries)
        self.temperature.setValue(s.temperature)
        self._update_key_info()
        self.refresh_usage()
        self.provider_line.setText("Currently: " + self.ctx.router.status_line())

    def save(self, quiet: bool = False) -> bool:
        s = self.ctx.settings.settings.ai
        try:
            ollama_url = validate_http_url(self.ollama_url.text(), field="Ollama endpoint")
            cloud_url = self.cloud_url.text().strip()
            if cloud_url:
                cloud_url = validate_cloud_url(cloud_url)
        except InputValidationError as exc:
            show_error(self, "Invalid setting", str(exc))
            return False
        s.routing_mode = next(m for m, rb in self.mode_btns.items() if rb.isChecked())
        s.manual_provider = self.manual.currentData()
        s.ollama_url = ollama_url
        s.ollama_model = self.ollama_model.currentText().strip()
        s.ollama_timeout_s = self.ollama_timeout.value()
        s.cloud_provider_name = self.preset.currentText()
        s.cloud_base_url = cloud_url
        s.cloud_model = self.cloud_model.currentText().strip()
        s.cloud_timeout_s = self.cloud_timeout.value()
        s.cloud_json_mode = self.json_mode.isChecked()
        s.cloud_allowed_operations = [op for op, cb in self.op_boxes.items() if cb.isChecked()]
        s.allow_source_material_to_cloud = self.allow_source.isChecked()
        s.verify_generated_questions = self.verify.isChecked()
        s.max_retries = self.retries.value()
        s.temperature = self.temperature.value()
        if self.api_key.text().strip():
            self._save_key(silent=True)
        try:
            self.ctx.settings.save()
        except OSError as exc:
            show_error(self, "Settings", f"Could not save settings: {exc}")
            return False
        self.provider_line.setText("Currently: " + self.ctx.router.status_line())
        self.saved.emit()
        if not quiet:
            show_info(self, "Settings", "AI settings saved.")
        return True

    # ------------------------------------------------------------------ local
    def _check_ollama(self) -> None:
        if not self.save(quiet=True):
            return
        self.ollama_status.show_text("Checking…")

        def done(st) -> None:
            cur = self.ollama_model.currentText()
            if st.models:
                self.ollama_model.clear()
                self.ollama_model.addItems(st.models)
                self.ollama_model.setCurrentText(cur if cur in st.models else st.models[0])
                if not cur:
                    self.save(quiet=True)
                    st = self.ctx.local_provider.check_status()
            self.ollama_status.show_text(("✅ " if st.available else "⚠ ") + html.escape(st.message), warn=not st.available)

        run_with_progress(self, "Ollama", self.ctx.local_provider.check_status, pass_control=False, on_done=done,
                          cancellable=False, text="Contacting Ollama…")

    # ------------------------------------------------------------------ cloud
    def _preset_changed(self, name: str) -> None:
        url = CLOUD_PROVIDER_PRESETS.get(name, "")
        if url:
            self.cloud_url.setText(url)
        self.cloud_model.clear()

    def _update_key_info(self) -> None:
        stored = self.ctx.secrets.get(API_KEY_SECRET_NAME)
        self.api_key.clear()
        self.api_key.setPlaceholderText("Key stored: " + mask_key(stored) if stored else "Paste your API key")
        self.key_info.setText(f"Storage: {self.ctx.secrets.backend_description}. Keys are never written to the config file, database or logs.")

    def _save_key(self, silent: bool = False) -> None:
        key = self.api_key.text().strip()
        if not key:
            if not silent:
                show_error(self, "API key", "Paste a key first.")
            return
        if any(c.isspace() for c in key) or len(key) < 8:
            show_error(self, "API key", "That does not look like a valid API key.")
            return
        try:
            self.ctx.secrets.set(API_KEY_SECRET_NAME, key)
        except Exception as exc:
            show_error(self, "API key", f"Could not store the key securely ({type(exc).__name__}).")
            return
        self._update_key_info()
        if not silent:
            show_info(self, "API key", "Key stored securely.")

    def _delete_key(self) -> None:
        if confirm(self, "Remove API key", "Remove the stored API key from this computer?"):
            self.ctx.secrets.delete(API_KEY_SECRET_NAME)
            self._update_key_info()

    def _fetch_models(self) -> None:
        if not self.save(quiet=True):
            return

        def done(models: list[str]) -> None:
            cur = self.cloud_model.currentText()
            self.cloud_model.clear()
            self.cloud_model.addItems(models)
            self.cloud_model.setCurrentText(cur)
            self.cloud_status.show_text(f"{len(models)} model(s) available.")

        run_with_progress(self, "Cloud models", self.ctx.cloud_provider.list_models, pass_control=False, on_done=done,
                          on_error=lambda m: self.cloud_status.show_text("⚠ " + html.escape(m), warn=True), cancellable=False,
                          text="Fetching the model list…")

    def _test_cloud(self) -> None:
        if not self.save(quiet=True):
            return
        run_with_progress(self, "Test connection", self.ctx.cloud_provider.check_status, pass_control=False, cancellable=False,
                          on_done=lambda st: (self.cloud_status.show_text(("✅ " if st.available else "⚠ ") + html.escape(st.message), warn=not st.available),
                                              self.refresh_usage()),
                          text="Sending a short test request…")

    def refresh_usage(self) -> None:
        rows = self.ctx.usage.recent(100)
        t = self.ctx.usage.totals()
        self.usage_totals.setText(f"{t['calls']} AI call(s), {t['ok']} successful · {t['cloud_calls']} to cloud ({t['cloud_chars']:,} characters sent)")
        self.usage.setRowCount(len(rows))
        for i, r in enumerate(rows):
            res = "ok" if r["success"] else f"failed ({r['error_kind']})"
            for j, v in enumerate([r["at"][:19].replace("T", " "), r["operation"], r["provider"], r["model"], res, str(r["chars_sent"])]):
                self.usage.setItem(i, j, QTableWidgetItem(v))

    def _clear_usage(self) -> None:
        self.ctx.usage.clear()
        self.refresh_usage()

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh_usage()
        super().showEvent(e)


class AppSettingsView(QWidget):
    theme_changed = Signal(str)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.addWidget(label("Application settings", "h1"))
        body = QWidget()
        lay = QGridLayout(body)
        outer.addWidget(_scroll(body), 1)
        a = ctx.settings.settings.app

        g = QGroupBox("Appearance & behaviour")
        f = QFormLayout(g)
        self.name = QLineEdit(a.user_name)
        self.theme = QComboBox()
        self.theme.addItem("Light", "light")
        self.theme.addItem("Dark", "dark")
        self.theme.setCurrentIndex(max(0, self.theme.findData(a.theme)))
        self.fullscreen = QCheckBox("Start exams in distraction-reduced full-screen mode")
        self.fullscreen.setChecked(a.fullscreen_exams)
        self.page_size = QSpinBox()
        self.page_size.setRange(20, 1000)
        self.page_size.setValue(a.page_size)
        f.addRow("Your name:", self.name)
        f.addRow("Theme:", self.theme)
        f.addRow(self.fullscreen)
        f.addRow("Rows per page:", self.page_size)
        f.addRow(label("Full-screen mode only reduces distractions. The application does not and cannot prevent "
                       "cheating, and it does not monitor you.", "muted"))
        lay.addWidget(g, 0, 0)

        g = QGroupBox("Programming questions")
        f = QFormLayout(g)
        self.code_exec = QCheckBox("Run test cases for programming answers (limited isolation)")
        self.code_exec.setChecked(a.code_execution_enabled)
        self.code_exec.toggled.connect(self._code_toggled)
        self.code_timeout = QSpinBox()
        self.code_timeout.setRange(1, 30)
        self.code_timeout.setSuffix(" s")
        self.code_timeout.setValue(a.code_execution_timeout_s)
        py = find_python_interpreter()
        f.addRow(self.code_exec)
        f.addRow("Time limit:", self.code_timeout)
        f.addRow(label(
            "Disabled by default. When enabled, answers run in a separate Python process (isolated mode, empty temp "
            "folder, no environment variables, time limit, restricted builtins and imports). This is NOT a full "
            "security sandbox – only enable it for code you wrote yourself. When disabled, code is checked statically "
            f"and graded by rubric (AI or manual). Interpreter: {py or 'not found'}", "muted"))
        lay.addWidget(g, 0, 1)

        g = QGroupBox("Your data")
        v = QVBoxLayout(g)
        v.addWidget(label(f"All data is stored locally in:<br><b>{html.escape(str(ctx.paths.root))}</b>", None))
        r1 = QHBoxLayout()
        r1.addWidget(button("Open data folder", slot=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.paths.root)))))
        r1.addWidget(button("Back up now…", slot=self._backup))
        r1.addWidget(button("Restore backup…", slot=self._restore))
        r1.addWidget(button("Export all data (JSON)…", slot=self._export_all))
        r1.addStretch()
        v.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(button("Delete exam history & learning profile…", "danger", slot=self._delete_history))
        r2.addStretch()
        v.addLayout(r2)
        v.addWidget(label("Backups contain your questions, exams, results and settings – never API keys.", "muted"))
        lay.addWidget(g, 1, 0)

        g = QGroupBox("Diagnostics & privacy")
        v = QVBoxLayout(g)
        self.diag = QCheckBox("Detailed diagnostic logging (takes effect after restart)")
        self.diag.setChecked(a.diagnostic_logging)
        v.addWidget(self.diag)
        v.addWidget(button("Open log folder", slot=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.paths.logs_dir)))))
        v.addWidget(label(
            "Logs never contain API keys (they are redacted automatically) or the content of your answers. "
            "Data is sent to a cloud provider only for AI operations you approve, and only the minimum needed "
            "(e.g. one question and your answer, or selected excerpts). Nothing is transmitted in the background.", "muted"))
        lay.addWidget(g, 1, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(button("Save settings", "primary", slot=self.save))
        outer.addLayout(bottom)

    def _code_toggled(self, on: bool) -> None:
        if on and not confirm(self, "Enable code execution?",
                              "Answers to programming questions will be executed on this computer with limited isolation.\n\n"
                              "Only enable this if you trust the code being run (e.g. your own answers). Continue?", yes="Enable"):
            self.code_exec.blockSignals(True)
            self.code_exec.setChecked(False)
            self.code_exec.blockSignals(False)

    def save(self) -> None:
        a = self.ctx.settings.settings.app
        a.user_name = self.name.text().strip()[:60] or "Student"
        a.theme = self.theme.currentData()
        a.fullscreen_exams = self.fullscreen.isChecked()
        a.page_size = self.page_size.value()
        a.code_execution_enabled = self.code_exec.isChecked()
        a.code_execution_timeout_s = self.code_timeout.value()
        a.diagnostic_logging = self.diag.isChecked()
        try:
            self.ctx.settings.save()
        except OSError as exc:
            show_error(self, "Settings", f"Could not save: {exc}")
            return
        self.theme_changed.emit(a.theme)
        show_info(self, "Settings", "Settings saved.")

    def _backup(self) -> None:
        default = self.ctx.paths.backups_dir / f"exam_simulator_backup_{datetime.now():%Y%m%d_%H%M}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Create backup", str(default), "Backup (*.zip)")
        if not path:
            return
        run_with_progress(self, "Backup", lambda: create_backup(self.ctx.db, self.ctx.settings, Path(path)), pass_control=False,
                          cancellable=False, on_done=lambda p: show_info(self, "Backup", f"Backup saved to {p}"), text="Creating backup…")

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore backup", str(self.ctx.paths.backups_dir), "Backup (*.zip)")
        if not path:
            return
        if not confirm(self, "Restore backup", "Restoring replaces ALL current data with the backup's content after the "
                                               "application restarts. A safety copy of the current database is kept in the backups folder. Continue?",
                       yes="Restore"):
            return
        try:
            created = stage_restore(Path(path), self.ctx.paths)
        except ExportError as exc:
            show_error(self, "Restore", str(exc))
            return
        show_info(self, "Restore", f"Backup from {created or 'unknown date'} is ready. Please close and restart the application to complete the restore.")

    def _export_all(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export all data", str(self.ctx.paths.exports_dir / "all_data.json"), "JSON (*.json)")
        if path:
            run_with_progress(self, "Export", lambda: export_all_data_json(self.ctx.db, Path(path)), pass_control=False,
                              cancellable=False, on_done=lambda p: show_info(self, "Export", f"Saved to {p}"), text="Exporting…")

    def _delete_history(self) -> None:
        text, ok = QInputDialog.getText(self, "Delete learning history",
                                        "This permanently deletes all exams, attempts, results and the learning profile "
                                        "(the question bank and study materials are kept).\n\nType DELETE to confirm:")
        if ok and text.strip() == "DELETE":
            self.ctx.delete_learning_history()
            show_info(self, "Deleted", "Exam history and learning profile were deleted.")

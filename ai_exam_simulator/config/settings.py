"""Application settings (non-secret) and data-directory layout.

Settings are stored as JSON in the per-user application data directory
(``%APPDATA%\\AIExamSimulator`` on Windows). API keys are *never* stored here;
see :mod:`ai_exam_simulator.security.secrets`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from .defaults import APP_DIR_NAME, CONFIG_FILENAME, DATA_DIR_ENV, DB_FILENAME, DEFAULT_OLLAMA_URL

log = logging.getLogger(__name__)


class RoutingMode(str, Enum):
    LOCAL_ONLY = "local_only"
    CLOUD_ONLY = "cloud_only"
    HYBRID_AUTO = "hybrid_auto"
    MANUAL = "manual"


class AIOperation(str, Enum):
    GENERATE_QUESTIONS = "generate_questions"
    GENERATE_FROM_SOURCE = "generate_from_source"
    VERIFY_QUESTIONS = "verify_questions"
    EXPLAIN = "explain"
    HINT = "hint"
    EVALUATE_WRITTEN = "evaluate_written"
    EVALUATE_CODE = "evaluate_code"
    ANALYZE_TOPICS = "analyze_topics"


OPERATION_LABELS: dict[AIOperation, str] = {
    AIOperation.GENERATE_QUESTIONS: "Question generation",
    AIOperation.GENERATE_FROM_SOURCE: "Question generation from study materials",
    AIOperation.VERIFY_QUESTIONS: "Second-pass question verification",
    AIOperation.EXPLAIN: "Explanations of mistakes",
    AIOperation.HINT: "Practice hints",
    AIOperation.EVALUATE_WRITTEN: "Grading written / essay answers",
    AIOperation.EVALUATE_CODE: "Grading programming answers",
    AIOperation.ANALYZE_TOPICS: "Topic detection in study materials",
}


class AISettings(BaseModel):
    routing_mode: RoutingMode = RoutingMode.LOCAL_ONLY
    manual_provider: str = "local"  # used when routing_mode == MANUAL: "local" | "cloud"

    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = ""
    ollama_timeout_s: int = Field(default=180, ge=10, le=1800)

    cloud_provider_name: str = "OpenAI"
    cloud_base_url: str = "https://api.openai.com/v1"
    cloud_model: str = ""
    cloud_timeout_s: int = Field(default=90, ge=10, le=600)
    cloud_json_mode: bool = True

    # Operations the user permits to run on the cloud provider at all.
    cloud_allowed_operations: list[AIOperation] = Field(
        default_factory=lambda: [
            AIOperation.GENERATE_QUESTIONS,
            AIOperation.VERIFY_QUESTIONS,
            AIOperation.EXPLAIN,
            AIOperation.HINT,
            AIOperation.EVALUATE_WRITTEN,
            AIOperation.EVALUATE_CODE,
        ]
    )
    allow_source_material_to_cloud: bool = False
    verify_generated_questions: bool = True
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float = Field(default=0.4, ge=0.0, le=1.5)


class AppPreferences(BaseModel):
    theme: str = "light"  # light | dark
    fullscreen_exams: bool = False
    code_execution_enabled: bool = False
    code_execution_timeout_s: int = Field(default=5, ge=1, le=30)
    diagnostic_logging: bool = False
    page_size: int = Field(default=100, ge=20, le=1000)
    user_name: str = "Student"

    @field_validator("theme")
    @classmethod
    def _theme(cls, v: str) -> str:
        return v if v in ("light", "dark") else "light"


class AppSettings(BaseModel):
    schema_version: int = 1
    ai: AISettings = Field(default_factory=AISettings)
    app: AppPreferences = Field(default_factory=AppPreferences)


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / DB_FILENAME

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def materials_dir(self) -> Path:
        return self.root / "materials"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def temp_dir(self) -> Path:
        return self.root / "temp"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def ensure(self) -> None:
        for d in (self.root, self.materials_dir, self.exports_dir, self.logs_dir, self.temp_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so an interrupted write never corrupts ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsManager:
    """Loads and saves :class:`AppSettings` to the config file."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.settings = self.load()

    def load(self) -> AppSettings:
        path = self.paths.config_path
        if not path.exists():
            return AppSettings()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Defensive: never accept a key smuggled into the config file.
            if isinstance(data, dict):
                data.get("ai", {}).pop("api_key", None)
            return AppSettings.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            backup = path.with_suffix(".corrupt.json")
            try:
                path.replace(backup)
            except OSError:
                pass
            log.error("Settings file was unreadable (%s); defaults restored, old file kept as %s", type(exc).__name__, backup.name)
            return AppSettings()

    def save(self) -> None:
        atomic_write_text(self.paths.config_path, self.settings.model_dump_json(indent=2))

    def export_dict(self) -> dict:
        """Settings suitable for export (contain no secrets by construction)."""
        return json.loads(self.settings.model_dump_json())

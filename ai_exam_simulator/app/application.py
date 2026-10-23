"""Service container: wires settings, storage, AI, grading and learning together.

Tests create an :class:`AppContext` with a temporary data directory and mock
providers; the UI receives the same object (explicit dependency injection).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from dataclasses import dataclass
from pathlib import Path

from ..ai.answer_evaluator import AnswerEvaluator
from ..ai.cloud_provider import API_KEY_SECRET_NAME, CloudProvider
from ..ai.hybrid_router import HybridRouter
from ..ai.ollama_provider import OllamaProvider
from ..ai.provider_interface import AIProvider
from ..ai.question_generator import QuestionGenerator
from ..config.settings import AppPaths, SettingsManager, default_data_dir
from ..documents.importer import DocumentImporter
from ..exams.exam_builder import ExamBuilder
from ..exams.exam_engine import ExamSession, SessionState
from ..exams.question_models import PRESET_NAMES, Question, preset_config
from ..exams.scoring import compute_summary
from ..grading.grading_service import GradingService
from ..grading.models import ExamSummary, QuestionResult
from ..learning.adaptive_engine import AdaptiveEngine
from ..learning.analytics import Analytics
from ..reports.export_service import apply_pending_restore
from ..security.redaction import RedactingFilter
from ..security.secrets import SecretStore
from ..storage.database import Database
from ..storage.repositories import (
    DocumentRepository,
    ExamRepository,
    ProfileRepository,
    QuestionRepository,
    TemplateRepository,
    UsageLogRepository,
)

log = logging.getLogger(__name__)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data"


def configure_logging(paths: AppPaths, diagnostic: bool) -> None:
    """Rotating file log + console, both passed through the secret-redaction filter."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_aies", False):
            root.removeHandler(h)
    root.setLevel(logging.DEBUG if diagnostic else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactingFilter()
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(redact)
        h._aies = True  # type: ignore[attr-defined]
        root.addHandler(h)
    # Never log HTTP request details (headers could include credentials).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class AppContext:
    paths: AppPaths
    settings: SettingsManager
    secrets: SecretStore
    db: Database
    questions: QuestionRepository
    templates: TemplateRepository
    exams: ExamRepository
    documents: DocumentRepository
    usage: UsageLogRepository
    profile: ProfileRepository
    local_provider: AIProvider
    cloud_provider: AIProvider
    router: HybridRouter
    generator: QuestionGenerator
    evaluator: AnswerEvaluator
    grading: GradingService
    analytics: Analytics
    adaptive: AdaptiveEngine
    builder: ExamBuilder
    importer: DocumentImporter
    restored_from_backup: bool = False

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(
        cls,
        data_dir: Path | None = None,
        *,
        secrets: SecretStore | None = None,
        local_provider: AIProvider | None = None,
        cloud_provider: AIProvider | None = None,
        setup_logging: bool = True,
        seed_samples: bool = True,
    ) -> "AppContext":
        paths = AppPaths(Path(data_dir) if data_dir else default_data_dir())
        paths.ensure()
        settings = SettingsManager(paths)
        if setup_logging:
            configure_logging(paths, settings.settings.app.diagnostic_logging)
        restored = apply_pending_restore(paths)
        secrets = secrets or SecretStore()
        db = Database(paths.db_path)
        usage = UsageLogRepository(db)

        ai = lambda: settings.settings.ai  # noqa: E731
        local = local_provider or OllamaProvider(lambda: ai().ollama_url, lambda: ai().ollama_model, lambda: ai().ollama_timeout_s)
        cloud = cloud_provider or CloudProvider(
            lambda: ai().cloud_base_url, lambda: ai().cloud_model, lambda: secrets.get(API_KEY_SECRET_NAME),
            name_getter=lambda: ai().cloud_provider_name, timeout_getter=lambda: ai().cloud_timeout_s,
            json_mode_getter=lambda: ai().cloud_json_mode,
        )
        router = HybridRouter(ai, local, cloud, usage_logger=usage)
        evaluator = AnswerEvaluator(router)
        grading = GradingService(
            evaluator,
            code_execution_enabled=lambda: settings.settings.app.code_execution_enabled,
            code_timeout_s=lambda: settings.settings.app.code_execution_timeout_s,
        )
        questions = QuestionRepository(db)
        exams = ExamRepository(db)
        documents = DocumentRepository(db)
        analytics = Analytics(db)
        adaptive = AdaptiveEngine()
        ctx = cls(
            paths=paths, settings=settings, secrets=secrets, db=db, questions=questions,
            templates=TemplateRepository(db), exams=exams, documents=documents, usage=usage,
            profile=ProfileRepository(db), local_provider=local, cloud_provider=cloud, router=router,
            generator=QuestionGenerator(router, ai), evaluator=evaluator, grading=grading,
            analytics=analytics, adaptive=adaptive,
            builder=ExamBuilder(questions, exams, documents, analytics, adaptive),
            importer=DocumentImporter(documents), restored_from_backup=restored,
        )
        if seed_samples:
            ctx.seed_samples()
        return ctx

    def seed_samples(self) -> None:
        """Install built-in templates and the sample question bank once."""
        if not self.templates.has_builtin():
            for name in PRESET_NAMES:
                self.templates.save(name, preset_config(name), builtin=True, template_id=f"builtin_{name.lower().replace(' ', '_')}")
        if self.profile.get("samples_seeded"):
            return
        sample = SAMPLE_DIR / "sample_questions.json"
        if sample.exists():
            from ..documents.question_bank_io import import_json

            res = import_json(sample, keep_ids=True)
            self.questions.save_many(res.questions)
            if res.errors:
                log.error("Sample bank had invalid entries: %s", res.errors[:3])
        self.profile.set("samples_seeded", True)

    # ------------------------------------------------------------------ exam lifecycle
    def persist_session(self, state: SessionState) -> None:
        self.exams.save_state(state)

    def start_exam(self, config, questions: list[Question], exam_id: str | None = None) -> ExamSession:
        eid = exam_id or self.exams.create_exam(config, questions)
        n = self.exams.attempt_count(eid)
        if config.attempts_allowed and n >= config.attempts_allowed:
            raise ValueError(f"The maximum number of attempts ({config.attempts_allowed}) for this exam has been reached.")
        return ExamSession.create(eid, config, questions, persist=self.persist_session, attempt_number=n + 1)

    def resume_session(self, attempt_id: str) -> ExamSession | None:
        state = self.exams.load_state(attempt_id)
        if state is None:
            return None
        session = ExamSession(state, persist=self.persist_session)
        if session.is_open:
            session.resume()
        return session

    def finalize(self, session: ExamSession, results: list[QuestionResult]) -> ExamSummary:
        summary = compute_summary(session.state.questions, results, session.config)
        self.exams.save_results(session.state, results, summary)
        return summary

    def update_result(self, state: SessionState, results: list[QuestionResult]) -> ExamSummary:
        summary = compute_summary(state.questions, results, state.config)
        self.exams.save_results(state, results, summary)
        return summary

    # ------------------------------------------------------------------ privacy controls
    def delete_learning_history(self) -> None:
        self.exams.delete_all_history()

    def export_settings_json(self) -> str:
        return json.dumps(self.settings.export_dict(), indent=2)

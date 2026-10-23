"""Assemble the question set for a new exam from the bank, adaptive plan and AI generation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..ai.question_generator import GenerationRequest, SourceExcerpt
from ..learning.adaptive_engine import AdaptiveEngine, AdaptivePlan
from ..learning.analytics import Analytics
from ..learning.weakness_detection import detect
from ..storage.repositories import DocumentRepository, ExamRepository, QuestionRepository
from ..documents.source_retrieval import build_chunks, select_excerpts
from .question_models import Difficulty, ExamConfig, ExamMode, Question


@dataclass
class BuildPlan:
    config: ExamConfig
    plan: AdaptivePlan
    from_bank: list[Question] = field(default_factory=list)
    missing: int = 0
    generation_requests: list[GenerationRequest] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ExamBuilder:
    def __init__(self, questions: QuestionRepository, exams: ExamRepository, documents: DocumentRepository,
                 analytics: Analytics, adaptive: AdaptiveEngine | None = None) -> None:
        self.questions = questions
        self.exams = exams
        self.documents = documents
        self.analytics = analytics
        self.adaptive = adaptive or AdaptiveEngine()

    def build_plan(self, config: ExamConfig, *, use_bank: bool = True, generate_missing: bool = True) -> BuildPlan:
        records = self.analytics.records(config.subject)
        report = detect(records)
        revision_ids = self.exams.incorrect_question_ids(config.subject) if config.mode == ExamMode.REVISION else None
        plan = self.adaptive.plan(config, report, records, revision_ids)
        bp = BuildPlan(config=config, plan=plan)

        if config.mode == ExamMode.REVISION:
            qs = self.questions.get_many(plan.revision_ids)
            qs = [q for q in qs if q.type in config.question_types] or qs
            bp.from_bank = qs[: config.num_questions]
            if len(bp.from_bank) < config.num_questions:
                bp.notes.append(f"Only {len(bp.from_bank)} previously-missed question(s) are available for revision.")
            return bp

        if use_bank and not config.source_document_ids:
            bank = self.questions.candidates(config.subject, plan.topics or None, [t.value for t in config.question_types])
            selected, unfilled = self.adaptive.select_from_bank(plan, bank, config.num_questions, config.question_types)
            bp.from_bank = selected
        else:
            unfilled = self.adaptive.allocate(plan, config.num_questions)
        bp.missing = len(unfilled)
        if bp.missing and generate_missing:
            bp.generation_requests = self._requests_for(config, plan, unfilled, bp.from_bank)
        return bp

    def _requests_for(self, config: ExamConfig, plan: AdaptivePlan, unfilled: list, existing: list[Question]) -> list[GenerationRequest]:
        groups: Counter[Difficulty] = Counter(d for _, d in unfilled)
        topics_for: dict[Difficulty, list[str]] = {}
        for t, d in unfilled:
            if t:
                topics_for.setdefault(d, [])
                if t not in topics_for[d]:
                    topics_for[d].append(t)
        excerpts = self.excerpts_for(config, plan.topics)
        reqs = []
        for diff, count in groups.items():
            types = sorted(config.question_types, key=lambda t: -plan.type_weights.get(t, 1.0))
            reqs.append(GenerationRequest(
                subject=config.subject, topics=topics_for.get(diff) or plan.topics or config.topics, difficulty=diff,
                count=count, types=types, language=config.language, avoid_texts=[q.text for q in existing],
                focus_notes=plan.focus_notes, excerpts=excerpts,
            ))
        return reqs

    def excerpts_for(self, config: ExamConfig, topics: list[str]) -> list[SourceExcerpt]:
        if not config.source_document_ids:
            return []
        chunks = []
        for doc_id in config.source_document_ids:
            doc = self.documents.get(doc_id)
            if not doc:
                continue
            chunks += build_chunks(doc, self.documents.sections(doc_id, included_only=True))
        query = " ".join(topics) + " " + config.subject
        picked = select_excerpts(chunks, query)
        return [SourceExcerpt(id=f"S{i + 1}", text=c.text, ref=c.ref) for i, c in enumerate(picked)]

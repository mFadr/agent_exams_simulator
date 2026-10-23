"""Core data structures: questions, answers and exam configuration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id(prefix: str = "q") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_SELECT = "multiple_select"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"
    FILL_BLANK = "fill_blank"
    NUMERICAL = "numerical"
    MATH_PROBLEM = "math_problem"
    PROGRAMMING = "programming"
    ESSAY = "essay"
    CASE_STUDY = "case_study"
    MATCHING = "matching"
    ORDERING = "ordering"


QUESTION_TYPE_LABELS: dict[QuestionType, str] = {
    QuestionType.SINGLE_CHOICE: "Single choice",
    QuestionType.MULTIPLE_SELECT: "Multiple select",
    QuestionType.TRUE_FALSE: "True / False",
    QuestionType.SHORT_ANSWER: "Short answer",
    QuestionType.FILL_BLANK: "Fill in the blank",
    QuestionType.NUMERICAL: "Numerical",
    QuestionType.MATH_PROBLEM: "Math problem",
    QuestionType.PROGRAMMING: "Programming",
    QuestionType.ESSAY: "Essay",
    QuestionType.CASE_STUDY: "Case study",
    QuestionType.MATCHING: "Matching",
    QuestionType.ORDERING: "Ordering",
}

OBJECTIVE_TYPES = {
    QuestionType.SINGLE_CHOICE,
    QuestionType.MULTIPLE_SELECT,
    QuestionType.TRUE_FALSE,
    QuestionType.MATCHING,
    QuestionType.ORDERING,
    QuestionType.FILL_BLANK,
}
OPEN_ENDED_TYPES = {QuestionType.ESSAY, QuestionType.CASE_STUDY}
CHOICE_TYPES = {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_SELECT, QuestionType.TRUE_FALSE}

BLANK_MARKER = "___"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXPERT = "expert"

    @property
    def level(self) -> int:
        return DIFFICULTY_ORDER.index(self)

    @classmethod
    def from_level(cls, level: int) -> "Difficulty":
        return DIFFICULTY_ORDER[max(0, min(len(DIFFICULTY_ORDER) - 1, level))]


DIFFICULTY_ORDER = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD, Difficulty.EXPERT]


class Provenance(str, Enum):
    USER = "user"
    AI_GENERATED = "ai_generated"
    SOURCE_DERIVED = "source_derived"
    SAMPLE = "sample"
    IMPORTED = "imported"


class ReviewStatus(str, Enum):
    UNVERIFIED = "unverified"  # passed schema/rule validation only
    AI_VERIFIED = "ai_verified"  # an independent second pass agreed with the key
    TOOL_VERIFIED = "tool_verified"  # checked by a deterministic tool (e.g. SymPy)
    FLAGGED = "flagged"  # potential problem detected - review before use
    USER_VERIFIED = "user_verified"  # a human reviewed/edited it


class Option(BaseModel):
    id: str
    text: str


class MatchPair(BaseModel):
    id: str
    left: str
    right: str


class NumericalAnswer(BaseModel):
    value: float
    abs_tolerance: float = 0.0
    rel_tolerance: float = 0.01  # 1 % default
    unit: str | None = None
    # Optional expression that should evaluate to ``value`` (used for tool verification).
    solution_expression: str | None = None


class CodeTestCase(BaseModel):
    call: str  # e.g. "add(2, 3)"
    expected: str  # Python literal, e.g. "5"
    hidden: bool = False


class RubricCriterion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("c"))
    description: str
    points: float = Field(gt=0)
    expected_concepts: list[str] = Field(default_factory=list)
    dimension: str = "correctness"  # correctness | reasoning | completeness | clarity | relevance


class Rubric(BaseModel):
    criteria: list[RubricCriterion] = Field(default_factory=list)
    acceptable_alternatives: list[str] = Field(default_factory=list)
    common_misconceptions: list[str] = Field(default_factory=list)
    partial_credit_notes: str = ""

    @property
    def total_points(self) -> float:
        return round(sum(c.points for c in self.criteria), 4)


class SourceRef(BaseModel):
    document_id: str
    document_name: str = ""
    section_id: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    excerpt: str = ""

    def label(self) -> str:
        parts = [self.document_name or self.document_id]
        if self.section_title:
            parts.append(self.section_title)
        if self.page_start:
            parts.append(f"p. {self.page_start}" + (f"-{self.page_end}" if self.page_end and self.page_end != self.page_start else ""))
        return " / ".join(parts)


class Question(BaseModel):
    """A single exam question including its grading reference."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=new_id)
    subject: str
    topic: str
    subtopic: str = ""
    text: str
    type: QuestionType
    difficulty: Difficulty = Difficulty.MEDIUM
    max_points: float = Field(default=1.0, gt=0, le=100)

    options: list[Option] = Field(default_factory=list)
    correct_option_ids: list[str] = Field(default_factory=list)
    accepted_answers: list[str] = Field(default_factory=list)  # short answer
    blank_answers: list[list[str]] = Field(default_factory=list)  # fill in the blank
    case_sensitive: bool = False
    numerical: NumericalAnswer | None = None
    expected_expression: str | None = None  # math problem final answer (symbolic)
    match_pairs: list[MatchPair] = Field(default_factory=list)
    order_items: list[str] = Field(default_factory=list)  # correct order
    code_context: str = ""  # code snippet shown with the question
    language: str = "python"
    function_name: str | None = None
    test_cases: list[CodeTestCase] = Field(default_factory=list)
    rubric: Rubric | None = None
    model_answer: str = ""

    explanation: str = ""
    hint: str = ""
    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_ref: SourceRef | None = None
    provenance: Provenance = Provenance.USER
    review_status: ReviewStatus = ReviewStatus.UNVERIFIED
    validation_notes: list[str] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("text", "subject", "topic")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def correct_answer_display(self) -> str:
        """Human-readable correct answer / grading reference."""
        t = self.type
        if t in CHOICE_TYPES:
            ids = set(self.correct_option_ids)
            return "; ".join(o.text for o in self.options if o.id in ids) or "(no key)"
        if t == QuestionType.SHORT_ANSWER:
            return " | ".join(self.accepted_answers) or self.model_answer or "(rubric-graded)"
        if t == QuestionType.FILL_BLANK:
            return "; ".join(f"[{i + 1}] " + " / ".join(a) for i, a in enumerate(self.blank_answers))
        if t == QuestionType.NUMERICAL and self.numerical:
            n = self.numerical
            tol = []
            if n.abs_tolerance:
                tol.append(f"±{n.abs_tolerance:g}")
            if n.rel_tolerance:
                tol.append(f"±{n.rel_tolerance * 100:g}%")
            return f"{n.value:g}{' ' + n.unit if n.unit else ''}" + (f" ({', '.join(tol)})" if tol else "")
        if t == QuestionType.MATH_PROBLEM:
            return self.expected_expression or self.model_answer or "(rubric-graded)"
        if t == QuestionType.MATCHING:
            return "; ".join(f"{p.left} → {p.right}" for p in self.match_pairs)
        if t == QuestionType.ORDERING:
            return " → ".join(self.order_items)
        if t == QuestionType.PROGRAMMING:
            return self.model_answer or f"Must pass {len(self.test_cases)} test case(s)"
        return self.model_answer or "(see rubric)"

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnswerRecord(BaseModel):
    """A student's answer to one question, as stored in an exam session."""

    question_id: str
    value: Any = None
    flagged: bool = False
    confidence: int | None = Field(default=None, ge=1, le=5)
    time_spent_s: float = 0.0
    updated_at: datetime | None = None
    hints_used: int = 0
    checked_in_practice: bool = False

    def is_answered(self) -> bool:
        v = self.value
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, (list, tuple)):
            return any((str(x).strip() if x is not None else "") for x in v)
        if isinstance(v, dict):
            return any(bool(x) for x in v.values())
        return True


# --------------------------------------------------------------------------- exam config


class ExamMode(str, Enum):
    FIXED = "fixed"
    ADAPTIVE = "adaptive"
    WEAKNESS = "weakness"
    REVISION = "revision"
    MIXED_MOCK = "mixed_mock"
    PRACTICE = "practice"


EXAM_MODE_LABELS = {
    ExamMode.FIXED: "Fixed exam",
    ExamMode.ADAPTIVE: "Adaptive exam",
    ExamMode.WEAKNESS: "Weakness practice",
    ExamMode.REVISION: "Revision exam",
    ExamMode.MIXED_MOCK: "Mixed mock exam",
    ExamMode.PRACTICE: "Practice mode (learning)",
}


class FeedbackMode(str, Enum):
    AFTER_SUBMISSION = "after_submission"
    IMMEDIATE = "immediate"


class MultiSelectScoring(str, Enum):
    ALL_OR_NOTHING = "all_or_nothing"
    PARTIAL = "partial"  # (correct picked - wrong picked) / total correct, floored at 0


class ScoringRules(BaseModel):
    negative_marking: bool = False
    negative_fraction: float = Field(default=0.25, ge=0, le=1)  # of max points, per wrong objective answer
    partial_credit: bool = True
    multi_select: MultiSelectScoring = MultiSelectScoring.PARTIAL
    floor_total_at_zero: bool = True


class PermittedTools(BaseModel):
    calculator: bool = False
    notes: bool = False
    formula_sheet: bool = False


class ExamConfig(BaseModel):
    title: str = "Untitled exam"
    subject: str = "Mathematics"
    topics: list[str] = Field(default_factory=list)
    num_questions: int = Field(default=10, ge=1, le=200)
    difficulty: Difficulty = Difficulty.MEDIUM
    duration_minutes: int = Field(default=20, ge=0, le=600)  # 0 = untimed
    scoring: ScoringRules = Field(default_factory=ScoringRules)
    pass_threshold: float = Field(default=50.0, ge=0, le=100)
    question_types: list[QuestionType] = Field(default_factory=lambda: [QuestionType.SINGLE_CHOICE])
    language: str = "English"
    attempts_allowed: int = Field(default=0, ge=0, le=100)  # 0 = unlimited
    shuffle_questions: bool = True
    shuffle_options: bool = True
    allow_revisit: bool = True
    allow_unanswered: bool = True
    tools: PermittedTools = Field(default_factory=PermittedTools)
    formula_sheet_text: str = ""
    feedback: FeedbackMode = FeedbackMode.AFTER_SUBMISSION
    mode: ExamMode = ExamMode.FIXED
    # adaptive limits
    min_difficulty: Difficulty = Difficulty.EASY
    max_difficulty: Difficulty = Difficulty.EXPERT
    source_document_ids: list[str] = Field(default_factory=list)
    instructions: str = ""

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Exam title must not be empty")
        return v[:200]

    @field_validator("question_types")
    @classmethod
    def _types(cls, v: list[QuestionType]) -> list[QuestionType]:
        if not v:
            raise ValueError("Select at least one question type")
        return list(dict.fromkeys(v))

    @property
    def is_practice(self) -> bool:
        return self.mode == ExamMode.PRACTICE

    @property
    def shows_immediate_feedback(self) -> bool:
        return self.is_practice or self.feedback == FeedbackMode.IMMEDIATE


def preset_config(name: str) -> ExamConfig:
    """Built-in presets: Quick Practice, Standard Exam, Difficult Exam, Mock Final, Custom."""
    QT = QuestionType
    presets: dict[str, dict[str, Any]] = {
        "Quick Practice": dict(
            title="Quick practice", num_questions=5, duration_minutes=0, difficulty=Difficulty.EASY,
            question_types=[QT.SINGLE_CHOICE, QT.TRUE_FALSE], mode=ExamMode.PRACTICE,
            feedback=FeedbackMode.IMMEDIATE, pass_threshold=50,
        ),
        "Standard Exam": dict(
            title="Standard exam", num_questions=15, duration_minutes=30, difficulty=Difficulty.MEDIUM,
            question_types=[QT.SINGLE_CHOICE, QT.MULTIPLE_SELECT, QT.TRUE_FALSE, QT.SHORT_ANSWER, QT.NUMERICAL],
            pass_threshold=60,
        ),
        "Difficult Exam": dict(
            title="Difficult exam", num_questions=15, duration_minutes=45, difficulty=Difficulty.HARD,
            question_types=[QT.SINGLE_CHOICE, QT.MULTIPLE_SELECT, QT.NUMERICAL, QT.SHORT_ANSWER, QT.ESSAY],
            scoring=ScoringRules(negative_marking=True, negative_fraction=0.25), pass_threshold=65,
        ),
        "Mock Final": dict(
            title="Mock final", num_questions=30, duration_minutes=90, difficulty=Difficulty.HARD,
            question_types=list(QT), mode=ExamMode.MIXED_MOCK, allow_unanswered=True, pass_threshold=60,
            attempts_allowed=1,
        ),
        "Custom Exam": dict(title="Custom exam"),
    }
    if name not in presets:
        raise KeyError(name)
    return ExamConfig(**presets[name])


PRESET_NAMES = ["Quick Practice", "Standard Exam", "Difficult Exam", "Mock Final", "Custom Exam"]

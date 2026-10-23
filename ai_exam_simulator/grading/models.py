"""Structured grading results."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ResultStatus(str, Enum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"
    UNANSWERED = "unanswered"
    PENDING_REVIEW = "pending_review"  # could not be graded automatically


class GradingMethod(str, Enum):
    DETERMINISTIC = "deterministic"
    NUMERICAL = "numerical"
    SYMBOLIC = "symbolic"
    CODE_TESTS = "code_tests"
    STATIC_CODE = "static_code"
    AI_RUBRIC = "ai_rubric"
    MANUAL = "manual"
    NONE = "none"


METHOD_LABELS = {
    GradingMethod.DETERMINISTIC: "Answer key (deterministic)",
    GradingMethod.NUMERICAL: "Numerical comparison with tolerance",
    GradingMethod.SYMBOLIC: "Symbolic equivalence (SymPy)",
    GradingMethod.CODE_TESTS: "Executed test cases",
    GradingMethod.STATIC_CODE: "Static code analysis",
    GradingMethod.AI_RUBRIC: "AI rubric evaluation (may be imperfect)",
    GradingMethod.MANUAL: "Manual grading / override",
    GradingMethod.NONE: "Not graded",
}


class CriterionScore(BaseModel):
    criterion_id: str
    description: str
    awarded: float
    max_points: float
    satisfied: bool | None = None
    comment: str = ""


class QuestionResult(BaseModel):
    question_id: str
    points: float
    max_points: float
    status: ResultStatus
    method: GradingMethod
    feedback: str = ""
    criteria: list[CriterionScore] = Field(default_factory=list)
    error_tags: list[str] = Field(default_factory=list)
    misconception: str = ""
    recommendation: str = ""
    confidence: float | None = None  # 0..1, only for AI-evaluated answers
    needs_review: bool = False
    provider: str = ""  # which AI provider evaluated (if any)
    overridden: bool = False
    override_note: str = ""
    ai_explanation: str = ""  # AI-generated tutoring text; never affects the score

    @property
    def fraction(self) -> float:
        return self.points / self.max_points if self.max_points else 0.0


def status_for(points: float, max_points: float) -> ResultStatus:
    if points >= max_points - 1e-9:
        return ResultStatus.CORRECT
    if points > 1e-9:
        return ResultStatus.PARTIAL
    return ResultStatus.INCORRECT


class TopicBreakdown(BaseModel):
    key: str
    points: float = 0.0
    max_points: float = 0.0
    count: int = 0
    correct: int = 0

    @property
    def percentage(self) -> float:
        return 100.0 * self.points / self.max_points if self.max_points else 0.0


class ExamSummary(BaseModel):
    points: float
    max_points: float
    percentage: float
    passed: bool | None
    pass_threshold: float | None
    correct: int
    incorrect: int
    partial: int
    unanswered: int
    pending_review: int
    by_topic: list[TopicBreakdown] = Field(default_factory=list)
    by_type: list[TopicBreakdown] = Field(default_factory=list)
    by_difficulty: list[TopicBreakdown] = Field(default_factory=list)
    provisional: bool = False  # True while some answers await review

"""Build a structured, inspectable exam report.

Calculated values (scores, counts, breakdowns) and AI-generated text are kept
in separate fields so every output can label them distinctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..exams.exam_engine import SUBMIT_REASON_TEXT, SessionState
from ..exams.question_models import QUESTION_TYPE_LABELS, Question, QuestionType
from ..grading.models import METHOD_LABELS, ExamSummary, GradingMethod, QuestionResult, ResultStatus


def format_answer(q: Question, value, state: SessionState | None = None) -> str:
    """Human-readable student answer."""
    if value is None or value == "" or value == [] or value == {}:
        return "(no answer)"
    t = q.type
    if t in (QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE):
        return next((o.text for o in q.options if o.id == value), str(value))
    if t == QuestionType.MULTIPLE_SELECT:
        chosen = set(value or [])
        return "; ".join(o.text for o in q.options if o.id in chosen) or "(none selected)"
    if t == QuestionType.FILL_BLANK:
        return "; ".join(f"[{i + 1}] {v or '—'}" for i, v in enumerate(value or []))
    if t == QuestionType.MATCHING:
        return "; ".join(f"{p.left} → {(value or {}).get(p.id) or '—'}" for p in q.match_pairs)
    if t == QuestionType.ORDERING:
        return " → ".join(str(v) for v in (value or []))
    return str(value)


@dataclass
class QuestionReportRow:
    number: int
    question: Question
    answer_text: str
    result: QuestionResult
    flagged: bool
    time_spent_s: float

    @property
    def method_label(self) -> str:
        return METHOD_LABELS.get(self.result.method, self.result.method.value)

    @property
    def is_ai_graded(self) -> bool:
        return self.result.method == GradingMethod.AI_RUBRIC


@dataclass
class ExamReport:
    title: str
    subject: str
    mode: str
    attempt_number: int
    started_at: str
    submitted_at: str
    submit_reason: str
    summary: ExamSummary
    rows: list[QuestionReportRow] = field(default_factory=list)
    ai_interpretation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def filtered(self, kind: str) -> list[QuestionReportRow]:
        if kind == "incorrect":
            return [r for r in self.rows if r.result.status in (ResultStatus.INCORRECT, ResultStatus.PARTIAL)]
        if kind == "unanswered":
            return [r for r in self.rows if r.result.status == ResultStatus.UNANSWERED]
        if kind == "flagged":
            return [r for r in self.rows if r.flagged]
        if kind == "review":
            return [r for r in self.rows if r.result.needs_review or r.result.status == ResultStatus.PENDING_REVIEW]
        return list(self.rows)


def build_report(state: SessionState, summary: ExamSummary, results: list[QuestionResult]) -> ExamReport:
    by_id = {r.question_id: r for r in results}
    qmap = {q.id: q for q in state.questions}
    rows = []
    for i, qid in enumerate(state.order, start=1):
        q = qmap[qid]
        ans = state.answers.get(qid)
        r = by_id.get(qid)
        if r is None:
            continue
        rows.append(QuestionReportRow(i, q, format_answer(q, ans.value if ans else None, state), r,
                                      bool(ans and ans.flagged), ans.time_spent_s if ans else 0.0))
    rep = ExamReport(
        title=state.config.title, subject=state.config.subject, mode=state.config.mode.value,
        attempt_number=state.attempt_number, started_at=state.started_at.strftime("%Y-%m-%d %H:%M UTC"),
        submitted_at=state.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if state.submitted_at else "—",
        submit_reason=SUBMIT_REASON_TEXT.get(state.submit_reason, "") if state.submit_reason else "",
        summary=summary, rows=rows,
    )
    if summary.provisional:
        rep.notes.append(f"{summary.pending_review} answer(s) still await review; the score is provisional.")
    ai_rows = [r for r in rows if r.is_ai_graded]
    if ai_rows:
        rep.notes.append(
            f"{len(ai_rows)} answer(s) were evaluated by an AI model against a rubric. AI evaluation can be imperfect - "
            "inspect the criteria and override the points if you disagree."
        )
    for r in rows:
        if r.result.ai_explanation:
            rep.ai_interpretation.append(f"Q{r.number}: {r.result.ai_explanation}")
    return rep


def type_label(value: str) -> str:
    try:
        return QUESTION_TYPE_LABELS[QuestionType(value)]
    except ValueError:
        return value

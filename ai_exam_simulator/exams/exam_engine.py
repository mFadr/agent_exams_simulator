"""Exam session state machine.

The session state is persisted after every change via the ``persist`` callback
(SQLite in the app). The deadline is an absolute UTC timestamp stored with the
session, so closing and reopening the app never grants extra time.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from .question_models import AnswerRecord, ExamConfig, Question, QuestionType, new_id, utcnow

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


class SessionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    GRADED = "graded"


class SubmitReason(str, Enum):
    USER = "user"
    TIME_EXPIRED = "time_expired"
    EXPIRED_WHILE_CLOSED = "expired_while_closed"


SUBMIT_REASON_TEXT = {
    SubmitReason.USER: "Submitted by the student.",
    SubmitReason.TIME_EXPIRED: "Time expired - the exam was submitted automatically.",
    SubmitReason.EXPIRED_WHILE_CLOSED: "The time limit passed while the application was closed; the answers saved "
                                       "before that point were submitted automatically.",
}


class SessionError(Exception):
    """User-facing error about an invalid session action."""


class SessionState(BaseModel):
    attempt_id: str = Field(default_factory=lambda: new_id("att"))
    exam_id: str
    attempt_number: int = 1
    config: ExamConfig
    questions: list[Question]  # frozen snapshots: later bank edits do not alter this attempt
    order: list[str]
    option_order: dict[str, list[str]] = Field(default_factory=dict)
    display_order: dict[str, list[str]] = Field(default_factory=dict)  # matching rights / ordering start order
    answers: dict[str, AnswerRecord] = Field(default_factory=dict)
    current_index: int = 0
    furthest_index: int = 0
    status: SessionStatus = SessionStatus.IN_PROGRESS
    started_at: datetime
    deadline: datetime | None = None
    submitted_at: datetime | None = None
    submit_reason: SubmitReason | None = None
    active_since: datetime | None = None
    save_failures: int = 0


class ExamSession:
    def __init__(
        self,
        state: SessionState,
        persist: Callable[[SessionState], None] | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.state = state
        self._persist = persist
        self._clock = clock
        self.last_save_error: str | None = None
        self._by_id = {q.id: q for q in state.questions}

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(
        cls,
        exam_id: str,
        config: ExamConfig,
        questions: list[Question],
        *,
        persist: Callable[[SessionState], None] | None = None,
        clock: Clock = utcnow,
        rng: random.Random | None = None,
        attempt_number: int = 1,
    ) -> "ExamSession":
        if not questions:
            raise SessionError("An exam needs at least one question.")
        rng = rng or random.Random()
        snaps = [q.model_copy(deep=True) for q in questions]
        order = [q.id for q in snaps]
        if config.shuffle_questions:
            rng.shuffle(order)
        option_order: dict[str, list[str]] = {}
        display: dict[str, list[str]] = {}
        for q in snaps:
            ids = [o.id for o in q.options]
            if config.shuffle_options and q.type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_SELECT):
                rng.shuffle(ids)
            option_order[q.id] = ids
            if q.type == QuestionType.MATCHING:
                rights = [p.right for p in q.match_pairs]
                rng.shuffle(rights)
                display[q.id] = rights
            elif q.type == QuestionType.ORDERING:
                items = list(q.order_items)
                for _ in range(10):  # never present the solved order
                    rng.shuffle(items)
                    if items != q.order_items:
                        break
                display[q.id] = items
        now = clock()
        deadline = now + timedelta(minutes=config.duration_minutes) if config.duration_minutes > 0 else None
        state = SessionState(
            exam_id=exam_id, attempt_number=attempt_number, config=config, questions=snaps, order=order,
            option_order=option_order, display_order=display,
            answers={q.id: AnswerRecord(question_id=q.id) for q in snaps},
            started_at=now, deadline=deadline, active_since=now,
        )
        session = cls(state, persist, clock)
        session._save()
        return session

    # ------------------------------------------------------------------ properties
    @property
    def config(self) -> ExamConfig:
        return self.state.config

    @property
    def is_open(self) -> bool:
        return self.state.status == SessionStatus.IN_PROGRESS

    @property
    def total(self) -> int:
        return len(self.state.order)

    @property
    def current_index(self) -> int:
        return self.state.current_index

    def question_at(self, index: int) -> Question:
        return self._by_id[self.state.order[index]]

    @property
    def current_question(self) -> Question:
        return self.question_at(self.state.current_index)

    def ordered_questions(self) -> list[Question]:
        return [self._by_id[qid] for qid in self.state.order]

    def answer(self, qid: str) -> AnswerRecord:
        return self.state.answers[qid]

    def answered_count(self) -> int:
        return sum(1 for a in self.state.answers.values() if a.is_answered())

    def unanswered_indices(self) -> list[int]:
        return [i for i, qid in enumerate(self.state.order) if not self.state.answers[qid].is_answered()]

    def flagged_indices(self) -> list[int]:
        return [i for i, qid in enumerate(self.state.order) if self.state.answers[qid].flagged]

    def remaining_seconds(self) -> float | None:
        if self.state.deadline is None:
            return None
        return max(0.0, (self.state.deadline - self._clock()).total_seconds())

    def is_expired(self) -> bool:
        rem = self.remaining_seconds()
        return rem is not None and rem <= 0

    # ------------------------------------------------------------------ actions
    def _require_open(self) -> None:
        if self.check_expiry():
            raise SessionError("Time is up - the exam has been submitted.")
        if not self.is_open:
            raise SessionError("This exam has already been submitted.")

    def can_go_to(self, index: int) -> bool:
        if not 0 <= index < self.total:
            return False
        if self.config.allow_revisit:
            return True
        return index >= self.state.current_index

    def go_to(self, index: int) -> None:
        self._require_open()
        if not self.can_go_to(index):
            raise SessionError("Returning to earlier questions is not allowed in this exam.")
        self._account_time()
        self.state.current_index = index
        self.state.furthest_index = max(self.state.furthest_index, index)
        self._save()

    def next(self) -> None:
        if self.state.current_index + 1 < self.total:
            self.go_to(self.state.current_index + 1)

    def previous(self) -> None:
        if self.state.current_index > 0:
            self.go_to(self.state.current_index - 1)

    def set_answer(self, qid: str, value: Any) -> None:
        self._require_open()
        if qid not in self.state.answers:
            raise SessionError("Unknown question.")
        rec = self.state.answers[qid]
        if rec.checked_in_practice and not self.config.is_practice:
            raise SessionError("This answer is locked because feedback has already been shown.")
        rec.value = value
        rec.updated_at = self._clock()
        self._save()

    def toggle_flag(self, qid: str) -> bool:
        self._require_open()
        rec = self.state.answers[qid]
        rec.flagged = not rec.flagged
        self._save()
        return rec.flagged

    def set_confidence(self, qid: str, confidence: int | None) -> None:
        self._require_open()
        self.state.answers[qid].confidence = confidence
        self._save()

    def mark_checked(self, qid: str) -> None:
        """Record that immediate feedback was shown (locks the answer outside practice mode)."""
        self.state.answers[qid].checked_in_practice = True
        self._save()

    def note_hint(self, qid: str) -> None:
        self.state.answers[qid].hints_used += 1
        self._save()

    def insert_question(self, q: Question, after_index: int) -> int:
        """Practice mode only: add an extra (e.g. similar) question after ``after_index``."""
        self._require_open()
        if not self.config.is_practice:
            raise SessionError("Questions can only be added during practice.")
        snap = q.model_copy(deep=True)
        self.state.questions.append(snap)
        self._by_id[snap.id] = snap
        self.state.order.insert(after_index + 1, snap.id)
        self.state.option_order[snap.id] = [o.id for o in snap.options]
        if snap.type == QuestionType.ORDERING:
            items = list(snap.order_items)
            random.shuffle(items)
            self.state.display_order[snap.id] = items
        elif snap.type == QuestionType.MATCHING:
            rights = [p.right for p in snap.match_pairs]
            random.shuffle(rights)
            self.state.display_order[snap.id] = rights
        self.state.answers[snap.id] = AnswerRecord(question_id=snap.id)
        self._save()
        return after_index + 1

    def reset_check(self, qid: str) -> None:
        """Practice mode: allow another try after feedback was shown."""
        if self.config.is_practice:
            self.state.answers[qid].checked_in_practice = False
            self._save()

    def can_submit(self) -> tuple[bool, str]:
        if not self.is_open:
            return False, "Already submitted."
        missing = self.unanswered_indices()
        if missing and not self.config.allow_unanswered and not self.is_expired():
            nums = ", ".join(str(i + 1) for i in missing[:10])
            return False, f"All questions must be answered before submitting. Unanswered: {nums}{'…' if len(missing) > 10 else ''}"
        return True, ""

    def submit(self, reason: SubmitReason = SubmitReason.USER) -> None:
        if not self.is_open:
            return
        if reason == SubmitReason.USER:
            ok, msg = self.can_submit()
            if not ok:
                raise SessionError(msg)
        self._account_time()
        self.state.status = SessionStatus.SUBMITTED
        now = self._clock()
        self.state.submitted_at = min(now, self.state.deadline) if self.state.deadline and reason != SubmitReason.USER else now
        self.state.submit_reason = reason
        self._save()

    def check_expiry(self, *, reopened: bool = False) -> bool:
        """Submit automatically if the deadline has passed. Returns True if it did."""
        if self.is_open and self.is_expired():
            self.submit(SubmitReason.EXPIRED_WHILE_CLOSED if reopened else SubmitReason.TIME_EXPIRED)
            return True
        return False

    def mark_graded(self) -> None:
        self.state.status = SessionStatus.GRADED
        self._save()

    # ------------------------------------------------------------------ internals
    def _account_time(self) -> None:
        now = self._clock()
        if self.state.active_since is not None and self.is_open:
            end = min(now, self.state.deadline) if self.state.deadline else now
            elapsed = max(0.0, (end - self.state.active_since).total_seconds())
            # ignore gaps longer than 30 min (app closed / computer asleep)
            if elapsed < 1800:
                qid = self.state.order[self.state.current_index]
                self.state.answers[qid].time_spent_s += elapsed
        self.state.active_since = now

    def resume(self) -> None:
        """Call after reopening a session: restarts per-question timing, auto-submits if expired."""
        self.state.active_since = self._clock()
        self.check_expiry(reopened=True)
        self._save()

    def save_now(self) -> bool:
        return self._save()

    def _save(self) -> bool:
        if self._persist is None:
            return True
        try:
            self._persist(self.state)
        except Exception as exc:  # disk full, locked DB, ...
            self.state.save_failures += 1
            self.last_save_error = f"Answers could not be saved ({type(exc).__name__}: {exc}). They are kept in memory and saving will be retried."
            log.error("Session autosave failed: %s", type(exc).__name__)
            return False
        self.last_save_error = None
        return True


def ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

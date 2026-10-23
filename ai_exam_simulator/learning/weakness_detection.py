"""Weakness detection across completed exams.

Conclusions are deliberately conservative: a pattern is only reported as
"recurring" when it appears in several attempts, and every insight states the
amount of evidence behind it. Small samples are labelled as tentative.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ..exams.question_models import QUESTION_TYPE_LABELS, QuestionType
from .analytics import ResultRecord

MIN_EVIDENCE = 3
STRONG_EVIDENCE = 6

CHOICE = {"single_choice", "multiple_select", "true_false", "matching", "ordering"}
PRODUCED = {"short_answer", "essay", "case_study", "fill_blank"}
CALC = {"numerical", "math_problem"}

TAG_GROUPS = {
    "units": ({"missing_unit", "wrong_unit"}, "units (missing or incompatible units)"),
    "signs": ({"sign_error"}, "signs (correct magnitude, wrong sign)"),
    "magnitude": ({"order_of_magnitude"}, "orders of magnitude / unit conversions"),
    "precision": ({"rounding_or_precision"}, "rounding and precision"),
    "edge_cases": ({"failed_tests", "runtime_error"}, "edge cases and runtime errors in code"),
    "definitions": ({"factual", "conceptual"}, "definitions and core concepts"),
    "incomplete": ({"incomplete"}, "incomplete written answers"),
    "reasoning": ({"reasoning"}, "reasoning steps in written answers"),
}


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class TopicStat:
    subject: str
    topic: str
    n: int = 0
    attempts: int = 0
    accuracy: float = 0.0
    low: float = 0.0
    high: float = 1.0
    wrong_attempts: int = 0
    recent_accuracy: float | None = None
    trend: float | None = None
    status: str = "insufficient_data"

    @property
    def label(self) -> str:
        return {
            "insufficient_data": "Not enough data",
            "recurring_weakness": "Recurring weakness",
            "one_time_mistake": "One-time mistake",
            "developing": "Developing",
            "mastered": "Mastered",
        }[self.status]


@dataclass
class Insight:
    kind: str
    text: str
    evidence: str
    strength: str  # "strong" | "tentative"


@dataclass
class WeaknessReport:
    topics: list[TopicStat] = field(default_factory=list)
    type_accuracy: dict[str, tuple[float, int]] = field(default_factory=dict)
    difficulty_accuracy: dict[str, tuple[float, int]] = field(default_factory=dict)
    insights: list[Insight] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    total_records: int = 0

    def weak_topics(self) -> list[TopicStat]:
        return [t for t in self.topics if t.status in ("recurring_weakness", "developing") and t.accuracy < 0.7]

    def mastered_topics(self) -> list[TopicStat]:
        return [t for t in self.topics if t.status == "mastered"]


def _acc(recs: list[ResultRecord]) -> float:
    return sum(r.fraction for r in recs) / len(recs) if recs else 0.0


def classify_topic(recs: list[ResultRecord]) -> TopicStat:
    s = TopicStat(subject=recs[0].subject, topic=recs[0].topic)
    s.n = len(recs)
    s.attempts = len({r.attempt_id for r in recs})
    s.accuracy = _acc(recs)
    s.low, s.high = wilson_interval(sum(r.fraction for r in recs), s.n)
    s.wrong_attempts = len({r.attempt_id for r in recs if r.is_wrong})
    wrong = sum(1 for r in recs if r.is_wrong)
    if s.n >= 4:
        half = s.n // 2
        s.recent_accuracy = _acc(recs[half:])
        s.trend = s.recent_accuracy - _acc(recs[:half])
    if s.n < MIN_EVIDENCE:
        s.status = "insufficient_data"
    elif s.accuracy >= 0.8 and s.n >= 5 and s.low >= 0.55:
        s.status = "mastered"
    elif s.wrong_attempts >= 2 and s.accuracy < 0.6:
        s.status = "recurring_weakness"
    elif wrong == 1:
        s.status = "one_time_mistake"
    else:
        s.status = "developing"
    return s


def _strength(n: int) -> str:
    return "strong" if n >= STRONG_EVIDENCE else "tentative"


def detect(records: list[ResultRecord]) -> WeaknessReport:
    rep = WeaknessReport(total_records=len(records))
    if not records:
        rep.recommendations.append("Complete a few exams to build a learning profile.")
        return rep

    by_topic: dict[tuple[str, str], list[ResultRecord]] = defaultdict(list)
    by_type: dict[str, list[ResultRecord]] = defaultdict(list)
    by_diff: dict[str, list[ResultRecord]] = defaultdict(list)
    for r in records:
        by_topic[(r.subject, r.topic)].append(r)
        by_type[r.qtype].append(r)
        by_diff[r.difficulty].append(r)
    rep.topics = sorted((classify_topic(v) for v in by_topic.values()), key=lambda t: (t.accuracy, -t.n))
    rep.type_accuracy = {k: (_acc(v), len(v)) for k, v in by_type.items()}
    rep.difficulty_accuracy = {k: (_acc(v), len(v)) for k, v in by_diff.items()}

    # --- pattern: recognises in multiple choice but cannot produce the answer
    for (subject, topic), recs in by_topic.items():
        ch = [r for r in recs if r.qtype in CHOICE]
        pr = [r for r in recs if r.qtype in PRODUCED]
        if len(ch) >= MIN_EVIDENCE and len(pr) >= MIN_EVIDENCE and _acc(ch) - _acc(pr) >= 0.3:
            rep.insights.append(Insight(
                "recognition_vs_recall",
                f"In '{topic}' you recognise correct answers in choice questions ({_acc(ch):.0%}) but struggle to "
                f"explain or produce them independently ({_acc(pr):.0%}).",
                f"{len(ch)} choice and {len(pr)} written answers", _strength(min(len(ch), len(pr))),
            ))

    # --- pattern: multi-step calculations vs concepts
    by_subject: dict[str, list[ResultRecord]] = defaultdict(list)
    for r in records:
        by_subject[r.subject].append(r)
    for subject, recs in by_subject.items():
        concept = [r for r in recs if r.qtype in CHOICE]
        calc = [r for r in recs if r.qtype in CALC]
        if len(concept) >= MIN_EVIDENCE and len(calc) >= MIN_EVIDENCE and _acc(concept) - _acc(calc) >= 0.3:
            rep.insights.append(Insight(
                "calculation_gap",
                f"In {subject} you understand the concepts ({_acc(concept):.0%} on conceptual questions) but lose "
                f"accuracy on calculations and multi-step problems ({_acc(calc):.0%}).",
                f"{len(concept)} conceptual and {len(calc)} calculation answers", _strength(min(len(concept), len(calc))),
            ))

    # --- pattern: accuracy drops with difficulty
    easy = by_diff.get("easy", []) + by_diff.get("medium", [])
    hard = by_diff.get("hard", []) + by_diff.get("expert", [])
    if len(easy) >= MIN_EVIDENCE and len(hard) >= MIN_EVIDENCE and _acc(easy) - _acc(hard) >= 0.3:
        rep.insights.append(Insight(
            "difficulty_drop",
            f"You perform well on easier questions ({_acc(easy):.0%}) but accuracy drops on hard ones ({_acc(hard):.0%}). "
            "Increase difficulty gradually.",
            f"{len(easy)} easier and {len(hard)} harder answers", _strength(min(len(easy), len(hard))),
        ))

    # --- pattern: recurring error types
    tag_counts: dict[str, set[str]] = defaultdict(set)
    tag_n: dict[str, int] = defaultdict(int)
    for r in records:
        for gname, (tags, _) in TAG_GROUPS.items():
            if tags & set(r.error_tags):
                tag_counts[gname].add(r.attempt_id)
                tag_n[gname] += 1
    for gname, attempts in tag_counts.items():
        if tag_n[gname] >= 2:
            recurring = len(attempts) >= 2
            rep.insights.append(Insight(
                f"errors_{gname}",
                f"{'Repeated' if recurring else 'Several'} errors involving {TAG_GROUPS[gname][1]}.",
                f"{tag_n[gname]} answers in {len(attempts)} exam(s)",
                "strong" if recurring and tag_n[gname] >= 3 else "tentative",
            ))

    # --- question format difficulties
    for qtype, (acc, n) in rep.type_accuracy.items():
        if n >= MIN_EVIDENCE + 1 and acc < 0.5:
            label = QUESTION_TYPE_LABELS.get(QuestionType(qtype), qtype) if qtype in QuestionType._value2member_map_ else qtype
            rep.insights.append(Insight("format", f"'{label}' questions are a weak format ({acc:.0%}).", f"{n} answers", _strength(n)))

    # --- confidence calibration
    confident = [r for r in records if r.confidence and r.confidence >= 4]
    if len(confident) >= 4:
        wrong = sum(1 for r in confident if r.is_wrong) / len(confident)
        if wrong >= 0.4:
            rep.insights.append(Insight(
                "overconfidence", f"{wrong:.0%} of answers you rated as confident were wrong - double-check before moving on.",
                f"{len(confident)} confidence ratings", _strength(len(confident)),
            ))

    # --- improvement over time
    for subject, recs in by_subject.items():
        attempts = sorted({(r.started_at, r.attempt_id) for r in recs})
        if len(attempts) >= 4:
            half = len(attempts) // 2
            first_ids = {a for _, a in attempts[:half]}
            first = [r for r in recs if r.attempt_id in first_ids]
            second = [r for r in recs if r.attempt_id not in first_ids]
            delta = _acc(second) - _acc(first)
            if abs(delta) >= 0.1:
                rep.insights.append(Insight(
                    "trend", f"{subject}: accuracy {'improved' if delta > 0 else 'declined'} from {_acc(first):.0%} to {_acc(second):.0%} across your exams.",
                    f"{len(attempts)} exams", _strength(len(attempts) * 2),
                ))

    # --- recommendations
    weak = rep.weak_topics()
    for t in weak[:5]:
        rep.recommendations.append(
            f"Practise '{t.topic}' ({t.subject}): {t.accuracy:.0%} over {t.n} answers in {t.attempts} exam(s) - {t.label.lower()}."
        )
    weak_types = sorted(((a, n, k) for k, (a, n) in rep.type_accuracy.items() if n >= MIN_EVIDENCE and a < 0.6))
    for a, n, k in weak_types[:2]:
        label = QUESTION_TYPE_LABELS[QuestionType(k)] if k in QuestionType._value2member_map_ else k
        rep.recommendations.append(f"Do extra practice with '{label}' questions ({a:.0%} over {n} answers).")
    if not rep.recommendations:
        if all(t.status == "insufficient_data" for t in rep.topics):
            rep.recommendations.append("Not enough data yet for reliable recommendations - complete more exams.")
        else:
            rep.recommendations.append("No clear weaknesses detected. Try harder questions or a mixed mock exam.")
    return rep

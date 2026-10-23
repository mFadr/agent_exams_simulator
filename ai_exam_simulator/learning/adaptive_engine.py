"""Adaptive exam planning and question selection.

The engine never lowers difficulty just to raise scores: it steps down only
after sustained low accuracy, by one level, within the configured floor, and
keeps part of the exam at the requested level so performance remains measured.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..exams.question_models import Difficulty, ExamConfig, ExamMode, Question, QuestionType
from .analytics import ResultRecord
from .weakness_detection import WeaknessReport

STEP_UP_ACCURACY = 0.8
STEP_DOWN_ACCURACY = 0.45
MIN_RECORDS_FOR_STEP = 5


@dataclass
class AdaptivePlan:
    mode: ExamMode
    topics: list[str]
    topic_weights: dict[str, float] = field(default_factory=dict)
    difficulty_mix: dict[Difficulty, float] = field(default_factory=dict)
    type_weights: dict[QuestionType, float] = field(default_factory=dict)
    revision_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    focus_notes: str = ""

    def main_difficulty(self) -> Difficulty:
        return max(self.difficulty_mix.items(), key=lambda kv: kv[1])[0] if self.difficulty_mix else Difficulty.MEDIUM


class AdaptiveEngine:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    def plan(
        self,
        config: ExamConfig,
        report: WeaknessReport,
        records: list[ResultRecord],
        revision_ids: list[str] | None = None,
    ) -> AdaptivePlan:
        subject_records = [r for r in records if r.subject == config.subject]
        topics = list(config.topics)
        plan = AdaptivePlan(mode=config.mode, topics=topics)
        plan.difficulty_mix = {config.difficulty: 1.0}
        plan.type_weights = {t: 1.0 for t in config.question_types}
        # None = "any topic of the subject" when the user did not restrict topics
        plan.topic_weights = {t: 1.0 for t in topics} if topics else {None: 1.0}  # type: ignore[dict-item]
        mode = config.mode

        if mode in (ExamMode.FIXED, ExamMode.PRACTICE):
            plan.reasons.append("Fixed settings: topics and difficulty exactly as you selected.")
            return plan

        stats = {t.topic: t for t in report.topics if t.subject == config.subject}

        if mode == ExamMode.ADAPTIVE:
            self._adapt_difficulty(plan, config, subject_records)
            for topic, st in stats.items():
                if topics and topic not in topics:
                    continue
                if st.status in ("recurring_weakness", "developing") and st.accuracy < 0.7:
                    plan.topic_weights[topic] = 2.0
                    plan.reasons.append(f"More questions on '{topic}' ({st.accuracy:.0%} over {st.n} answers).")
                elif st.status == "mastered":
                    plan.topic_weights[topic] = 0.4
                    plan.reasons.append(f"Fewer basic questions on mastered topic '{topic}'.")

        elif mode == ExamMode.WEAKNESS:
            weak = [t for t in report.weak_topics() if t.subject == config.subject]
            if not weak:
                weak = [t for t in report.topics if t.subject == config.subject and t.status == "one_time_mistake"]
            if weak:
                plan.topics = [t.topic for t in weak[:6]]
                plan.topic_weights = {t.topic: 1.0 + (1.0 - t.accuracy) for t in weak[:6]}
                plan.reasons.append("Focusing on: " + ", ".join(f"{t.topic} ({t.accuracy:.0%}, {t.label.lower()})" for t in weak[:6]) + ".")
                plan.focus_notes = "Target typical mistakes in: " + ", ".join(t.topic for t in weak[:6])
            else:
                plan.reasons.append("No weak topics detected for this subject yet - using your selected topics.")
            weak_types = [QuestionType(k) for k, (a, n) in report.type_accuracy.items()
                          if n >= 3 and a < 0.6 and k in QuestionType._value2member_map_ and QuestionType(k) in config.question_types]
            for t in weak_types:
                plan.type_weights[t] = 2.0
                plan.reasons.append(f"Extra practice in the '{t.value}' format.")

        elif mode == ExamMode.REVISION:
            plan.revision_ids = list(revision_ids or [])
            if plan.revision_ids:
                plan.reasons.append(f"Re-testing {len(plan.revision_ids)} question(s) you previously answered incorrectly.")
            else:
                plan.reasons.append("No previously incorrect questions found - nothing to revise yet.")

        elif mode == ExamMode.MIXED_MOCK:
            studied = sorted({r.topic for r in subject_records})
            plan.topics = sorted(set(topics) | set(studied))
            plan.topic_weights = {t: 1.0 for t in plan.topics}
            lvl = config.difficulty.level
            mix = {Difficulty.from_level(lvl - 1): 0.25, config.difficulty: 0.5, Difficulty.from_level(lvl + 1): 0.25}
            plan.difficulty_mix = self._clamp_mix(mix, config)
            plan.reasons.append(f"Mixed mock: {len(plan.topics)} topic(s) incl. previously studied ones, mixed formats and difficulty.")
        return plan

    def _adapt_difficulty(self, plan: AdaptivePlan, config: ExamConfig, recs: list[ResultRecord]) -> None:
        level = config.difficulty
        at_level = [r for r in recs if r.difficulty == level.value][-15:]
        if len(at_level) < MIN_RECORDS_FOR_STEP:
            plan.reasons.append(f"Only {len(at_level)} answers at '{level.value}' difficulty so far - keeping the level.")
            return
        acc = sum(r.fraction for r in at_level) / len(at_level)
        if acc >= STEP_UP_ACCURACY and level.level < config.max_difficulty.level:
            up = Difficulty.from_level(level.level + 1)
            plan.difficulty_mix = {level: 0.4, up: 0.6}
            plan.reasons.append(f"Raising difficulty towards '{up.value}': {acc:.0%} accuracy on the last {len(at_level)} '{level.value}' answers.")
        elif acc <= STEP_DOWN_ACCURACY and level.level > config.min_difficulty.level:
            down = Difficulty.from_level(level.level - 1)
            plan.difficulty_mix = {down: 0.5, level: 0.5}
            plan.reasons.append(
                f"Adding '{down.value}' questions to rebuild foundations ({acc:.0%} on '{level.value}'); half of the exam stays at "
                f"'{level.value}' so your progress is still measured."
            )
        else:
            plan.reasons.append(f"Keeping '{level.value}' difficulty ({acc:.0%} on the last {len(at_level)} answers).")

    @staticmethod
    def _clamp_mix(mix: dict[Difficulty, float], config: ExamConfig) -> dict[Difficulty, float]:
        out: dict[Difficulty, float] = {}
        for d, w in mix.items():
            lvl = max(config.min_difficulty.level, min(config.max_difficulty.level, d.level))
            dd = Difficulty.from_level(lvl)
            out[dd] = out.get(dd, 0.0) + w
        return out

    # ------------------------------------------------------------------ selection
    def allocate(self, plan: AdaptivePlan, n: int) -> list[tuple[str | None, Difficulty]]:
        """Return n (topic, difficulty) slots according to the plan's weights."""
        topics = list(plan.topic_weights.items()) or [(None, 1.0)]
        diffs = list(plan.difficulty_mix.items()) or [(Difficulty.MEDIUM, 1.0)]
        slots = []
        for i in range(n):
            topic = self.rng.choices([t for t, _ in topics], weights=[w for _, w in topics])[0]
            diff = self._quota_pick(diffs, i, n)
            slots.append((topic, diff))
        return slots

    @staticmethod
    def _quota_pick(diffs: list[tuple[Difficulty, float]], i: int, n: int) -> Difficulty:
        total = sum(w for _, w in diffs)
        acc = 0.0
        pos = (i + 0.5) / n
        for d, w in sorted(diffs, key=lambda x: x[0].level):
            acc += w / total
            if pos <= acc:
                return d
        return diffs[-1][0]

    def select_from_bank(
        self, plan: AdaptivePlan, bank: list[Question], n: int, types: list[QuestionType],
    ) -> tuple[list[Question], list[tuple[str | None, Difficulty]]]:
        """Pick bank questions for each slot. Returns (selected, unfilled_slots)."""
        if plan.mode == ExamMode.REVISION:
            by_id = {q.id: q for q in bank}
            picked = [by_id[i] for i in plan.revision_ids if i in by_id][:n]
            return picked, []
        pool = [q for q in bank if q.type in types]
        self.rng.shuffle(pool)
        type_w = plan.type_weights
        pool.sort(key=lambda q: -type_w.get(q.type, 1.0) * self.rng.random())
        used: set[str] = set()
        selected: list[Question] = []
        unfilled: list[tuple[str | None, Difficulty]] = []
        for topic, diff in self.allocate(plan, n):
            match = next((q for q in pool if q.id not in used and (topic is None or q.topic == topic) and q.difficulty == diff), None)
            if match is None:  # relax difficulty by one level
                match = next((q for q in pool if q.id not in used and (topic is None or q.topic == topic)
                              and abs(q.difficulty.level - diff.level) <= 1), None)
            if match is None:
                unfilled.append((topic, diff))
                continue
            used.add(match.id)
            selected.append(match)
        return selected, unfilled

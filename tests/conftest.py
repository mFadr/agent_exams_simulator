from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ai_exam_simulator.ai.mock_provider import MockProvider  # noqa: E402
from ai_exam_simulator.ai.provider_interface import ProviderKind  # noqa: E402
from ai_exam_simulator.app.application import AppContext  # noqa: E402
from ai_exam_simulator.exams.question_models import (  # noqa: E402
    Difficulty,
    MatchPair,
    NumericalAnswer,
    Option,
    Question,
    QuestionType,
    Rubric,
    RubricCriterion,
)
from ai_exam_simulator.security.secrets import MemoryBackend, SecretStore  # noqa: E402


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def local_mock() -> MockProvider:
    return MockProvider(ProviderKind.LOCAL, name="Mock local")


@pytest.fixture
def cloud_mock() -> MockProvider:
    return MockProvider(ProviderKind.CLOUD, name="Mock cloud", model="mock-cloud")


@pytest.fixture
def ctx(tmp_path, local_mock, cloud_mock) -> AppContext:
    c = AppContext.create(tmp_path / "data", secrets=SecretStore(MemoryBackend()), local_provider=local_mock,
                          cloud_provider=cloud_mock, setup_logging=False)
    c.settings.settings.ai.max_retries = 0
    c.router._sleep = lambda s: None
    yield c
    c.db.close()


def mcq(qid: str = "q1", topic: str = "Algebra", points: float = 1, difficulty: Difficulty = Difficulty.MEDIUM, text: str | None = None) -> Question:
    return Question(
        id=qid, subject="Mathematics", topic=topic, type=QuestionType.SINGLE_CHOICE, difficulty=difficulty, max_points=points,
        text=text or f"What is 2 + 2? ({qid})", options=[Option(id="a", text="3"), Option(id="b", text="4"), Option(id="c", text="5")],
        correct_option_ids=["b"], explanation="2 + 2 = 4",
    )


def multi(qid: str = "m1") -> Question:
    return Question(
        id=qid, subject="Mathematics", topic="Number theory", type=QuestionType.MULTIPLE_SELECT,
        text="Which numbers are prime? Select all that apply.",
        options=[Option(id="a", text="2"), Option(id="b", text="4"), Option(id="c", text="5"), Option(id="d", text="9")],
        correct_option_ids=["a", "c"], max_points=2, explanation="2 and 5 are prime",
    )


def numerical(qid: str = "n1", value: float = 9.81, unit: str | None = "m/s^2", rel: float = 0.01) -> Question:
    return Question(
        id=qid, subject="Physics", topic="Mechanics", type=QuestionType.NUMERICAL, text="What is g on Earth's surface?",
        numerical=NumericalAnswer(value=value, unit=unit, rel_tolerance=rel), max_points=2, explanation="g ≈ 9.81 m/s²",
    )


def essay(qid: str = "e1") -> Question:
    return Question(
        id=qid, subject="Physics", topic="Mechanics", type=QuestionType.ESSAY, max_points=4,
        text="Explain Newton's third law and give an example.",
        rubric=Rubric(criteria=[
            RubricCriterion(id="c1", description="States the law", points=2),
            RubricCriterion(id="c2", description="Gives a correct example", points=2),
        ]), model_answer="Forces come in equal and opposite pairs acting on different bodies.", explanation="…",
    )


def matching(qid: str = "mt1") -> Question:
    return Question(
        id=qid, subject="Chemistry", topic="Atomic Structure", type=QuestionType.MATCHING, text="Match particle to charge.",
        match_pairs=[MatchPair(id="p1", left="Proton", right="+1"), MatchPair(id="p2", left="Electron", right="−1"),
                     MatchPair(id="p3", left="Neutron", right="0")], max_points=3, explanation="…",
    )


def ordering(qid: str = "o1") -> Question:
    return Question(
        id=qid, subject="History", topic="Cold War", type=QuestionType.ORDERING, text="Order these events chronologically.",
        order_items=["A", "B", "C", "D"], max_points=3, explanation="…",
    )

"""Import/export of question banks (JSON native format, CSV export)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..config.defaults import MAX_IMPORT_FILE_BYTES, SUPPORTED_BANK_EXTENSIONS
from ..exams.question_models import Provenance, Question, ReviewStatus, new_id
from ..exams.validation import validate_question
from ..security.validation import InputValidationError, validate_import_file

FORMAT_ID = "ai-exam-simulator/question-bank"
FORMAT_VERSION = 1


@dataclass
class BankImportResult:
    questions: list[Question] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def export_json(questions: list[Question], path: Path) -> None:
    data = {"format": FORMAT_ID, "version": FORMAT_VERSION, "questions": [q.model_dump(mode="json") for q in questions]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(questions: list[Question], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "subject", "topic", "type", "difficulty", "points", "question", "options", "correct_answer", "explanation", "tags", "provenance", "review_status"])
        for q in questions:
            w.writerow([q.id, q.subject, q.topic, q.type.value, q.difficulty.value, q.max_points, q.text,
                        " | ".join(o.text for o in q.options), q.correct_answer_display(), q.explanation,
                        ",".join(q.tags), q.provenance.value, q.review_status.value])


def import_json(path: str | Path, *, keep_ids: bool = False) -> BankImportResult:
    """Validate every question; invalid entries are reported, never imported."""
    res = BankImportResult()
    try:
        p = validate_import_file(path, SUPPORTED_BANK_EXTENSIONS, MAX_IMPORT_FILE_BYTES)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except InputValidationError as exc:
        res.errors.append(str(exc))
        return res
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        res.errors.append(f"Could not read the file as JSON: {exc}")
        return res
    items = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        res.errors.append("Expected a list of questions (or an object with a 'questions' list).")
        return res
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            res.errors.append(f"Entry {i}: not an object.")
            continue
        item = dict(item)
        if not keep_ids:
            item["id"] = new_id()
        item.setdefault("provenance", Provenance.IMPORTED.value)
        if item.get("review_status") in (ReviewStatus.AI_VERIFIED.value, ReviewStatus.TOOL_VERIFIED.value, ReviewStatus.USER_VERIFIED.value):
            # verification claims from another file cannot be trusted
            item["review_status"] = ReviewStatus.UNVERIFIED.value
        try:
            q = Question.model_validate(item)
        except ValidationError as exc:
            e = exc.errors()[0]
            res.errors.append(f"Entry {i}: {'.'.join(str(x) for x in e.get('loc', []))} - {e.get('msg')}")
            continue
        report = validate_question(q)
        if not report.ok:
            res.errors.append(f"Entry {i} ('{q.text[:40]}'): " + "; ".join(report.errors))
            continue
        if report.warnings:
            res.warnings.append(f"Entry {i}: " + "; ".join(report.warnings))
        res.questions.append(q)
    return res

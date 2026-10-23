"""Exports, backup/restore, secret handling, log redaction and restricted code execution."""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3

import pytest

from ai_exam_simulator.ai.cloud_provider import API_KEY_SECRET_NAME
from ai_exam_simulator.config.settings import AppPaths, SettingsManager
from ai_exam_simulator.exams.question_models import CodeTestCase, ExamConfig, Question, QuestionType, ScoringRules
from ai_exam_simulator.grading.code_grader import grade_with_tests, static_check
from ai_exam_simulator.grading.grading_service import GradingService
from ai_exam_simulator.grading.models import ResultStatus
from ai_exam_simulator.reports.export_service import (
    ExportError,
    apply_pending_restore,
    create_backup,
    export_all_data_json,
    export_report_csv,
    export_report_pdf,
    stage_restore,
)
from ai_exam_simulator.reports.report_generator import build_report
from ai_exam_simulator.security.redaction import RedactingFilter, redact, register_secret
from ai_exam_simulator.security.secrets import MemoryBackend, SecretStore, mask_key
from ai_exam_simulator.security.validation import InputValidationError, safe_filename, validate_cloud_url

from conftest import essay, mcq, numerical

KEY = "sk-live-THISISAFAKEKEY1234567890"


def _graded(ctx):
    qs = [mcq("a"), numerical("n"), essay("e")]
    ctx.questions.save_many(qs)
    s = ctx.start_exam(ExamConfig(title="Export ž test"), qs)
    s.set_answer("a", "b")
    s.set_answer("n", "9.5")
    s.set_answer("e", "Forces come in pairs.")
    s.submit()
    results = ctx.grading.grade_all(s.state.questions, s.state.answers, s.config.scoring, use_ai=False)
    summary = ctx.finalize(s, results)
    return s, summary, results


def test_csv_and_pdf_export(ctx, tmp_path):
    s, summary, results = _graded(ctx)
    rep = build_report(s.state, summary, results)
    assert rep.notes  # essay pending -> provisional note
    csv_path = export_report_csv(rep, tmp_path / "r.csv")
    rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
    assert rows[0] == ["Exam", "Export ž test"] and any(r and r[0] == "1" for r in rows)
    pdf = export_report_pdf(rep, tmp_path / "r.pdf")
    assert pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 1500
    assert rep.filtered("unanswered") == [] and len(rep.filtered("review")) == 1


def test_full_data_export_and_backup_restore(ctx, tmp_path):
    _graded(ctx)
    ctx.secrets.set(API_KEY_SECRET_NAME, KEY)
    out = export_all_data_json(ctx.db, tmp_path / "all.json")
    text = out.read_text(encoding="utf-8")
    assert KEY not in text and json.loads(text)["attempts"]
    backup = create_backup(ctx.db, ctx.settings, tmp_path / "b.zip")
    import zipfile

    with zipfile.ZipFile(backup) as z:
        assert set(z.namelist()) == {"exam_simulator.db", "config.json", "manifest.json"}
        assert KEY.encode() not in b"".join(z.read(n) for n in z.namelist())
    # restore into a fresh data dir
    paths = AppPaths(tmp_path / "restore_target")
    paths.ensure()
    stage_restore(backup, paths)
    assert apply_pending_restore(paths)
    con = sqlite3.connect(paths.db_path)
    assert con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    con.close()
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    with pytest.raises(ExportError):
        stage_restore(bad, paths)


def test_api_key_never_in_config_db_or_logs(ctx, caplog):
    ctx.secrets.set(API_KEY_SECRET_NAME, KEY)
    ctx.settings.save()
    assert KEY not in ctx.paths.config_path.read_text(encoding="utf-8")
    with open(ctx.paths.db_path, "rb") as fh:
        assert KEY.encode() not in fh.read()
    logger = logging.getLogger("test.redaction")
    handler = logging.StreamHandler(io.StringIO())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.error("request failed with key %s and header Authorization: Bearer %s", KEY, KEY)
    logged = handler.stream.getvalue()
    assert KEY not in logged and "REDACTED" in logged
    assert mask_key(KEY).endswith("7890") and KEY not in mask_key(KEY)


def test_redaction_patterns():
    register_secret("my-custom-secret-value")
    s = redact("x my-custom-secret-value y api_key=abcdef123456 Bearer abc.def.ghi123 sk-abcdefghijklmnop")
    assert "my-custom-secret-value" not in s and "abcdef123456" not in s and "abc.def.ghi123" not in s and "sk-abcdefghijklmnop" not in s


def test_settings_file_ignores_smuggled_key_and_survives_corruption(tmp_path):
    paths = AppPaths(tmp_path)
    paths.ensure()
    paths.config_path.write_text(json.dumps({"ai": {"api_key": "sk-should-be-dropped", "routing_mode": "hybrid_auto"}}), encoding="utf-8")
    m = SettingsManager(paths)
    assert m.settings.ai.routing_mode.value == "hybrid_auto"
    m.save()
    assert "sk-should-be-dropped" not in paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text("{corrupt", encoding="utf-8")
    assert SettingsManager(paths).settings.ai.routing_mode.value == "local_only"  # defaults restored
    assert (tmp_path / "config.corrupt.json").exists()


def test_secret_store_memory_backend():
    s = SecretStore(MemoryBackend())
    assert not s.is_persistent and "Session memory" in s.backend_description
    s.set("k", "  value-123456  ")
    assert s.get("k") == "value-123456" and s.has("k")
    s.delete("k")
    assert s.get("k") is None


def test_input_validation():
    with pytest.raises(InputValidationError):
        validate_cloud_url("http://api.example.com/v1")  # plain http to a remote host
    assert validate_cloud_url("http://localhost:8000/v1/") == "http://localhost:8000/v1"
    with pytest.raises(InputValidationError):
        validate_cloud_url("ftp://x")
    assert safe_filename("CON") == "export" and safe_filename('a<b>:c?.pdf') == "a_b_c_.pdf"


# ------------------------------------------------------------------ programming questions

PAL = Question(subject="Programming", topic="Strings", type=QuestionType.PROGRAMMING, max_points=4,
               text="Write is_pal(s) returning True if s is a palindrome.", function_name="is_pal",
               test_cases=[CodeTestCase(call="is_pal('aba')", expected="True"), CodeTestCase(call="is_pal('ab')", expected="False"),
                           CodeTestCase(call="is_pal('')", expected="True", hidden=True)])


def test_static_check():
    ok, notes = static_check("def is_pal(s):\n    return s == s[::-1]\n", "is_pal")
    assert ok and notes == []
    ok, notes = static_check("def x(:\n", "is_pal")
    assert not ok and "Syntax error" in notes[0]
    ok, notes = static_check("import os\ndef other(): pass", "is_pal")
    assert any("not defined" in n for n in notes) and any("os" in n for n in notes)


def test_restricted_execution_grades_tests():
    r = grade_with_tests(PAL, "def is_pal(s):\n    return s == s[::-1]\n", timeout_s=10)
    assert r.status == ResultStatus.CORRECT and r.points == 4
    partial = grade_with_tests(PAL, "def is_pal(s):\n    return len(s) % 2 == 1\n", timeout_s=10)
    assert partial.status == ResultStatus.PARTIAL and "(hidden test)" in partial.feedback


def test_restricted_execution_blocks_imports_files_and_loops():
    evil = "import os\ndef is_pal(s):\n    return True\n"
    r = grade_with_tests(PAL, evil, timeout_s=10)
    assert r.status == ResultStatus.INCORRECT and "not allowed" in r.feedback
    opener = "def is_pal(s):\n    open('x.txt', 'w').write('hi')\n    return True\n"
    r2 = grade_with_tests(PAL, opener, timeout_s=10)
    assert r2.points == 0 and "NameError" in r2.feedback
    loop = "def is_pal(s):\n    while True:\n        pass\n"
    r3 = grade_with_tests(PAL, loop, timeout_s=2)
    assert r3.status == ResultStatus.INCORRECT and "time limit" in r3.feedback


def test_code_execution_disabled_by_default_falls_back_to_review():
    svc = GradingService()
    r, ai_candidate = svc.grade_deterministic(PAL, "def is_pal(s):\n    return s == s[::-1]\n", ScoringRules())
    assert r.status == ResultStatus.PENDING_REVIEW and ai_candidate
    assert "disabled" in r.feedback
    enabled = GradingService(code_execution_enabled=lambda: True, code_timeout_s=lambda: 10)
    r2, _ = enabled.grade_deterministic(PAL, "def is_pal(s):\n    return s == s[::-1]\n", ScoringRules())
    assert r2.status == ResultStatus.CORRECT

"""SQLite persistence, migrations, version history and error handling."""

from __future__ import annotations

import sqlite3

import pytest

from ai_exam_simulator.storage.database import Database, DatabaseError
from ai_exam_simulator.storage.migrations import MIGRATIONS, SCHEMA_VERSION, _split
from ai_exam_simulator.storage.repositories import ExamRepository, QuestionRepository, TemplateRepository
from ai_exam_simulator.exams.question_models import ExamConfig, ReviewStatus, preset_config

from conftest import essay, mcq, numerical


def test_fresh_database_is_fully_migrated(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"questions", "attempts", "question_results", "documents", "learning_profile"} <= tables
    db.close()


def test_migration_from_version_1(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    for stmt in _split(MIGRATIONS[0]):
        con.execute(stmt)
    con.execute("PRAGMA user_version = 1")
    con.execute("INSERT INTO exams (id, title, subject, mode, config, question_ids, created_at) VALUES ('e','t','s','fixed','{}','[]','now')")
    con.commit()
    con.close()
    db = Database(path)
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    row = db.query_one("SELECT source_document_ids FROM exams WHERE id='e'")
    assert row[0] == "[]"  # new column with default, old data kept
    db.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer version"):
        Database(path)


def test_corrupted_file_gives_clear_error(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(DatabaseError, match="damaged"):
        Database(path)


def test_question_persistence_and_history(tmp_path):
    db = Database(tmp_path / "q.db")
    repo = QuestionRepository(db)
    q = mcq("x")
    repo.save(q)
    q.text = "What is 2 + 2 (edited)?"
    q.review_status = ReviewStatus.USER_VERIFIED
    repo.save(q)
    got = repo.get("x")
    assert got.version == 2 and "edited" in got.text
    versions = repo.versions("x")
    assert versions[0][0] == 1 and "edited" not in versions[0][2].text
    # upsert_if_changed does not create history for identical content
    repo.upsert_if_changed(got)
    assert repo.get("x").version == 2
    db.close()
    # persists across reopen
    db2 = Database(tmp_path / "q.db")
    assert QuestionRepository(db2).get("x").version == 2
    db2.close()


def test_search_pagination_and_soft_delete(tmp_path):
    db = Database(tmp_path / "s.db")
    repo = QuestionRepository(db)
    for i in range(25):
        repo.save(mcq(f"q{i}", topic="Algebra" if i % 2 else "Geometry"))
    page1, total = repo.search(limit=10, offset=0)
    page3, _ = repo.search(limit=10, offset=20)
    assert total == 25 and len(page1) == 10 and len(page3) == 5
    alg, n = repo.search(topic="Algebra")
    assert n == 12
    repo.delete("q1")
    assert repo.search(topic="Algebra")[1] == 11
    cid = repo.create_collection("Favourites")
    repo.add_to_collection(cid, ["q2", "q4"])
    assert repo.search(collection_id=cid)[1] == 2
    db.close()


def test_malformed_record_is_skipped(tmp_path):
    db = Database(tmp_path / "m.db")
    repo = QuestionRepository(db)
    repo.save(mcq("ok"))
    with db.transaction() as c:
        c.execute("INSERT INTO questions (id, subject, topic, qtype, difficulty, text, data, provenance, review_status, created_at, updated_at) "
                  "VALUES ('bad','S','T','single_choice','easy','x','{broken','user','unverified','n','n')")
    qs, total = repo.search()
    assert total == 2 and [q.id for q in qs] == ["ok"]
    db.close()


def test_transaction_rollback(tmp_path):
    db = Database(tmp_path / "t.db")
    repo = QuestionRepository(db)
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            c.execute("INSERT INTO collections (id, name, created_at) VALUES ('c', 'n', 'now')")
            raise RuntimeError("interrupted")
    assert repo.list_collections() == []
    db.close()


def test_templates_and_results_persist(ctx):
    tid = ctx.templates.save("My template", preset_config("Difficult Exam"))
    assert any(t[0] == tid for t in ctx.templates.list())
    assert len([t for t in ctx.templates.list() if t[3]]) == 5  # built-in presets seeded
    qs = [mcq("a"), numerical("n"), essay("e")]
    ctx.questions.save_many(qs)
    s = ctx.start_exam(ExamConfig(title="Persist"), qs)
    s.set_answer("a", "b")
    s.submit()
    results = ctx.grading.grade_all(s.state.questions, s.state.answers, s.config.scoring, use_ai=False)
    summary = ctx.finalize(s, results)
    loaded = ctx.exams.load_results(s.state.attempt_id)
    assert loaded[0].points == summary.points and len(loaded[1]) == 3
    rows = ctx.exams.list_attempts()
    assert rows[0].percentage == summary.percentage and rows[0].status == "graded"
    assert ctx.exams.open_attempt_ids() == []

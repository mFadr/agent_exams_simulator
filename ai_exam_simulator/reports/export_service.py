"""Exports: CSV, printable PDF, JSON data export, backup & restore."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..config.settings import AppPaths, SettingsManager
from ..storage.database import Database
from ..storage.migrations import SCHEMA_VERSION
from .report_generator import ExamReport

RESTORE_PENDING = "restore_pending.db"


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------- CSV


def export_report_csv(report: ExamReport, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        s = report.summary
        w.writerow(["Exam", report.title])
        w.writerow(["Subject", report.subject])
        w.writerow(["Started", report.started_at])
        w.writerow(["Score (calculated)", f"{s.points:g} / {s.max_points:g}", f"{s.percentage:.1f}%"])
        w.writerow(["Passed", "n/a" if s.passed is None else ("yes" if s.passed else "no")])
        w.writerow(["Provisional", "yes" if s.provisional else "no"])
        w.writerow([])
        w.writerow(["#", "Topic", "Type", "Difficulty", "Question", "Your answer", "Correct answer / reference",
                    "Points", "Max", "Status", "Grading method", "Needs review", "Feedback", "Time (s)", "Flagged"])
        for r in report.rows:
            q = r.question
            w.writerow([r.number, q.topic, q.type.value, q.difficulty.value, q.text, r.answer_text,
                        q.correct_answer_display(), f"{r.result.points:g}", f"{r.result.max_points:g}",
                        r.result.status.value, r.method_label, "yes" if r.result.needs_review else "no",
                        r.result.feedback, f"{r.time_spent_s:.0f}", "yes" if r.flagged else "no"])
    return path


def export_history_csv(rows: list, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["attempt_id", "exam", "subject", "mode", "attempt", "started_at", "submitted_at", "status", "percentage", "passed", "provisional"])
        for r in rows:
            w.writerow([r.attempt_id, r.title, r.subject, r.mode, r.attempt_number, r.started_at, r.submitted_at or "",
                        r.status, "" if r.percentage is None else f"{r.percentage:.1f}",
                        "" if r.passed is None else ("yes" if r.passed else "no"), "yes" if r.provisional else "no"])
    return path


# ---------------------------------------------------------------------------- PDF


def export_report_pdf(report: ExamReport, path: Path, include_explanations: bool = True) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise ExportError("PDF export requires the 'reportlab' package.") from exc

    font, bold = "Helvetica", "Helvetica-Bold"
    # Use a Unicode TrueType font when available (Czech/Polish characters etc.)
    for regular, b in ((r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
                       (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf")):
        if Path(regular).exists() and Path(b).exists():
            try:
                pdfmetrics.registerFont(TTFont("UF", regular))
                pdfmetrics.registerFont(TTFont("UF-Bold", b))
                font, bold = "UF", "UF-Bold"
                break
            except Exception:
                pass

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName=font, fontSize=9.5, leading=12.5)
    small = ParagraphStyle("s", parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName=bold, fontSize=18)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=bold, fontSize=13)
    h3 = ParagraphStyle("h3", parent=ss["Heading4"], fontName=bold, fontSize=10.5)

    def P(text: str, style=body) -> Paragraph:
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    s = report.summary
    story = [P(report.title, h1), P(f"{report.subject} · attempt {report.attempt_number} · started {report.started_at} · submitted {report.submitted_at}", small)]
    if report.submit_reason:
        story.append(P(report.submit_reason, small))
    story.append(Spacer(1, 4 * mm))
    story.append(P("Calculated result", h2))
    passed = "—" if s.passed is None else ("PASSED" if s.passed else "NOT PASSED")
    data = [
        ["Points", f"{s.points:g} / {s.max_points:g}", "Percentage", f"{s.percentage:.1f} %"],
        ["Status", passed + (" (provisional)" if s.provisional else ""), "Pass threshold", "—" if s.pass_threshold is None else f"{s.pass_threshold:g} %"],
        ["Correct", str(s.correct), "Partially correct", str(s.partial)],
        ["Incorrect", str(s.incorrect), "Unanswered", str(s.unanswered)],
    ]
    t = Table(data, colWidths=[32 * mm, 50 * mm, 35 * mm, 50 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
                           ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f0f0"))]))
    story += [t, Spacer(1, 3 * mm)]
    if s.by_topic:
        story.append(P("By topic", h3))
        tdata = [["Topic", "Points", "%", "Questions"]] + [[b.key, f"{b.points:g}/{b.max_points:g}", f"{b.percentage:.0f}%", str(b.count)] for b in s.by_topic]
        tt = Table(tdata, colWidths=[80 * mm, 30 * mm, 20 * mm, 25 * mm])
        tt.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTNAME", (0, 0), (-1, 0), bold), ("FONTSIZE", (0, 0), (-1, -1), 9),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc"))]))
        story.append(tt)
    for note in report.notes:
        story.append(P("Note: " + note, small))
    story.append(Spacer(1, 4 * mm))
    story.append(P("Question review", h2))
    for r in report.rows:
        q = r.question
        block = [P(f"Q{r.number}. [{q.topic} · {q.type.value} · {q.difficulty.value}] {r.result.points:g}/{r.result.max_points:g} pts – {r.result.status.value}", h3),
                 P(q.text)]
        if q.code_context:
            block.append(P(q.code_context, small))
        if q.options and q.type.value != "true_false":
            block.append(P("Options: " + " | ".join(o.text for o in q.options), small))
        block.append(P(f"Your answer: {r.answer_text}"))
        block.append(P(f"Correct answer / reference: {q.correct_answer_display()}"))
        block.append(P(f"Grading: {r.method_label}. {r.result.feedback}", small))
        for c in r.result.criteria:
            block.append(P(f"  • {c.description}: {c.awarded:g}/{c.max_points:g} – {c.comment}", small))
        if r.result.needs_review:
            block.append(P("⚠ This grading is uncertain and should be reviewed.", small))
        if r.result.overridden:
            block.append(P(f"Manually adjusted: {r.result.override_note}", small))
        if include_explanations and q.explanation:
            block.append(P(f"Explanation: {q.explanation}", small))
        if include_explanations and r.result.ai_explanation:
            block.append(P(f"AI tutor explanation (AI-generated, may contain errors): {r.result.ai_explanation}", small))
        block.append(Spacer(1, 3 * mm))
        story.append(KeepTogether(block))

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=report.title, author="AI Exam Simulator")
    doc.build(story)
    return path


# ---------------------------------------------------------------------------- data export / backup


def export_all_data_json(db: Database, path: Path) -> Path:
    """Export the user's learning data (no secrets exist in the DB by design)."""
    tables = ["questions", "collections", "collection_items", "templates", "exams", "attempts", "attempt_results",
              "question_results", "documents", "learning_profile", "ai_usage_log"]
    out: dict[str, list] = {}
    for t in tables:
        out[t] = [dict(r) for r in db.query(f"SELECT * FROM {t}")]
    out["_meta"] = [{"exported_at": datetime.now().isoformat(), "schema_version": SCHEMA_VERSION}]
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def create_backup(db: Database, settings: SettingsManager, target: Path) -> Path:
    tmp = settings.paths.temp_dir / "backup_snapshot.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    db.backup_to(tmp)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "exam_simulator.db")
        z.writestr("config.json", json.dumps(settings.export_dict(), indent=2))
        z.writestr("manifest.json", json.dumps({"app": "AI Exam Simulator", "schema_version": SCHEMA_VERSION,
                                                 "created": datetime.now().isoformat(), "contains_secrets": False}))
    tmp.unlink(missing_ok=True)
    return target


def stage_restore(backup_zip: Path, paths: AppPaths) -> str:
    """Validate a backup and stage it; it is applied on the next application start."""
    try:
        with zipfile.ZipFile(backup_zip) as z:
            names = set(z.namelist())
            if "exam_simulator.db" not in names or "manifest.json" not in names:
                raise ExportError("This is not an AI Exam Simulator backup.")
            manifest = json.loads(z.read("manifest.json"))
            if int(manifest.get("schema_version", 0)) > SCHEMA_VERSION:
                raise ExportError("The backup was made by a newer version of the application.")
            staged = paths.root / RESTORE_PENDING
            with z.open("exam_simulator.db") as src, staged.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        raise ExportError(f"The backup file is damaged: {exc}") from exc
    try:
        con = sqlite3.connect(str(staged))
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
    except sqlite3.DatabaseError as exc:
        staged.unlink(missing_ok=True)
        raise ExportError("The database inside the backup is damaged.") from exc
    if ok != "ok":
        staged.unlink(missing_ok=True)
        raise ExportError("The database inside the backup failed the integrity check.")
    return manifest.get("created", "")


def apply_pending_restore(paths: AppPaths) -> bool:
    """Called at startup before the database is opened."""
    staged = paths.root / RESTORE_PENDING
    if not staged.exists():
        return False
    if paths.db_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        paths.backups_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.db_path, paths.backups_dir / f"pre_restore_{stamp}.db")
        for suffix in ("-wal", "-shm"):
            side = Path(str(paths.db_path) + suffix)
            if side.exists():
                shutil.copy2(side, paths.backups_dir / f"pre_restore_{stamp}.db{suffix}")
                side.unlink()
    staged.replace(paths.db_path)
    return True

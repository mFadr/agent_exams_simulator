"""Import study materials into the local library.

Imported text is stored locally in SQLite. The original file stays where the
user keeps it (only its path and hash are recorded); a missing or changed
source file is detected and reported, but the extracted text remains usable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config.defaults import MAX_IMPORT_FILE_BYTES, MAX_PASTED_TEXT_CHARS, SUPPORTED_IMPORT_EXTENSIONS
from ..exams.question_models import new_id
from ..security.validation import InputValidationError, validate_import_file
from ..storage.repositories import DocumentRepository
from .text_extraction import ExtractionError, ExtractionResult, extract_file, extract_pasted

log = logging.getLogger(__name__)


@dataclass
class ImportOutcome:
    document_id: str | None
    name: str
    status: str  # "ok" | "partial" | "needs_ocr" | "duplicate" | "error"
    message: str
    warnings: list[str] = field(default_factory=list)
    sections: int = 0
    chars: int = 0


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DocumentImporter:
    def __init__(self, repo: DocumentRepository) -> None:
        self.repo = repo

    def import_file(self, path: str | Path, *, subject: str = "", use_ocr: bool = False, allow_duplicate: bool = False) -> ImportOutcome:
        name = Path(path).name
        try:
            p = validate_import_file(path, SUPPORTED_IMPORT_EXTENSIONS, MAX_IMPORT_FILE_BYTES)
        except InputValidationError as exc:
            return ImportOutcome(None, name, "error", str(exc))
        digest = file_sha256(p)
        dup = self.repo.find_by_hash(digest)
        if dup and not allow_duplicate:
            return ImportOutcome(dup["id"], name, "duplicate", f"This file was already imported as '{dup['name']}'.")
        try:
            res = extract_file(p, use_ocr=use_ocr)
        except ExtractionError as exc:
            return ImportOutcome(None, name, "error", str(exc))
        except Exception as exc:  # defensive: parser libraries can raise anything on hostile files
            log.exception("Unexpected extraction failure for %s", name)
            return ImportOutcome(None, name, "error", f"Unexpected error while reading the file ({type(exc).__name__}).")
        return self._store(name, str(p), digest, res, subject)

    def import_text(self, text: str, title: str = "Pasted text", subject: str = "") -> ImportOutcome:
        if len(text) > MAX_PASTED_TEXT_CHARS:
            return ImportOutcome(None, title, "error", "The pasted text is too long.")
        try:
            res = extract_pasted(text, title)
        except ExtractionError as exc:
            return ImportOutcome(None, title, "error", str(exc))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._store(title, "(pasted)", digest, res, subject)

    def _store(self, name: str, path: str, digest: str, res: ExtractionResult, subject: str) -> ImportOutcome:
        if not res.sections:
            status = "needs_ocr" if res.needs_ocr else "error"
            return ImportOutcome(None, name, status, " ".join(res.warnings) or "No text could be extracted.", res.warnings)
        status = "partial" if res.needs_ocr or res.warnings else "ok"
        doc_id = new_id("doc")
        sections = [
            {"id": new_id("sec"), "idx": i, "title": s.title[:200], "page_start": s.page_start, "page_end": s.page_end, "text": s.text}
            for i, s in enumerate(res.sections)
        ]
        self.repo.add(
            {"id": doc_id, "name": name, "path": path, "file_hash": digest, "format": res.format,
             "status": status, "page_count": res.page_count, "char_count": res.char_count,
             "warnings": res.warnings, "subject": subject},
            sections,
        )
        msg = f"Imported {len(sections)} section(s), {res.char_count:,} characters."
        if res.warnings:
            msg += " " + " ".join(res.warnings)
        return ImportOutcome(doc_id, name, status, msg, res.warnings, len(sections), res.char_count)

    def source_file_state(self, doc: dict) -> str:
        """'ok', 'missing', 'changed' or 'n/a' (pasted text)."""
        if doc["path"] == "(pasted)":
            return "n/a"
        p = Path(doc["path"])
        if not p.exists():
            return "missing"
        try:
            return "ok" if file_sha256(p) == doc["file_hash"] else "changed"
        except OSError:
            return "missing"

"""Select relevant excerpts from imported materials (TF-IDF ranking).

Only the most relevant, size-limited excerpts are sent to a model - never the
whole document.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..config.defaults import MAX_SOURCE_CHARS_PER_REQUEST
from ..exams.question_models import SourceRef

_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_STOP = set("""the and for are but not you all any can had her was one our out has have from they this that with
which their there what when where who will would about into than then them these those been were its also such
only other some more most very upon each over under between while after before because could should through""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text)) if t not in _STOP]


def chunk_text(text: str, size: int = 1800, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class Chunk:
    __slots__ = ("text", "ref", "tokens")

    def __init__(self, text: str, ref: SourceRef) -> None:
        self.text = text
        self.ref = ref
        self.tokens = Counter(tokenize(text))


def build_chunks(doc: dict, sections: list[dict]) -> list[Chunk]:
    out = []
    for s in sections:
        for piece in chunk_text(s["text"]):
            out.append(Chunk(piece, SourceRef(
                document_id=doc["id"], document_name=doc["name"], section_id=s["id"], section_title=s["title"],
                page_start=s.get("page_start"), page_end=s.get("page_end"), excerpt=piece[:300],
            )))
    return out


def rank_chunks(chunks: list[Chunk], query: str, k: int = 6) -> list[Chunk]:
    """Rank chunks by TF-IDF similarity to ``query``; with an empty query, spread evenly."""
    if not chunks:
        return []
    q = tokenize(query)
    if not q:
        step = max(1, len(chunks) // k)
        return chunks[::step][:k]
    n = len(chunks)
    df: Counter[str] = Counter()
    for c in chunks:
        df.update(set(c.tokens))
    scores = []
    for i, c in enumerate(chunks):
        length = sum(c.tokens.values()) or 1
        s = sum((c.tokens[t] / length) * math.log(1 + n / (1 + df[t])) for t in q if t in c.tokens)
        scores.append((s, -i, c))
    scores.sort(reverse=True, key=lambda x: (x[0], x[1]))
    best = [c for s, _, c in scores if s > 0][:k]
    if len(best) < k:  # pad with unranked chunks for coverage
        seen = {id(c) for c in best}
        best += [c for c in chunks if id(c) not in seen][: k - len(best)]
    return best


def select_excerpts(chunks: list[Chunk], query: str, max_chars: int = MAX_SOURCE_CHARS_PER_REQUEST, k: int = 8) -> list[Chunk]:
    out, total = [], 0
    for c in rank_chunks(chunks, query, k):
        if total + len(c.text) > max_chars and out:
            break
        out.append(c)
        total += len(c.text)
    return out


def keyword_topics(text: str, n: int = 10) -> list[str]:
    """Frequent content words as a local, AI-free topic suggestion."""
    counts = Counter(tokenize(text))
    return [w for w, _ in counts.most_common(n * 3) if len(w) > 4][:n]

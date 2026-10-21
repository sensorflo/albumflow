"""Find an album from a loosely typed "topic/album" query.

Candidates are the existing <new>\\<topic>\\<album> folders. Matching ignores
case and treats spaces, "-" and "_" alike. A query whose words all appear in a
name ranks highest; otherwise difflib similarity catches typos. Ties go to the
most recently modified album (folder time: last photo added or removed).
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from albumflow import config

MIN_SIMILARITY = 0.4


@dataclass(frozen=True)
class Album:
    topic: str
    name: str
    mtime: float

    def __str__(self) -> str:
        return f"{self.topic}/{self.name}"


def split_query(query: str) -> tuple[str | None, str]:
    """'party/secret' or 'party\\secret' -> ('party', 'secret'); 'secret' -> (None, 'secret')."""
    parts = [p for p in re.split(r"[/\\]", query.strip()) if p]
    if len(parts) > 2:
        raise ValueError(f"Expected topic/album, got {query!r}")
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0] if parts else ""


def normalize(text: str) -> str:
    return " ".join(re.split(r"[\s_\-]+", text.lower())).strip()


def albums(root: Path | None = None) -> list[Album]:
    root = root or config.PICTURES_NEW
    found = []
    for topic in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        for album in topic.iterdir():
            # A junction is an album moved to the annex: not offered (writing there would be slow).
            if album.is_dir() and not os.path.isjunction(album) and not any(
                    re.search(p, album.name, re.IGNORECASE) for p in config.EXCLUDED_ALBUM_PATTERNS):
                found.append(Album(topic.name, album.name, album.stat().st_mtime))
    return found


def score(query: str, text: str) -> float:
    """2: every query word appears in text (then recency decides); else difflib similarity in [0, 1]."""
    q, t = normalize(query), normalize(text)
    if all(word in t for word in q.split()):
        return 2.0
    return difflib.SequenceMatcher(None, q, t).ratio()


def exact(query: str, candidates: list[Album]) -> list[Album]:
    topic, name = split_query(query)
    return [a for a in candidates
            if normalize(a.name) == normalize(name) and (topic is None or normalize(a.topic) == normalize(topic))]


def rank(query: str, candidates: list[Album], limit: int = 5) -> list[Album]:
    """Best matches first; an empty query lists the most recently modified albums."""
    topic, name = split_query(query)
    scored = []
    for a in candidates:
        if topic is None:
            s = score(name, a.name)
        else:
            s = score(f"{topic} {name}", f"{a.topic} {a.name}")
        if s >= MIN_SIMILARITY:
            scored.append((s, a.mtime, a))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [a for _, _, a in scored[:limit]]

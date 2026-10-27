"""A movie's canonical name, its folder name in the view:

    <English title>[ - <original title>] (<year>)[ {<edition>}] [<what the files hold>]

The titles and the year come from EMDB, the edition from the release name (which the store keeps forever), and the
rest from MediaInfo (mediainfo.describe), e.g.
"Parasite - 기생충 (2019) [1080p AVC 29Mbps, ko TrueHD Atmos 7.1, sub en +1]".
"""

from __future__ import annotations

import re

# Words in a release name (after the title and year), as a regular expression, and the edition they stand for
EDITIONS = [
    (r"remaster(ed)?|rm4k", "Remastered"),
    (r"director'?s cut|dc", "Director's Cut"),
    (r"final cut", "Final Cut"),
    (r"extended( cut| edition)?", "Extended"),
    (r"uncut", "Uncut"),
    (r"unrated", "Unrated"),
    (r"theatrical( cut)?", "Theatrical"),
    (r"imax", "IMAX"),
    (r"criterion", "Criterion"),
    (r"(\d+)(st|nd|rd|th) anniv(ersary)?( edition)?", r"\1\2 Anniversary"),
]


def words(text: str) -> str:
    """Separators of release names (dots, underscores, brackets) as spaces."""
    return " ".join(re.split(r"[\s._()\[\]{}-]+", text.lower())).strip()


def edition(release: str) -> str:
    """E.g. "Donnie.Darko.2001.REMASTERED.Directors.Cut-FGT" -> "Remastered, Director's Cut"; only the part after
    the year is looked at, so a title like "The Final Cut" isn't taken for an edition."""
    years = list(re.finditer(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", release))  # the last: "Blade.Runner.2049.2017"
    rest = " " + words(release[years[-1].end():] if years else release) + " "
    found = []
    for pattern, name in EDITIONS:
        m = re.search(rf" ({pattern}) ", rest)
        if m:
            found.append((m.start(), re.fullmatch(pattern, m.group(1)).expand(name)))
    return ", ".join(name for _, name in sorted(found))


def safe(text: str) -> str:
    """text as a Windows file name: ':' becomes ' - ', '/' and '\\' become '-', '?*"<>|' are dropped, and no dot or
    space at the end (Windows drops them silently)."""
    text = re.sub(r"\s*:\s*", " - ", text)
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r'[?*"<>|]', "", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(". ")


def canonical(title: str, original: str, year: str, release: str, tech: str | None) -> str:
    name = safe(title)
    if original and original.strip().lower() != title.strip().lower():
        name += f" - {safe(original)}"
    if year:
        name += f" ({year})"
    if ed := edition(release):
        name += f" {{{ed}}}"
    if tech:
        name += f" [{safe(tech)}]"
    return name

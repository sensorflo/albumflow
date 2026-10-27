"""EMDB's database (emdb.dat): reading the movies' Locations and changing them, nothing else.

The file is UTF-16 with a BOM and CRLF lines, JSON-like but not JSON. Each movie has one line starting with
YEAR_PREFIX whose value is FIELDS fields separated by "\\x1e"; field LOCATION_FIELD is the Location, e.g. a movie
file's path. In it EMDB writes "[" as "<SC>" and "]" as "<SCB>"; a leading "[label]" is a volume label. Anything
unexpected stops reading, and a written file differs from the read one only in the changed Locations.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from albumflow import config
from albumflow.files import AlbumflowError

BOM = b"\xff\xfe"
TITLE_PREFIX = '        "title": "'
YEAR_PREFIX = '        "year": "'
SEPARATOR = "\x1e"
FIELDS = 17
LOCATION_FIELD = 7


@dataclass
class Movie:
    title: str
    location: str  # as EMDB shows it, e.g. "[FloBig]D:\Videos\x.mkv"
    line: int  # index of its YEAR_PREFIX line


def decode(field: str) -> str:
    return field.replace("<SCB>", "]").replace("<SC>", "[")


def encode(location: str) -> str:
    return location.replace("[", "<SC>").replace("]", "<SCB>")


def without_label(location: str) -> str:
    """The path of a Location, without a leading "[volume label]"."""
    return re.sub(r"^\[[^\]\\]*\](?=[A-Za-z]:\\)", "", location)


def read_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    if not raw.startswith(BOM):
        raise AlbumflowError(f"{path}: not the EMDB format albumflow knows (no UTF-16 byte order mark)")
    return raw[len(BOM):].decode("utf-16-le").split("\r\n")


def movies(lines: list[str], path: Path | str = "EMDB's database") -> list[Movie]:
    found, title = [], None
    for i, line in enumerate(lines):
        if line.startswith(TITLE_PREFIX):
            title = line[len(TITLE_PREFIX):].split(SEPARATOR)[0]
        elif line.startswith(YEAR_PREFIX):
            fields = line.split(SEPARATOR)
            if len(fields) != FIELDS or title is None:
                raise AlbumflowError(f"{path}, line {i + 1}: not the EMDB format albumflow knows "
                                     f"({len(fields)} fields instead of {FIELDS})")
            found.append(Movie(title, decode(fields[LOCATION_FIELD]), i))
            title = None
    return found


def with_locations(lines: list[str], changes: dict[int, str]) -> list[str]:
    """lines with new Locations, by the line index of their movie."""
    lines = list(lines)
    for i, location in changes.items():
        fields = lines[i].split(SEPARATOR)
        fields[LOCATION_FIELD] = encode(location)
        lines[i] = SEPARATOR.join(fields)
    return lines


def write_lines(path: Path, lines: list[str]) -> None:
    """Atomically, so a crash leaves the old or the new file."""
    temp = path.with_name(path.name + ".albumflow-new")
    temp.write_bytes(BOM + "\r\n".join(lines).encode("utf-16-le"))
    os.replace(temp, path)


def newest_backup(backups: Path | None = None) -> Path | None:
    """emdb.dat of the newest backup made in EMDB (a folder EMDB_BACKUP_<date>_<time>; not its daily
    AUTO_BACKUP_*)."""
    backups = backups or config.EMDB_BACKUPS
    folders = sorted(backups.glob("EMDB_BACKUP_*")) if backups.is_dir() else []
    return folders[-1] / "emdb.dat" if folders else None


def check_safe_to_write(path: Path, running: list[str], backups: Path | None = None) -> None:
    """EMDB closed (it may rewrite the file on exit), and its newest backup is exactly the current database."""
    if any(name.lower() == "emdb.exe" for name in running):
        raise AlbumflowError("EMDB is running; close it first (it would overwrite the changes)")
    backup = newest_backup(backups)
    if backup is None or not backup.is_file() or backup.read_bytes() != path.read_bytes():
        raise AlbumflowError(f"No backup of the current EMDB database in {backups or config.EMDB_BACKUPS}; make "
                             "one in EMDB, close EMDB, then run this again")

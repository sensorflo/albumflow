"""Read-only checks against Lightroom catalogs.

Lightroom keeps all edits in its catalog, and copies them into the photos'
XMP sidecars only when they are saved ("Save Metadata to File"). Before a
folder is moved, its files should hold everything on their own; otherwise the
work depends on the catalog alone. Virtual copies never make it into a file.

The catalog is opened read-only and never changed. Lightroom must be closed
(the callers check that), so the catalog is consistent.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from albumflow import config
from albumflow.files import AlbumflowError

# Adobe_images.sidecarStatus is 1 for photos whose metadata changed since it was
# saved to the file (Lightroom's "metadata file needs to be updated" badge).
UNSAVED = 1


@dataclass
class Report:
    photos: int = 0  # photos of the folder(s) in the catalogs
    unsaved: dict[Path, int] = field(default_factory=dict)  # folder -> photos with unsaved metadata
    virtual: dict[Path, int] = field(default_factory=dict)  # folder -> virtual copies


def _key(path: Path | str) -> str:
    """Comparable form of a path: forward slashes, lower case, trailing slash."""
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/") + "/"


def _rows(catalogs: list[Path] | None) -> list[tuple[str, float, int]]:
    """(folder path, sidecarStatus, is virtual copy) of every photo in the catalogs."""
    rows = []
    for catalog in config.LIGHTROOM_CATALOGS if catalogs is None else catalogs:
        if not Path(catalog).is_file():
            raise AlbumflowError(f"Lightroom catalog not found: {catalog} (see LIGHTROOM_CATALOGS in config.py)")
        try:
            con = sqlite3.connect(Path(catalog).as_uri() + "?mode=ro", uri=True)
            try:
                rows += con.execute("""
                    select r.absolutePath || fo.pathFromRoot, i.sidecarStatus, i.masterImage is not null
                    from Adobe_images i
                    join AgLibraryFile f on i.rootFile = f.id_local
                    join AgLibraryFolder fo on f.folder = fo.id_local
                    join AgLibraryRootFolder r on fo.rootFolder = r.id_local""").fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            raise AlbumflowError(f"Can't read the Lightroom catalog {catalog}: {e}") from None
    return rows


def _report(rows, folders: list[Path]) -> Report:
    report = Report()
    wanted = [_key(f) for f in folders]
    for folder, status, is_virtual in rows:
        if not any(_key(folder).startswith(w) for w in wanted):
            continue
        report.photos += 1
        path = Path(folder)
        if is_virtual:
            report.virtual[path] = report.virtual.get(path, 0) + 1
        elif status == UNSAVED:
            report.unsaved[path] = report.unsaved.get(path, 0) + 1
    return report


def check(folders: list[Path], catalogs: list[Path] | None = None) -> Report:
    """What the catalogs know about the photos below any of folders."""
    return _report(_rows(catalogs), folders)


def check_each(folders: list[Path], catalogs: list[Path] | None = None) -> list[Report]:
    """check() for each folder separately, reading the catalogs only once."""
    rows = _rows(catalogs)
    return [_report(rows, [f]) for f in folders]


def refuse_if_unsafe(report: Report, relative_to: Path) -> None:
    """Raise if moving would leave work only in the catalog, naming the folders."""
    def lines(counts: dict[Path, int], what: str) -> str:
        out = []
        for folder, n in sorted(counts.items(), key=lambda kv: str(kv[0]).lower()):
            try:
                shown = Path(os.path.abspath(folder)).relative_to(relative_to)
            except ValueError:
                shown = folder
            out.append(f"  {shown} ({n} {what})")
        return "\n".join(out)

    problems = []
    if report.unsaved:
        problems.append("Lightroom has metadata not saved to the files in:\n" + lines(report.unsaved, "photos")
                        + "\nIn Lightroom: select them > Metadata > Save Metadata to File (Ctrl+S).")
    if report.virtual:
        problems.append("There are virtual copies, whose edits exist only in the Lightroom catalog, in:\n"
                        + lines(report.virtual, "virtual copies"))
    if problems:
        raise AlbumflowError("Not moving. " + "\n\n".join(problems))

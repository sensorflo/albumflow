"""Purging albums from where they are no longer needed, to make room.

Four places, each with its rule:

- card: <card>\\Backup\\<album>, the originals an import left on the card. Once all its photos are in a
  secondary backup (on the secondary drive or in the cloud), or the album is finished.
- rejected: Rejected\\<topic>\\<album>, the C: folder and its annex part. Once all its photos are in a
  secondary backup, or the album is finished. To C:'s Recycle Bin (the annex part is moved back to C:
  first, since S: has none), or permanently.
- primary: the primary drive's copied-to-secondary\\<topic>\\<album>: always, it is on the secondary drive.
- secondary: the secondary drive's pictures-new\\<topic>\\<album>: once the album is finished.

An album is finished once it is in ORIGINALS (also when moved to the annex): restic backs up what was kept.
"""

from __future__ import annotations

import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from albumflow import annex, backup, card_import, clock, config, index
from albumflow.clock import split_name
from albumflow.files import AlbumflowError, copy_file, recycle

PLACES = ("card", "rejected", "primary", "secondary")


@dataclass
class Candidate:
    """An album that may be purged from a place, unless blocked."""
    name: str  # <topic>\<album> (on a card: <album>)
    folders: list[Path]  # deleted together: for rejected the C: folder and the annex folder (with the link)
    root: Path  # nothing above it is deleted
    files: dict[str, int]  # name -> size of its photos
    newest: float  # modification time of its newest file
    blocked: str | None  # why the rule doesn't allow purging it yet; None: it does

    @property
    def size(self) -> int:
        return sum(self.files.values())

    def score(self, now: float) -> float:
        """Size, weighted by age, like the annex suggestions."""
        age = max(0.0, (now - self.newest) / 86400)
        return self.size / 1e9 * (1 - math.exp(-age / config.ANNEX_AGE_DAYS))


def is_finished(topic: str | None, album: str) -> bool:
    """Whether the album is in ORIGINALS, also if moved to the annex; topic None: in any topic."""
    folders = [config.ORIGINALS / topic / album] if topic else list(config.ORIGINALS.glob(f"*/{album}"))
    folders += [annex.link_of(f) for f in folders] + list(config.ORIGINALS.glob(f"*/{album}{config.ANNEX_SUFFIX}"))
    return any(f.is_dir() or os.path.lexists(f) for f in folders)


def not_in_secondary_backup(files: dict[str, int], album: str) -> int:
    """How many of the files are in no secondary backup: by name and size, or for a clock-corrected one
    (<master time>-<import name>), its original by import name."""
    dirs = backup.secondary_backup_dirs_named(album)
    known = backup.on_secondary_drive(backup.find_backup_drive(), None, album)

    def found(name: str, size: int) -> bool:
        corrected, import_name = split_name(name)
        if known.get(import_name) is not None and (corrected or known[import_name] == size):
            return True
        return any((d / import_name).is_file() and (corrected or (d / import_name).stat().st_size == size)
                   for d in dirs)

    return sum(1 for name, size in files.items() if not found(name, size))


def _files(folders: list[Path]) -> tuple[dict[str, int], float]:
    files, newest = {}, 0.0
    for folder in folders:
        for path in folder.rglob("*") if folder.is_dir() else []:
            if path.is_file():
                stat = path.stat()
                files[path.name] = stat.st_size
                newest = max(newest, stat.st_mtime)
    return files, newest


def _rule_secondary_backup(topic: str | None, album: str, files: dict[str, int]) -> str | None:
    if is_finished(topic, album):
        return None
    missing = not_in_secondary_backup(files, album)
    return f"{missing} of {len(files)} photos not yet in a secondary backup" if missing else None


def card_candidates(card: Path, except_album: str = "") -> list[Candidate]:
    found = []
    for old in card_import.old_backups(card, except_album):
        files, newest = _files([old.path])
        found.append(Candidate(old.path.name, [old.path], card, files, newest,
                               _rule_secondary_backup(None, old.path.name, files)))
    return found


def rejected_candidates() -> list[Candidate]:
    root = annex.home() / config.REJECTED_TREE
    found = []
    for topic in sorted(p for p in root.iterdir() if p.is_dir() and not annex.is_link(p)) if root.is_dir() else []:
        albums = {p.name.removesuffix(config.ANNEX_SUFFIX) for p in topic.iterdir()
                  if p.is_dir() and (not annex.is_link(p) or annex.is_annex_link(p))}
        for album in sorted(albums):
            here, link = topic / album, annex.link_of(topic / album)
            folders = ([here] if here.is_dir() and not annex.is_link(here) else []) \
                + ([annex.link_target(link)] if annex.is_annex_link(link) else [])
            files, newest = _files(folders)
            found.append(Candidate(f"{topic.name}\\{album}", folders, root, files, newest,
                                   _rule_secondary_backup(topic.name, album, files)))
    return found


def corrections_pending(topic: str, album: str) -> bool:
    """Whether photos of the album (in the new tree) still wait for a clock correction."""
    album_dir = config.album_dir(topic, album)
    if not (album_dir.is_dir() and clock.has_measurements(album_dir)):
        return False
    state = clock.AlbumState(album_dir)
    return bool(clock.plan(clock.load_shots(album_dir, state, log=lambda *_: None)))


def drive_candidates(root: Path, finished_only: bool, drive: Path | None = None) -> list[Candidate]:
    """The albums below root; finished_only: on the secondary drive, else on the primary drive (drive)."""
    found = []
    for folder in sorted(root.glob("*/*")) if root.is_dir() else []:
        if not folder.is_dir() or annex.is_link(folder):
            continue
        files, newest = _files([folder])
        blocked = None
        if finished_only and not is_finished(folder.parent.name, folder.name):
            blocked = "not finished yet (not in Originals)"
        elif not finished_only and corrections_pending(folder.parent.name, folder.name):
            blocked = "clock corrections pending, which need the originals here"
        elif not finished_only and not is_finished(folder.parent.name, folder.name):
            # Afterwards only the secondary drive (or the primary drive's index of it) knows the photos are there
            known = backup.on_secondary_drive(drive, folder.parent.name, folder.name)
            if missing := sum(1 for name, size in files.items() if known.get(name) != size):
                blocked = (f"{missing} of {len(files)} photos not in the index of the secondary drive yet "
                           "(update-secondary scans it)")
        found.append(Candidate(str(folder.relative_to(root)), [folder], root, files, newest, blocked))
    return found


def places() -> list[tuple[str, str, list[Candidate]]]:
    """(place, description, candidates, large and old first) of every place available now."""
    found = []
    for card in card_import.find_cards():
        found.append(("card", f"the card {card}", card_candidates(card)))
    found.append(("rejected", f"{config.REJECTED_TREE} (C: and the annex)", rejected_candidates()))
    drive, secondary = backup.find_backup_drive(), backup.find_secondary_drive()
    if drive:
        found.append(("primary", f"the primary drive {drive} ({config.COPIED_DIR})",
                      drive_candidates(drive / config.COPIED_DIR, finished_only=False, drive=drive)))
    if secondary:
        found.append(("secondary", f"the secondary drive {secondary} ({config.SECONDARY_DIR})",
                      drive_candidates(secondary / config.SECONDARY_DIR, finished_only=True)))
    now = time.time()
    return [(place, description, sorted(candidates, key=lambda c: c.score(now), reverse=True))
            for place, description, candidates in found]


def delete(candidate: Candidate, place: str, permanent: bool, log=print) -> None:
    """Delete the album from the place: permanently, except rejected ones without permanent, which go to
    C:'s Recycle Bin (their annex part moved back to C: first, since S: has none)."""
    if place != "rejected":
        for folder in candidate.folders:
            annex.delete_tree(folder, candidate.root)
            _remove_if_empty(folder.parent, candidate.root)
        drive, secondary = backup.find_backup_drive(), backup.find_secondary_drive()
        if place == "secondary" and drive and secondary:
            index.record_secondary(drive, secondary)
        return
    topic, album = candidate.name.split("\\", 1)
    rejected = annex.home() / config.REJECTED_TREE
    here = rejected / topic / album
    link = annex.link_of(here)
    there = annex.link_target(link) if annex.is_annex_link(link) else None
    if there and there.is_dir():
        if permanent:
            annex.delete_tree(there, annex.require_annex())
        else:
            _move_back(there, here, log)
    if os.path.lexists(link):
        os.rmdir(link)  # only the junction
    if here.is_dir() and not annex.is_link(here):
        if permanent:
            annex.delete_tree(here, annex.home())
        else:
            recycle(here)
    _remove_if_empty(here.parent, rejected)
    if there:
        _remove_if_empty(there.parent, annex.require_annex() / config.REJECTED_TREE)


def _move_back(there: Path, here: Path, log) -> None:
    """Move the annex part's files into the C: folder, file by file (crash-safe: a re-run goes on), then remove
    the annex folder."""
    needed = sum(p.stat().st_size for p in there.rglob("*") if p.is_file())
    if needed > shutil.disk_usage(annex.home()).free:
        raise AlbumflowError(f"Not enough space on {annex.home().drive} to move {there} there for the Recycle "
                             "Bin; --permanent deletes it in place")
    log(f"  moving its annex part ({needed / 1e9:.1f} GB) back to {annex.home().drive}, for the Recycle Bin")
    for src in sorted(p for p in there.rglob("*") if p.is_file()):
        dest = here / src.relative_to(there)
        if not (dest.is_file() and dest.stat().st_size == src.stat().st_size):
            copy_file(src, dest)
        src.unlink()
    annex.delete_tree(there, annex.require_annex())


def _remove_if_empty(folder: Path, root: Path) -> None:
    """The topic folder, once its last album is gone."""
    if folder != root and folder.is_dir() and not annex.is_link(folder) and not any(folder.iterdir()):
        folder.rmdir()

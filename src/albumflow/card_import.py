r"""Import photos from a camera card.

For each photo, one after the other:
  1. copy DCIM\...\_DSC8045.ARW to <new>\<topic>\<album>\_DSC8045.ARW.part
  2. read its capture time with exiftool (on the PC copy, which is fast)
  3. rename the copy to <capture time>-_DSC8045.ARW
  4. move the original on the card to <card>\Backup\<album>\<same new name>

So a photo leaves DCIM only once a complete copy is on the PC, and an
interrupted import can simply be run again.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from albumflow import backup, clock, config
from albumflow.exiftool import ExifTool
from albumflow.files import AlbumflowError, ByteCounter, Progress, drives, eject, same_content, volume_serial

POLL_SECONDS = 2.0  # how often to look for inserted cards


@dataclass
class ImportItem:
    source: Path  # on the card, in DCIM
    pc: Path  # destination on the PC
    card_backup: Path  # destination on the card


def card_candidates() -> list[Path]:
    """Drives that might be camera cards: all but the system drive."""
    return [d for d in drives() if d.drive.upper() != "C:"]


def has_photos(card: Path) -> bool:
    dcim = card / "DCIM"
    return dcim.is_dir() and any(p.suffix.lower() in config.CAMERA_SUFFIXES and p.is_file() for p in dcim.rglob("*"))


def find_cards() -> list[Path]:
    """All inserted camera cards: drives with a DCIM folder (which may be empty)."""
    return [d for d in card_candidates() if (d / "DCIM").is_dir()]


def card_key(card: Path) -> tuple[str, int | None]:
    """Identifies a card, so a finished card left in the reader isn't imported again,
    while another card inserted into the same reader is."""
    return str(card).upper(), volume_serial(card)


def camera_files(card: Path) -> list[Path]:
    dcim = card / "DCIM"
    return sorted(p for p in dcim.rglob("*") if p.suffix.lower() in config.CAMERA_SUFFIXES and p.is_file())


def new_name(capture_time: str, source: Path) -> str:
    return f"{capture_time}-{source.stem}{source.suffix}"


def check_album_dir(topic: str, album: str, create_album: bool) -> Path:
    """The album folder; the topic folder must exist, the album only if not create_album."""
    topic_dir = config.PICTURES_NEW / topic
    if not topic_dir.is_dir():
        raise AlbumflowError(f"Topic folder does not exist: {topic_dir}")
    album_dir = config.album_dir(topic, album)
    if not album_dir.is_dir() and not create_album:
        raise AlbumflowError(f"Album folder does not exist: {album_dir}\n"
                             "Check the name, or pass --create-album to start a new album.")
    return album_dir


def import_only(topic: str, album: str, card: Path | None = None, create_album: bool = False,
                eject_cards: bool = False, dry_run: bool = False, log=print,
                card_progress: Callable[[Path], Progress | None] | None = None,
                on_card: Callable[[int], None] | None = None,
                on_photo: Callable[[int], None] | None = None,
                on_card_done: Callable[[Path], None] | None = None) -> list[ImportItem]:
    """Import one card, or (card=None) every inserted card, one after the other.

    Waits (polling) until a card is inserted. The next card is only looked for
    once the current one is done, so it can be inserted while the current one
    is still being imported; when none is there, the import ends. A card with
    an empty DCIM folder is simply done. Finished cards are recognized by their
    volume serial number and not picked again. With eject_cards, each card is
    ejected once it is done.

    card_progress(card) returns the progress callback for that card, so each
    card can get its own progress bar. on_card(bytes) is called with the size
    of a card's photos before they are copied, on_photo(bytes) after each photo
    has its final name on the PC.
    """
    check_album_dir(topic, album, create_album)

    def one(c: Path) -> list[ImportItem]:
        progress = card_progress(c) if card_progress and not dry_run else None
        return import_card(c, topic, album, create_album=create_album, dry_run=dry_run, log=log,
                           progress=progress, on_card=on_card, on_photo=on_photo)

    def inserted() -> list[Path]:
        if card is None:
            return find_cards()
        return [card] if (card / "DCIM").is_dir() else []

    items, done, waiting = [], set(), False
    while True:
        cards = [c for c in inserted() if card_key(c) not in done]
        if not cards:
            if done:
                break
            if dry_run:
                raise AlbumflowError("No camera card found (no drive with a DCIM folder)")
            if not waiting:
                log("Waiting for a camera card (a drive with a DCIM folder)...")
                waiting = True
            time.sleep(POLL_SECONDS)
            continue
        current = cards[0]
        done.add(card_key(current))
        items += one(current)
        if dry_run:
            continue
        if has_photos(current):  # e.g. a photo exiftool could not handle
            raise AlbumflowError(f"Photos remain in {current / 'DCIM'} after importing; please check by hand")
        log(f"Card {current} done.")
        if on_card_done:
            on_card_done(current)  # e.g. offer to prune old backups, while the card is still there
        if eject_cards:
            eject_card(current, log)
        if card is None:
            log("Looking for the next card...")
    if card is None and not dry_run:
        log("No more cards.")
    return items


def eject_card(card: Path, log=print) -> None:
    """Eject a finished card; failing is only a warning, the import itself succeeded."""
    try:
        eject(card)
        log(f"Ejected {card}; it can be removed.")
    except AlbumflowError as e:
        log(f"Warning: {e}. Please eject it by hand.")


def import_card(card: Path, topic: str, album: str, create_album: bool = False, dry_run: bool = False,
                log=print, progress: Progress | None = None, on_card: Callable[[int], None] | None = None,
                on_photo: Callable[[int], None] | None = None) -> list[ImportItem]:
    album_dir = check_album_dir(topic, album, create_album)
    card_backup_dir = card / config.CARD_BACKUP_DIR / album
    sources = camera_files(card)
    if not album_dir.is_dir():
        log("  (new album folder" + (" would be created)" if dry_run else " created)"))
    if not sources:
        warn_remaining(card, set(), dry_run, log)
        return []
    if dry_run:
        return dry_run_card(card, sources, album_dir, card_backup_dir, log)

    total = sum(src.stat().st_size for src in sources)
    free = shutil.disk_usage(config.PICTURES_NEW).free
    if total > free:
        raise AlbumflowError(f"Not enough space on the PC: need {total / 1e9:.1f} GB, free {free / 1e9:.1f} GB")

    album_dir.mkdir(exist_ok=True)
    if on_card:
        on_card(total)
    counter = ByteCounter(total, progress)
    items, skipped = [], 0
    state = clock.AlbumState(album_dir)  # records each photo's camera, for the clock sync
    with ExifTool() as exif:
        try:
            for i, src in enumerate(sources, 1):
                size = src.stat().st_size
                counter.start(src.name)
                item, was_there, camera = import_photo(src, album_dir, card_backup_dir, exif)
                state.cameras[Path(item.pc.name).stem] = camera
                if i % 100 == 0:
                    state.save()
                counter.add(size)
                items.append(item)
                skipped += was_there
                if on_photo:
                    on_photo(size)
        finally:
            state.save()

    log(f"Done: {len(items) - skipped} imported" + (f", {skipped} were already on the PC" if skipped else "") + ".")
    warn_remaining(card, set(), dry_run, log)
    return items


def import_photo(src: Path, album_dir: Path, card_backup_dir: Path,
                 exif: ExifTool) -> tuple[ImportItem, bool, str]:
    """Copy, name by capture time, move the original on the card.

    Returns (item, whether it was already on the PC, its camera).
    """
    tmp = album_dir / (src.name + ".part")  # ".part" is never taken for a photo, e.g. by the backup
    shutil.copy2(src, tmp)
    try:
        capture_time, camera = exif.capture_info(tmp)
        if not capture_time:
            raise AlbumflowError(f"No usable capture time in {src}")
        name = new_name(capture_time, src)
        item = ImportItem(src, album_dir / name, card_backup_dir / name)
        if item.card_backup.exists():
            raise AlbumflowError(f"{item.card_backup} already exists, but {src} is still in DCIM. "
                                 "Please check by hand which one is right.")
        # Already imported and clock-corrected since? (Its content differs then, by design.)
        corrected = [p for p in album_dir.glob(f"*-{name}") if clock.split_name(p.name) == (p.name[:15], name)]
        was_there = item.pc.exists() or bool(corrected)
        if item.pc.exists() and not same_content(tmp, item.pc):
            raise AlbumflowError(f"{item.pc} already exists with different content than {src}")
        if not was_there:
            tmp.replace(item.pc)
    finally:
        tmp.unlink(missing_ok=True)
    item.card_backup.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, item.card_backup)
    return item, was_there, camera


def dry_run_card(card: Path, sources: list[Path], album_dir: Path, card_backup_dir: Path,
                 log=print) -> list[ImportItem]:
    """Show the new names, reading capture times directly on the card."""
    items = []
    with ExifTool() as exif:
        for src in sources:
            capture_time = exif.capture_time(src)
            if not capture_time:
                log(f"  {src.relative_to(card)} -> no usable capture time, the import would stop here")
                continue
            name = new_name(capture_time, src)
            items.append(ImportItem(src, album_dir / name, card_backup_dir / name))
            log(f"  {src.relative_to(card)} -> {name}")
    warn_remaining(card, set(sources), True, log)
    return items


def warn_remaining(card: Path, imported: set[Path], dry_run: bool, log=print) -> None:
    """Warn about files in DCIM that are (or would be) left behind by the import."""
    remaining = sorted(p for p in (card / "DCIM").rglob("*") if p.is_file() and p not in imported)
    if remaining:
        verb = "would remain" if dry_run else "remain"
        log(f"Warning: {len(remaining)} file(s) {verb} in {card / 'DCIM'}:")
        for path in remaining:
            log(f"  {path.relative_to(card)}")


def import_and_backup(topic: str, album: str, card: Path | None = None, drive: Path | None = None,
                      create_album: bool = False, eject_cards: bool = False, dry_run: bool = False,
                      log=print, card_progress: Callable[[Path], Progress | None] | None = None,
                      backup_progress: Progress | None = None, remove_drive: bool = False,
                      on_backup_drive: Callable[[Path], None] | None = None,
                      on_card_done: Callable[[Path], None] | None = None) -> list[ImportItem]:
    """import_only, with backup_raw running in parallel.

    The backup drive may be connected later; the backup starts when it appears,
    and at the end this waits for it if needed. If the backup fails midway, the
    import still goes on; the backup error is raised at the end.
    """
    if dry_run:
        items = import_only(topic, album, card=card, create_album=create_album, dry_run=True, log=log)
        if config.album_dir(topic, album).is_dir():
            backup.backup_raw(topic, album, drive=drive, dry_run=True, log=log)
        log("All photos would be backed up in parallel to the import")
        return items

    parallel = backup.ParallelBackup(topic, album, drive, log=log, progress=backup_progress,
                                     on_drive=on_backup_drive)
    parallel.start()
    try:
        items = import_only(topic, album, card=card, create_album=create_album, eject_cards=eject_cards,
                            log=log, card_progress=card_progress, on_card=parallel.expect,
                            on_photo=parallel.arrived, on_card_done=on_card_done)
    except BaseException:
        log("Import failed; letting the backup finish what is already on the PC...")
        error = parallel.finish(abort=True)
        if error:
            log(f"Backup failed too: {error}")
        raise
    error = parallel.finish()
    if error:
        raise AlbumflowError(f"Import done, but the backup failed: {error}\n"
                             "The photos are safe on the PC and the card; run backup-raw again later.")
    album_dir = config.album_dir(topic, album)
    if clock.has_measurements(album_dir):  # the originals are on the backup now
        dirs = backup.album_backup_dirs(parallel.drive, topic, album) if parallel.drive else []
        clock.apply(album_dir, dirs, log=log)
    if remove_drive and parallel.drive:
        backup.remove_backup_drive(parallel.drive, log)
    return items


@dataclass
class OldBackup:
    """A folder in the card's Backup folder from an earlier import."""
    path: Path
    mtime: float
    size: int
    files: dict[str, int]  # name -> size


def free_fraction(card: Path) -> float:
    usage = shutil.disk_usage(card)
    return usage.free / usage.total


def old_backups(card: Path, album: str) -> list[OldBackup]:
    """The folders in the card's Backup folder other than the album's, oldest first."""
    root = card / config.CARD_BACKUP_DIR
    found = []
    for folder in (root.iterdir() if root.is_dir() else []):
        if folder.is_dir() and folder.name.lower() != album.lower():
            files = {p.name: p.stat().st_size for p in folder.rglob("*") if p.is_file()}
            found.append(OldBackup(folder, folder.stat().st_mtime, sum(files.values()), files))
    return sorted(found, key=lambda b: b.mtime)


def missing_on(backup: OldBackup, folders: list[Path]) -> int:
    """How many of the old backup's files are in none of the folders (by name and size)."""
    return sum(1 for name, size in backup.files.items()
               if not any((folder / name).is_file() and (folder / name).stat().st_size == size for folder in folders))

"""Back up an album's raw originals to the external backup drive, and from there to the secondary one.

Only adds files, never deletes or overwrites: photos culled on the PC later
stay on the backup. update-secondary only moves files on the backup drive,
once they are on the secondary. Only purging deletes: finished albums, chosen
by hand.
"""

from __future__ import annotations

import math
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from albumflow import annex, config
from albumflow import clock
from albumflow.clock import split_name
from albumflow.files import AlbumflowError, ByteCounter, Progress, copy_file, drives, poll_until, remove_device


POLL_SECONDS = 2.0  # how often to look for the backup drive


def set_backed_up(album_dir: Path) -> None:
    """Clear the album's "backup pending" mark: all its originals are on the backup drive now."""
    state = clock.AlbumState(album_dir)
    if state.backup_pending:
        state.backup_pending = False
        state.save()


def pending_albums() -> list[tuple[str, str]]:
    """(topic, album) of the albums with photos imported but maybe not yet on the backup drive."""
    found = []
    for state_file in sorted(config.PICTURES_NEW.glob(f"*/*/{clock.STATE_FILE}")):
        if clock.AlbumState(state_file.parent).backup_pending:
            found.append((state_file.parent.parent.name, state_file.parent.name))
    return found


def find_backup_drive(drive: Path | None = None) -> Path | None:
    """The given drive if it is connected, else the one with the marker file; None if not connected."""
    if drive is not None:
        return drive if drive.exists() else None
    found = [d for d in drives() if (d / config.BACKUP_MARKER).is_file()]
    if len(found) > 1:
        raise AlbumflowError(f"Several backup drives found: {', '.join(str(d) for d in found)}")
    return found[0] if found else None


def wait_for_backup_drive(drive: Path | None = None, log=print, stop: threading.Event | None = None) -> Path | None:
    """Wait until the backup drive is connected. Returns None if stop is set first."""
    found = find_backup_drive(drive)
    if found:
        return found
    what = drive or f"a drive with a {config.BACKUP_MARKER} file"
    log(f"Waiting for the backup drive ({what})...")
    found = poll_until(lambda: find_backup_drive(drive), POLL_SECONDS, stop)
    if found:
        log(f"Backup drive {found} connected.")
    return found


def backup_dir(drive: Path, topic: str, album: str) -> Path:
    """Where the album's originals are copied to."""
    return drive / config.BACKUP_DIR / topic / album


def copied_dir(drive: Path, topic: str, album: str) -> Path:
    """Where the album's originals are on the backup drive once copied to the secondary backup drive."""
    return drive / config.COPIED_DIR / topic / album


def album_backup_dirs(drive: Path, topic: str, album: str) -> list[Path]:
    """Where the album's originals may be on the backup drive: still waiting for the copy to the secondary
    backup drive, or already copied (photos backed up after that copy wait again)."""
    return [backup_dir(drive, topic, album), copied_dir(drive, topic, album)]


def backup_dirs_named(drive: Path, album: str) -> list[Path]:
    """The album folders with this name on the backup drive, in any topic, copied to the secondary or not."""
    return [folder for root in (config.BACKUP_DIR, config.COPIED_DIR)
            for folder in sorted((drive / root).glob(f"*/{album}")) if folder.is_dir()]


def safe_dirs_named(album: str) -> list[Path]:
    """The folders with this album name, in any topic, whose files count as safe: those on the secondary
    backup drive (also those moved to COPIED_DIR on the backup drive, which happens only once they are on the
    secondary) and those in the cloud. Only what is connected is looked at."""
    roots = []
    drive, secondary = find_backup_drive(), find_secondary_drive()
    if drive:
        roots.append(drive / config.COPIED_DIR)
    if secondary:
        roots.append(secondary / config.SECONDARY_DIR)
    if config.CLOUD_PICTURES.is_dir():
        roots.append(config.CLOUD_PICTURES)
    return [folder for root in roots for folder in sorted(root.glob(f"*/{album}")) if folder.is_dir()]


def on_backup(dirs: list[Path], name: str) -> Path | None:
    """The file name in the first of dirs that has it, None if none has."""
    return next((d / name for d in dirs if (d / name).is_file()), None)


def missing_on_backup(source_dir: Path, dest_dir: Path, also: list[Path] = ()) -> list[tuple[Path, Path]]:
    """(source, destination in dest_dir) of every original in source_dir not yet on the backup, i.e. in
    neither dest_dir nor the folders in also."""
    dirs = [dest_dir, *also]
    todo = []
    for src in sorted(source_dir.iterdir()):
        if not (src.is_file() and src.suffix.lower() in config.CAMERA_SUFFIXES):
            continue  # also skips ".part" files of copies in progress
        corrected, import_name = split_name(src.name)
        if corrected:
            # Clock-corrected on the PC; the backup keeps the original under its import name.
            if not on_backup(dirs, import_name):
                raise AlbumflowError(f"{src.name} was clock-corrected, but its original {import_name} "
                                     f"is not on the backup drive")
            continue
        dest = on_backup(dirs, src.name)
        if dest is None:
            todo.append((src, dest_dir / src.name))
        # Comparing sizes keeps re-runs fast; a partly written copy never has the final name.
        elif src.stat().st_size != dest.stat().st_size:
            raise AlbumflowError(f"{dest} already exists with a different size than {src}")
    return todo


def backup_new(topic: str, album: str, drive: Path | None = None, dry_run: bool = False,
               log=print, progress: Progress | None = None, remove_drive: bool = False) -> None:
    """Copy the album's originals missing on the backup drive; with remove_drive, remove it at the end."""
    source_dir = config.album_dir(topic, album)
    if not source_dir.is_dir():
        raise AlbumflowError(f"Album folder not found: {source_dir}")
    if dry_run:
        drive = find_backup_drive(drive)
        if drive is None:
            log("The backup drive is not connected; the backup would wait for it.")
            return
    else:
        drive = wait_for_backup_drive(drive, log)
    dest_dir = backup_dir(drive, topic, album)

    todo = missing_on_backup(source_dir, dest_dir, [copied_dir(drive, topic, album)])
    log(f"{len(todo)} originals in {source_dir} to back up to {dest_dir}")
    if dry_run:
        for src, _ in todo:
            log(f"  {src.name}")
        return
    counter = ByteCounter(sum(src.stat().st_size for src, _ in todo), progress)
    for src, dest in todo:
        counter.start(src.name)
        copy_file(src, dest, counter.add)
    log("Backup done.")
    set_backed_up(source_dir)
    if remove_drive:
        remove_backup_drive(dest_dir, log)


def remove_backup_drive(path: Path, log=print) -> None:
    """Safely remove the backup drive; failing is only a warning, the backup itself succeeded."""
    try:
        remove_device(Path(path.anchor))
        log(f"Removed {path.anchor}; it can be unplugged.")
    except AlbumflowError as e:
        log(f"Warning: {e}. Please remove it by hand.")


class ParallelBackup:
    """Backs up an album in a background thread while it is still being imported.

    The thread repeatedly copies whatever is on the PC but not yet on the
    backup, until the import is finished and everything is copied. The import
    announces the size of each card's photos with expect() and each photo that
    has arrived on the PC with arrived(), so the progress total also covers
    photos that are still on the card.

    The backup drive may be connected later: the backup starts once it appears.
    on_drive(drive) is called then (e.g. to label a progress bar).

    An error stops only the backup; it is kept in .error, and the import goes on.
    """

    def __init__(self, topic: str, album: str, drive: Path | None = None, log=print,
                 progress: Progress | None = None, poll_seconds: float = 1.0,
                 on_drive: Callable[[Path], None] | None = None):
        self.topic, self.album = topic, album
        self.source_dir = config.album_dir(topic, album)
        self.wanted_drive = drive
        self.drive: Path | None = None
        self.dest_dir: Path | None = None
        self.log, self.progress, self.poll_seconds, self.on_drive = log, progress, poll_seconds, on_drive
        self._abort = threading.Event()
        self.error: Exception | None = None
        self._expected = 0  # bytes announced by the import
        self._arrived = 0  # of these, bytes that have arrived on the PC
        self._lock = threading.Lock()
        self._import_finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backup", daemon=True)
        self._done = 0

    def start(self) -> None:
        self._thread.start()

    def expect(self, nbytes: int) -> None:
        with self._lock:
            self._expected += nbytes

    def arrived(self, nbytes: int) -> None:
        with self._lock:
            self._arrived += nbytes

    def finish(self, abort: bool = False) -> Exception | None:
        """Tell the backup the import is over, wait until it is done, return its error if any.

        If the backup drive isn't connected yet, this waits for it, unless abort.
        """
        if abort:
            self._abort.set()
        self._import_finished.set()
        while self._thread.is_alive():
            self._thread.join(0.5)  # in steps, so Ctrl+C works while waiting
        return self.error

    def _still_on_card(self) -> int:
        with self._lock:
            return self._expected - self._arrived

    def _run(self) -> None:
        try:
            self.drive = wait_for_backup_drive(self.wanted_drive, self.log, stop=self._abort)
            if self.drive is None:
                return  # import failed before the drive was connected
            self.dest_dir = backup_dir(self.drive, self.topic, self.album)
            if self.on_drive:
                self.on_drive(self.drive)
            while True:
                # Read the flag before scanning: once the import is finished,
                # no new files appear, so this scan is the last one needed.
                finished = self._import_finished.is_set()
                # Photos arriving after this point are not in todo, so they
                # are counted as still on the card until the next scan.
                still_on_card = self._still_on_card()
                copied = [copied_dir(self.drive, self.topic, self.album)]
                todo = missing_on_backup(self.source_dir, self.dest_dir, copied) if self.source_dir.is_dir() else []
                remaining = sum(src.stat().st_size for src, _ in todo)
                for src, dest in todo:
                    self._report(remaining + still_on_card, src.name)
                    copy_file(src, dest)
                    size = src.stat().st_size
                    self._done += size
                    remaining -= size
                    self._report(remaining + still_on_card, src.name)
                if finished:
                    if self._done:
                        self.log(f"Backup done: {self.dest_dir}")
                    return
                if not todo:
                    self._import_finished.wait(self.poll_seconds)
        except Exception as e:  # noqa: BLE001 - reported by finish()
            self.error = e

    def _report(self, left: int, name: str) -> None:
        if self.progress:
            self.progress(self._done, self._done + max(left, 0), name)


# --- update-secondary: the albums waiting on the backup drive, to the secondary backup drive ---

def find_secondary_drive() -> Path | None:
    """The drive with the secondary backup marker file; None if not connected."""
    found = [d for d in drives() if (d / config.SECONDARY_BACKUP_MARKER).is_file()]
    if len(found) > 1:
        raise AlbumflowError(f"Several secondary backup drives found: {', '.join(str(d) for d in found)}")
    return found[0] if found else None


def free_space(drive: Path) -> int:
    return shutil.disk_usage(drive).free


@dataclass
class WaitingAlbum:
    """An album not yet (completely) on the secondary: in the backup drive's BACKUP_DIR, or, for the
    cloud, also an album pending a backup, on the PC."""
    name: str  # <topic>\<album>
    source: Path  # on the backup drive, in BACKUP_DIR (or the album folder on the PC)
    copied: Path | None  # on the backup drive, in COPIED_DIR (None for an album on the PC)
    dest: Path  # on the secondary backup drive, or in the cloud
    todo: list[tuple[Path, Path]]  # (source, destination) of the files not yet on the secondary

    @property
    def size(self) -> int:
        return sum(src.stat().st_size for src, _ in self.todo)


def album_files(folder: Path) -> list[Path]:
    """All files below folder, without the ".part" files of interrupted copies."""
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix != ".part")


def waiting_albums(drive: Path, dest_root: Path) -> list[WaitingAlbum]:
    """The albums waiting on the backup drive, with what dest_root\\<topic>\\<album> lacks of them."""
    root = drive / config.BACKUP_DIR
    albums = []
    for source in sorted(root.glob("*/*")):
        if not source.is_dir():
            continue
        relative = source.relative_to(root)
        dest = dest_root / relative
        todo = []
        for src in album_files(source):
            target = dest / src.relative_to(source)
            if not target.exists():
                todo.append((src, target))
            elif target.stat().st_size != src.stat().st_size:
                raise AlbumflowError(f"{target} already exists with a different size than {src}")
        albums.append(WaitingAlbum(str(relative), source, drive / config.COPIED_DIR / relative, dest, todo))
    return albums


def move_to_copied(album: WaitingAlbum) -> None:
    """Once every file of the album is on the secondary (checked by size), move it on the backup drive from
    BACKUP_DIR to COPIED_DIR, joining what an earlier update-secondary moved there."""
    files = album_files(album.source)
    for src in files:
        target = album.dest / src.relative_to(album.source)
        if not (target.is_file() and target.stat().st_size == src.stat().st_size):
            raise AlbumflowError(f"{src} is not on the secondary backup drive as {target}")
    for src in files:
        target = album.copied / src.relative_to(album.source)
        if target.exists():
            if target.stat().st_size != src.stat().st_size:
                raise AlbumflowError(f"{target} already exists with a different size than {src}")
            src.unlink()  # the same file, e.g. of a move interrupted before
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            src.replace(target)
    for part in album.source.rglob("*.part"):  # incomplete copies; backup-new copies those files again
        part.unlink()
    for folder in sorted((p for p in album.source.rglob("*") if p.is_dir()), reverse=True):
        folder.rmdir()
    album.source.rmdir()
    if not any(album.source.parent.iterdir()):
        album.source.parent.rmdir()  # the topic folder


def update_secondary(dry_run: bool = False, log=print, progress: Progress | None = None,
                     remove_drives: bool = False) -> None:
    """Copy the albums waiting on the backup drive to the secondary backup drive, then move them on the
    backup drive from BACKUP_DIR to COPIED_DIR. With remove_drives, remove both drives at the end."""
    drive, secondary = find_backup_drive(), find_secondary_drive()
    if drive is None:
        raise AlbumflowError(f"The backup drive (with a {config.BACKUP_MARKER} file) is not connected")
    if secondary is None:
        raise AlbumflowError(f"The secondary backup drive (with a {config.SECONDARY_BACKUP_MARKER} file) "
                             "is not connected")
    if drive == secondary:
        raise AlbumflowError(f"{drive} has the markers of both the backup and the secondary backup drive")
    dest_root = secondary / config.SECONDARY_DIR
    albums = waiting_albums(drive, dest_root)
    if _check_space(albums, dest_root, free_space(secondary), log) and not dry_run:
        for album in _copy(albums, progress):
            move_to_copied(album)
            log(f"  {album.name}: on the secondary, moved to {config.COPIED_DIR}")
    # The cloud only stands in for the secondary: what is on the secondary now goes from there
    delete_from_cloud(cloud_files_on(dest_root), "on the secondary now", dry_run, log)
    if remove_drives and not dry_run:
        remove_backup_drive(drive, log)
        remove_backup_drive(secondary, log)


def cloud_files_on(root: Path) -> list[Path]:
    """The files in the cloud whose photo is also below root (<topic>\\<album>\\<name>): with the same name
    and size, or, for a clock-corrected copy (<master time>-<import name>), its original by import name."""
    cloud = config.CLOUD_PICTURES
    found = []
    for path in sorted(cloud.rglob("*")) if cloud.is_dir() else []:
        if not path.is_file() or path.suffix == ".part":
            continue
        corrected, import_name = split_name(path.name)
        other = root / path.parent.relative_to(cloud) / import_name
        if other.is_file() and (corrected or other.stat().st_size == path.stat().st_size):
            found.append(path)
    return found


def delete_from_cloud(files: list[Path], reason: str, dry_run: bool, log) -> list[Path]:
    """Delete the files from the cloud (one line per album), and folders left empty. Returns the deleted."""
    cloud = config.CLOUD_PICTURES
    by_album: dict[Path, list[Path]] = {}
    for path in files:
        by_album.setdefault(path.parent, []).append(path)
    for folder, paths in by_album.items():
        size = sum(p.stat().st_size for p in paths)
        verb = "Would delete" if dry_run else "Deleting"
        log(f"{verb} from the cloud, {reason}: {folder.relative_to(cloud)}, {len(paths)} files, {size / 1e9:.1f} GB")
        if dry_run:
            continue
        for path in paths:
            path.unlink()
        for empty in (folder, folder.parent):
            if empty != cloud and empty.is_dir() and not any(empty.iterdir()):
                empty.rmdir()
    return [] if dry_run else files

def update_cloud(dry_run: bool = False, log=print, progress: Progress | None = None) -> None:
    """Copy what hasn't reached the secondary backup drive to CLOUD_PICTURES, which doubles for the
    secondary while that is out of reach: the albums waiting on the backup drive, and the photos of albums
    pending a backup, from the PC. The backup drive stays as it is.

    Kept photos (still in the new tree) come first. When space is short (CLOUD_RESERVE is always left free),
    rejected photos (no longer in the new tree) are deleted from the cloud, and copied only if there is room.
    """
    drive = find_backup_drive()
    cloud = config.CLOUD_PICTURES
    if not Path(cloud.anchor).exists():
        raise AlbumflowError(f"The cloud drive {cloud.anchor} is not available")
    if drive is None:
        log("The backup drive is not connected: only the albums pending a backup are copied, from the PC.")
    kept, rejected = [], []  # (source, destination)
    for album in cloud_todo(drive):
        if where := safe_elsewhere(album):
            log(f"  {album.name}: skipped, safe elsewhere ({where})")
            continue
        names = kept_names(album.dest.relative_to(cloud))
        for src, dest in album.todo:
            (kept if names is None or split_name(src.name)[1] in names else rejected).append((src, dest))
        if album.todo:
            log(f"  {album.name}: {len(album.todo)} files, {album.size / 1e9:.1f} GB to copy")
    kept_size, rejected_size = (sum(src.stat().st_size for src, _ in files) for files in (kept, rejected))
    free = free_space(Path(cloud.anchor)) - config.CLOUD_RESERVE
    log(f"{(kept_size + rejected_size) / 1e9:.1f} GB to copy to {cloud} ({rejected_size / 1e9:.1f} GB of it "
        f"rejected photos), {free / 1e9:.1f} GB free there (keeping {config.CLOUD_RESERVE / 1e9:.0f} GB free).")
    if not kept and not rejected:
        return
    room = free - kept_size  # for rejected photos: not what deleting rejected ones frees, which would only churn
    if kept_size + rejected_size > free:
        deletable = rejected_in_cloud()
        deletable_size = sum(p.stat().st_size for p in deletable)
        if kept_size > free + deletable_size:
            raise AlbumflowError(f"Not enough space in {cloud}: the kept photos need {kept_size / 1e9:.1f} GB, "
                                 f"{(free + deletable_size) / 1e9:.1f} GB could be made free by deleting the "
                                 "rejected ones there")
        delete_from_cloud(deletable, "rejected", dry_run, log)
    fitting = []
    for src, dest in rejected:
        if src.stat().st_size <= room:
            fitting.append((src, dest))
            room -= src.stat().st_size
    if len(fitting) < len(rejected):
        log(f"{len(rejected) - len(fitting)} rejected photos are not copied to the cloud: not enough room.")
    if not dry_run:
        _copy_files(kept + fitting, progress)
        log(f"Copied {len(kept) + len(fitting)} files to the cloud.")


def kept_names(album: Path) -> set[str] | None:
    """The import names of the photos of album (<topic>\\<album>) still in the new tree, in new or new-old,
    also when in the annex; None if the album isn't found there (e.g. renamed): then all count as kept."""
    for root in (config.PICTURES_NEW, config.PICTURES_NEW_OLD):
        for folder in (root / album, annex.link_of(root / album)):
            if folder.is_dir():
                return {split_name(p.name)[1] for p in clock.photo_files(folder)}
    return None


def rejected_in_cloud() -> list[Path]:
    """The photos in the cloud that are no longer in the new tree (in albums found there)."""
    cloud = config.CLOUD_PICTURES
    found = []
    for folder in sorted(cloud.glob("*/*")) if cloud.is_dir() else []:
        names = kept_names(folder.relative_to(cloud))
        if names is not None:
            found += [p for p in sorted(folder.iterdir())
                      if p.is_file() and p.suffix != ".part" and split_name(p.name)[1] not in names]
    return found


def _copy_files(files: list[tuple[Path, Path]], progress: Progress | None) -> None:
    counter = ByteCounter(sum(src.stat().st_size for src, _ in files), progress)
    for src, dest in files:
        counter.start(src.name)
        copy_file(src, dest, counter.add)


def safe_elsewhere(album: WaitingAlbum) -> str | None:
    """Where the album is safe though not on the secondary (config.SAFE_ELSEWHERE), for an album of
    cloud_todo; None if it isn't."""
    return config.SAFE_ELSEWHERE.get(str(album.dest.relative_to(config.CLOUD_PICTURES)))


def cloud_todo(drive: Path | None) -> list[WaitingAlbum]:
    """What the cloud lacks of the albums waiting on the backup drive (if connected), and of the photos of
    albums pending a backup, read from the PC."""
    cloud = config.CLOUD_PICTURES
    albums = waiting_albums(drive, cloud) if drive else []
    for topic, album in pending_albums():
        on_drive = drive / config.BACKUP_DIR / topic / album if drive else None
        todo = []
        for src in clock.photo_files(config.album_dir(topic, album)):
            corrected, _ = split_name(src.name)
            if corrected or (on_drive and (on_drive / src.name).is_file()):
                continue  # clock-corrected only once its original is on the backup drive; or copied from there
            target = cloud / topic / album / src.name
            if not target.exists():
                todo.append((src, target))
            elif target.stat().st_size != src.stat().st_size:
                raise AlbumflowError(f"{target} already exists with a different size than {src}")
        albums.append(WaitingAlbum(f"{topic}\\{album} (pending a backup, from the PC)",
                                   config.album_dir(topic, album), None, cloud / topic / album, todo))
    return albums


def _check_space(albums: list[WaitingAlbum], dest_root: Path, free: int, log) -> bool:
    """List what is to be copied; False if there is nothing to copy, an error if there isn't enough space."""
    total = sum(a.size for a in albums)
    if not albums:
        log(f"Nothing to copy to {dest_root}.")
        return False
    for album in albums:
        what = f"{len(album.todo)} files, {album.size / 1e9:.1f} GB to copy" if album.todo else "already copied"
        log(f"  {album.name}: {what}")
    log(f"{total / 1e9:.1f} GB to copy to {dest_root}, {free / 1e9:.1f} GB free there.")
    if total > free:
        raise AlbumflowError(f"Not enough space in {dest_root}; delete albums there that are finished")
    return True


def _copy(albums: list[WaitingAlbum], progress: Progress | None):
    """Copy the albums' missing files, yielding each album once it is complete."""
    counter = ByteCounter(sum(a.size for a in albums), progress)
    for album in albums:
        for src, dest in album.todo:
            counter.start(src.name)
            copy_file(src, dest, counter.add)
        yield album


# --- purging finished albums from the backup drives, to make room ---

@dataclass
class Purgeable:
    """A finished album on a backup drive: it is in ORIGINALS, which restic covers."""
    name: str  # <topic>\<album>
    folder: Path
    size: int
    newest: float  # modification time of its newest file
    score: float  # size, weighted by age, like the annex suggestions


def purge_roots() -> list[tuple[str, Path]]:
    """(description, folder of <topic>\\<album>) of the connected backup drives' album trees that may be
    purged: on the backup drive only what is on the secondary too, on the secondary everything."""
    roots = []
    drive, secondary = find_backup_drive(), find_secondary_drive()
    if drive:
        roots.append((f"the backup drive {drive}", drive / config.COPIED_DIR))
    if secondary:
        roots.append((f"the secondary backup drive {secondary}", secondary / config.SECONDARY_DIR))
    return roots


def is_finished(topic: str, album: str) -> bool:
    """Whether the album is in ORIGINALS, also if moved to the annex."""
    folder = config.ORIGINALS / topic / album
    return folder.is_dir() or annex.link_of(folder).exists()


def purgeable(root: Path, now: float | None = None) -> list[Purgeable]:
    """The finished albums below root, highest score first."""
    now = time.time() if now is None else now
    found = []
    for folder in sorted(root.glob("*/*")):
        if not folder.is_dir() or annex.is_link(folder) or not is_finished(folder.parent.name, folder.name):
            continue
        files = [p.stat() for p in folder.rglob("*") if p.is_file()]
        size, newest = sum(s.st_size for s in files), max((s.st_mtime for s in files), default=0.0)
        age = max(0.0, (now - newest) / 86400)
        score = size / 1e9 * (1 - math.exp(-age / config.ANNEX_AGE_DAYS))
        found.append(Purgeable(str(folder.relative_to(root)), folder, size, newest, score))
    return sorted(found, key=lambda p: p.score, reverse=True)


def purge(album: Purgeable, root: Path) -> None:
    annex.delete_tree(album.folder, root)
    if not any(album.folder.parent.iterdir()):
        album.folder.parent.rmdir()  # the topic folder

"""Command line entry point."""

from __future__ import annotations

import argparse
import ctypes
import datetime
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Callable

from albumflow import (album_choice, annex, backup, card_import, clock, completion, config, gphotos_api,
                       gphotos_sync, lightroom)
from albumflow.exiftool import ExifTool
from albumflow.files import AlbumflowError, volume_label


def _api(interactive: bool = False) -> gphotos_api.GPhotos:
    return gphotos_api.GPhotos(gphotos_api.credentials(interactive=interactive))


def cmd_google_login(args) -> int:
    _api(interactive=True)
    print("Logged in to Google (Photos and Drive).")
    return 0


def cmd_gphotos_albums(args) -> int:
    for album in _api().list_albums():
        print(f'{album.get("mediaItemsCount", "0"):>6}  {album.get("title", "")}')
    return 0


def cmd_gphotos_sync(args) -> int:
    folder = Path(args.folder)
    title = args.title or folder.name
    return gphotos_sync.sync(_api(), folder, title, dry_run=args.dry_run)


def drive_description(drive: Path) -> str:
    """E.g. 'E: SONY64 (63.9 GB, 41% free)'."""
    usage = shutil.disk_usage(drive)
    label = volume_label(drive)
    return (f"{drive.drive}{' ' + label if label else ''} "
            f"({usage.total / 1e9:.1f} GB, {usage.free / usage.total:.0%} free)")


class _Bar:
    def __init__(self, label: str | Callable[[], str], last: bool, unit: str = "bytes"):
        self.describe, self.last, self.unit = label, last, unit
        self.label = label if isinstance(label, str) else ""
        self.done, self.total, self.reported = 0, 0, False
        self.start = time.monotonic()

    def refresh_label(self) -> None:
        if callable(self.describe):
            try:
                self.label = self.describe()
            except OSError:
                pass  # e.g. the drive is gone: keep the last description

    def line(self, columns: int, label_width: int) -> str:
        frac = self.done / self.total if self.total else 1.0
        rate = self.done / max(time.monotonic() - self.start, 1e-6)
        eta = int((self.total - self.done) / rate) if rate > 0 else 0
        width = max(10, min(30, columns - label_width - 45))
        filled = int(frac * width)
        if self.unit == "bytes":
            amount = f"{self.done / 1e9:.1f}/{self.total / 1e9:.1f} GB  {rate / 1e6:.0f} MB/s"
        else:
            amount = f"{self.done}/{self.total} {self.unit}  {rate:.0f}/s"
        text = (f"{self.label:<{label_width}}  [{'#' * filled}{'-' * (width - filled)}] {frac:4.0%}  "
                f"{amount}  {eta // 60}:{eta % 60:02d} left")
        return text[:columns]


class ProgressDisplay:
    """Progress bars on stderr, one line per task, redrawn in place.

    bar(label) returns a progress callback for the API; log() prints a message
    above the bars. Bars stay until the end, also when complete. Thread-safe,
    since import and backup report in parallel.
    """

    def __init__(self):
        self.bars: list[_Bar] = []
        self.drawn = 0
        self.last = 0.0
        self.lock = threading.Lock()
        self.paused = False  # while asking a question: no redraws
        _enable_ansi()

    def bar(self, label: str | Callable[[], str], last: bool = False, unit: str = "bytes"):
        """A progress callback for a new bar, shown once it reports.

        label may be a function, re-evaluated on each redraw (e.g. free space).
        Bars appear in creation order, those with last=True below all others.
        """
        bar = _Bar(label, last, unit)
        bar.refresh_label()
        with self.lock:
            self.bars.append(bar)

        def progress(done: int, total: int, name: str) -> None:
            with self.lock:
                if done < bar.done:  # started over
                    bar.start = time.monotonic()
                bar.done, bar.total, bar.reported = done, total, True
                if not self.paused and (time.monotonic() - self.last >= 0.2 or done >= total):
                    self._redraw()
        return progress

    def log(self, message: str = "") -> None:
        with self.lock:
            self._erase()
            sys.stderr.write(f"{message}\n")
            if not self.paused:
                self._redraw()

    def ask(self, prompt: str, prefill: str = "", complete=None) -> str:
        """Ask a question below the messages; the bars keep their state but aren't redrawn meanwhile.

        prefill is typed in for the user, who can edit it (Backspace, Esc clears).
        Where that isn't possible (no real console, e.g. Git Bash's mintty), the
        prefill is shown in brackets and complete(prefill, typed) builds the
        answer; by default, Enter takes the prefill.
        """
        with self.lock:
            self._erase()
            self.paused = True
        try:
            if prefill and _console_keys():
                return _edit_line(prompt, prefill)
            sys.stderr.write(f"{prompt}[{prefill}] " if prefill else prompt)
            sys.stderr.flush()
            try:
                typed = input()
            except EOFError:
                typed = ""
            if not prefill:
                return typed
            return complete(prefill, typed) if complete else (typed or prefill)
        finally:
            with self.lock:
                self.paused = False
                self._redraw()

    def _erase(self) -> None:
        if self.drawn:
            sys.stderr.write(f"\x1b[{self.drawn}F\x1b[J")  # up to the first bar line, clear below
            self.drawn = 0

    def _redraw(self) -> None:
        self._erase()
        columns = shutil.get_terminal_size().columns - 1
        shown = [b for b in self.bars if b.reported and not b.last] + [b for b in self.bars if b.reported and b.last]
        for bar in shown:
            bar.refresh_label()
        label_width = max((len(b.label) for b in shown), default=0)
        for bar in shown:
            sys.stderr.write(bar.line(columns, label_width) + "\n")
        self.drawn = len(shown)
        self.last = time.monotonic()
        sys.stderr.flush()


def _console_keys() -> bool:
    """Whether single key presses can be read: a real Windows console, not a pipe or mintty."""
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        return False
    return sys.stdin.isatty()


def ask_key(prompt: str, keys: str) -> str:
    """One key press out of keys (lower case); others are ignored, Esc counts as 'q'.

    Without a real console (e.g. Git Bash's mintty), a letter plus Enter instead.
    """
    if _console_keys():
        import msvcrt
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            ch = msvcrt.getwch()
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):  # arrow and function keys
                msvcrt.getwch()
                continue
            key = "q" if ch == "\x1b" and "q" in keys else ch.lower()
            if key in keys:
                print(key)
                return key
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return "q" if "q" in keys else keys[0]
        if len(answer) == 1 and answer in keys:
            return answer


def _edit_line(prompt: str, text: str) -> str:
    """Read a line with text already typed in. Backspace deletes, Esc clears, Enter accepts."""
    import msvcrt
    buf = list(text)
    sys.stderr.write(prompt + text)
    sys.stderr.flush()
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stderr.write("\n")
            return "".join(buf)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x00", "\xe0"):  # arrow and function keys: ignored
            msvcrt.getwch()
        elif ch == "\x08":
            if buf:
                buf.pop()
                sys.stderr.write("\b \b")
        elif ch == "\x1b":
            sys.stderr.write("\b \b" * len(buf))
            buf.clear()
        elif ch.isprintable():
            buf.append(ch)
            sys.stderr.write(ch)
        sys.stderr.flush()


def _enable_ansi() -> None:
    """Let the classic Windows console understand the cursor codes used above."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        pass


def _card_bar(display: ProgressDisplay):
    """Factory for card_progress: one bar per card, described while the card is still inserted."""
    return lambda card: display.bar(f"import {drive_description(card)}")


def parse_selection(answer: str, count: int) -> list[int] | None:
    """'1 3', '1,3', '2-4' -> sorted 1-based numbers; '' -> []; None if invalid."""
    chosen = set()
    for token in answer.replace(",", " ").split():
        low, _, high = token.partition("-")
        if not low.isdigit() or (high and not high.isdigit()):
            return None
        first, last = int(low), int(high or low)
        if not 1 <= first <= last <= count:
            return None
        chosen.update(range(first, last + 1))
    return sorted(chosen)


def offer_pruning(card: Path, album: str, log=print, ask=input, force: bool = False) -> None:
    """If the card is fuller than allowed, offer to delete old folders in its Backup folder.

    A folder is only deleted once it is safe, i.e. all its files are on the secondary backup drive or in
    the cloud (see backup.safe_dirs_named); with force, also before.
    """
    free = card_import.free_fraction(card)
    if free >= config.CARD_MIN_FREE:
        return
    olds = card_import.old_backups(card, album)
    if not olds:
        log(f"{card} has only {free:.0%} free, but there are no old backups on it to delete.")
        return
    try:
        ssd = backup.find_backup_drive()
        safe_dirs = {old.path.name: backup.safe_dirs_named(old.path.name) for old in olds}
    except AlbumflowError as e:
        log(f"Warning: {e}")
        ssd, safe_dirs = None, {old.path.name: [] for old in olds}
    log(f"{card} has only {free:.0%} free (less than {config.CARD_MIN_FREE:.0%}). "
        f"Old backups on the card, oldest first:")
    unsafe = set()
    for i, old in enumerate(olds, 1):
        missing = card_import.missing_on(old, safe_dirs[old.path.name])
        if not missing:
            status = "safe: on the secondary or in the cloud"
        else:
            unsafe.add(i)
            status = f"{missing} of {len(old.files)} files not yet on the secondary or in the cloud"
            if ssd is None:
                status += "; backup drive not connected"
            elif not_on_drive := card_import.missing_on(old, backup.backup_dirs_named(ssd, old.path.name)):
                status += f", {not_on_drive} not even on the backup drive"
        date = datetime.date.fromtimestamp(old.mtime)
        log(f"  {i}  {date:%Y-%m-%d}  {old.size / 1e9:6.1f} GB  {old.path.name}  ({status})")
    answer = ask("Delete which? (e.g. 1 2 or 1-3; Enter: none): ")
    selection = parse_selection(answer, len(olds))
    if selection is None:
        log(f"Not understood: {answer!r}; nothing deleted.")
        return
    deleted = False
    for i in selection:
        if i in unsafe and not force:
            log(f"Not deleted {olds[i - 1].path}: not yet safe (--force-prune deletes it anyway)")
            continue
        shutil.rmtree(olds[i - 1].path)
        log(f"Deleted {olds[i - 1].path}")
        deleted = True
    if deleted:
        log(f"{card} now has {card_import.free_fraction(card):.0%} free.")


class _BackupBar:
    """The backup's progress bar; its drive may only become known later."""

    def __init__(self, display: ProgressDisplay, drive: Path | None = None):
        self.drive = drive
        self.progress = display.bar(self._label, last=True)

    def set_drive(self, drive: Path) -> None:
        self.drive = drive

    def _label(self) -> str:
        return f"backup {drive_description(self.drive)}" if self.drive else "backup"


def cmd_import(args, ask=None) -> int:
    remind_pending()
    if not args.dry_run and backup.find_backup_drive(_path(args.drive)) is None:
        answer = (ask or ask_key)("The backup drive is not connected: [w]ait for it, or [c]ontinue without "
                                  "the backup? [w/c/q] ", "wcq")
        if answer == "q":
            raise KeyboardInterrupt
        if answer == "c":
            cmd_import_only(args)
            print(f"Not backed up yet: run albumflow backup-new \"{args.topic}/{args.album}\" once the backup "
                  "drive is connected. Until then the photos are only on the PC and the card(s).")
            return 0
    display = ProgressDisplay()
    bar = _BackupBar(display)
    card_import.import_and_backup(args.topic, args.album, card=_path(args.card), drive=_path(args.drive),
                                  create_album=args.create_album, eject_cards=not args.no_eject,
                                  dry_run=args.dry_run, log=display.log, card_progress=_card_bar(display),
                                  backup_progress=bar.progress, on_backup_drive=bar.set_drive,
                                  remove_drive=not args.no_eject, on_card_done=_pruning(args, display))
    return 0


def cmd_import_only(args) -> int:
    display = ProgressDisplay()
    card_import.import_only(args.topic, args.album, card=_path(args.card), create_album=args.create_album,
                            eject_cards=not args.no_eject, dry_run=args.dry_run, log=display.log,
                            card_progress=_card_bar(display), on_card_done=_pruning(args, display))
    return 0


def remind_pending(log=print) -> None:
    """One line naming the albums whose photos may not be on the backup drive yet."""
    pending = backup.pending_albums()
    if pending:
        log(f"Backup pending: {', '.join(f'{t}/{a}' for t, a in pending)} (details: albumflow backup-status)")


def _pruning(args, display: ProgressDisplay):
    return lambda card: offer_pruning(card, args.album, log=display.log, ask=display.ask, force=args.force_prune)


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _age(mtime: float) -> str:
    days = (datetime.date.today() - datetime.date.fromtimestamp(mtime)).days
    if days <= 0:
        return "modified today"
    if days == 1:
        return "modified yesterday"
    if days < 14:
        return f"modified {days} days ago"
    return f"modified {datetime.date.fromtimestamp(mtime):%Y-%m-%d}"


def album_candidates(query: str) -> list[album_choice.Album]:
    """The albums the album commands (import, backup-new, sync-clocks, ...) work on: not the sub-topic
    OnlyForOthers, which holds the leftovers of finished albums (their raws are on the backup drives
    already, under the album's own topic)."""
    try:
        topic, _ = album_choice.split_query(query)
    except ValueError as e:
        raise AlbumflowError(str(e)) from None
    if topic and album_choice.normalize(topic) == album_choice.normalize(config.ONLY_FOR_OTHERS):
        raise AlbumflowError(f"{config.ONLY_FOR_OTHERS} holds the leftovers of finished albums; "
                             "importing, backing up and clock syncing don't apply to it")
    return [a for a in album_choice.albums()
            if album_choice.normalize(a.topic) != album_choice.normalize(config.ONLY_FOR_OTHERS)]


def choose_album(query: str, create: bool, ask=None, out=print,
                 candidates: list[album_choice.Album] | None = None) -> tuple[str, str]:
    """Resolve a loosely typed "topic/album" to (topic, album), asking if it isn't exact.

    candidates: the albums to choose from (default: the albums in config.PICTURES_NEW).
    """
    candidates = album_choice.albums() if candidates is None else candidates
    ask = ask or input  # looked up now, not at import
    try:
        topic, name = album_choice.split_query(query)
    except ValueError as e:
        raise AlbumflowError(str(e)) from None
    hits = album_choice.exact(query, candidates)
    if len(hits) == 1:
        return hits[0].topic, hits[0].name

    options = album_choice.rank(query, candidates)
    new = None
    if create and name and not hits:
        if topic is None:
            raise AlbumflowError("To create a new album, give its topic too: topic/album")
        new = (topic, name)
        if not options:
            return new  # nothing similar exists, so no risk of a typo duplicate
    if not options:
        hint = "" if create else "; to start a new album, add -c"
        raise AlbumflowError(f"No album matches {query!r} in {config.PICTURES_NEW}{hint}")

    out("Which album?")
    for i, album in enumerate(options, 1):
        out(f"  {i}  {album}  ({_age(album.mtime)})")
    if new:
        out(f"  0  create new album {new[0]}/{new[1]}")
    out(f"  (not offered: albums matching {', '.join(config.EXCLUDED_ALBUM_PATTERNS)}, ignoring case)")
    default = "0" if new else "1"  # with -c, creating is what was asked for
    try:
        answer = ask(f"Number [{default}], q to quit: ").strip() or default
    except EOFError:
        answer = "q"
    if answer.lower() == "q":
        raise KeyboardInterrupt
    if new and answer == "0":
        out(f"New album: {new[0]}/{new[1]}")
        return new
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        chosen = options[int(answer) - 1]
        out(f"Album: {chosen}")
        return chosen.topic, chosen.name
    raise AlbumflowError("No album chosen")


def find_photo(album_dir: Path, query: str) -> Path:
    """The photo whose name contains query (case-insensitive); its RAW if there is one."""
    matches = [p for p in clock.photo_files(album_dir) if query.lower() in p.name.lower()]
    stems = {Path(clock.split_name(p.name)[1]).stem for p in matches}
    if not matches:
        raise AlbumflowError(f"No photo matching {query!r} in {album_dir}")
    if len(stems) > 1:
        shown = "\n  ".join(sorted(stems)[:10])
        raise AlbumflowError(f"{len(stems)} photos match {query!r}; be more specific:\n  {shown}")
    return sorted(matches, key=lambda p: p.suffix.lower() != ".arw")[0]


def _one_file_per_photo(album_dir: Path) -> list[Path]:
    """The album's photos in name order, one file each (the RAW if there are several)."""
    chosen: dict[str, Path] = {}
    for path in clock.photo_files(album_dir):
        if path.stem not in chosen or path.suffix.lower() == ".arw":
            chosen[path.stem] = path
    return sorted(chosen.values(), key=lambda p: p.name)


def parse_clock_time(text: str) -> datetime.time:
    """'09:28:54', '09 28 54', '9.28.54', '09:28' or '092854': any separator between the parts."""
    parts = re.findall(r"\d+", text)
    if len(parts) == 1 and len(parts[0]) in (4, 6):
        parts = re.findall(r"\d\d", parts[0])
    try:
        if len(parts) in (2, 3) and all(len(p) <= 2 for p in parts):
            return datetime.time(*(int(p) for p in parts))
    except ValueError:
        pass
    raise AlbumflowError(f"Not a time: {text!r} (expected hours, minutes and seconds, e.g. 09 28 54)")


def _complete_time(hour: str, typed: str) -> str:
    """Without an editable prefill: typed is the whole time, or just minutes and seconds."""
    return typed if len(re.findall(r"\d+", typed)) >= 3 else f"{hour} {typed}"



def record_reading(album_dir: Path, photo: Path, clock_time: datetime.time, exif: ExifTool, log=print) -> None:
    camera, _ = clock.measure(album_dir, photo, clock_time, exif)
    log(f"  {clock.camera_label(camera)}: {clock.offset_text(clock.AlbumState(album_dir), camera)}")


def cmd_sync_clocks(args) -> int:
    album_dir = config.album_dir(args.topic, args.album)
    display = ProgressDisplay()
    log = display.log
    readings = []
    for reading in args.reading or []:
        photo, sep, text = reading.rpartition("=")
        if not sep or not photo:
            raise AlbumflowError(f"Expected PHOTO=TIME, got {reading!r}")
        readings.append((find_photo(album_dir, photo), parse_clock_time(text)))

    if args.readings_from:  # e.g. another album of the same festival
        log(f"Clock readings from another album, for {args.topic}/{args.album}:")
        others = [a for a in album_choice.albums() if (a.topic, a.name) != (args.topic, args.album)]
        topic, album = choose_album(args.readings_from, create=False, candidates=others)
        taken, kept = clock.copy_readings(config.album_dir(topic, album), album_dir, f"{topic}/{album}")
        for camera in taken:
            log(f"  {clock.camera_label(camera)}: taken over from {topic}/{album}")
        for camera in kept:
            log(f"  {clock.camera_label(camera)}: keeps this album's own reading")

    with ExifTool() as exif:
        for photo, clock_time in readings:
            record_reading(album_dir, photo, clock_time, exif, log)
        if not (args.reading or args.assume_in_sync or args.readings_from):  # ask for clock readings
            files = _one_file_per_photo(album_dir)
            default = files[0].name if files else ""
            log("Clock readings: photo of the clock, then the time it shows.")
            while True:
                suggestion = f" [{default}]" if default else ""
                typed = display.ask(f"Clock photo{suggestion} (- to finish): ").strip()
                query = typed or default
                if typed == "-" or not query:
                    break
                try:
                    photo = find_photo(album_dir, query)
                    log(f"  {photo.name}")
                    hour = photo.name[9:11]  # the camera's hour; the clock rarely differs
                    clock_time = parse_clock_time(display.ask("Time on the clock: ", f"{hour} ", _complete_time))
                except AlbumflowError as e:
                    log(f"  {e}")
                    continue
                record_reading(album_dir, photo, clock_time, exif, log)
                later = [f for f in files if f.name > photo.name and f.stem != photo.stem]
                default = later[0].name if later else ""

        state = clock.AlbumState(album_dir)
        shots = clock.load_shots(album_dir, state, exif, log, progress=display.bar("cameras", unit="photos"))
    for wanted in args.assume_in_sync or []:
        matching = sorted({s.camera for s in shots if clock.model(s.camera).lower() == wanted.lower()})
        if not matching:
            raise AlbumflowError(f"No photos of a camera {wanted!r} in this album")
        state.assumed = sorted(set(state.assumed) | set(matching))
        state.save()

    counts: dict[str, int] = {}
    for s in shots:
        counts[clock.model(s.camera)] = counts.get(clock.model(s.camera), 0) + 1
    log("Cameras in this album: " + ", ".join(f"{m} ({n} photos)" for m, n in sorted(counts.items())))
    if state.clock or state.assumed:
        log("Clock readings (+: the camera's clock is ahead):")
        for line in clock.describe(state):
            log(line)
    else:
        log("No clock readings for this album yet.")
        return 0

    drive = backup.find_backup_drive(_path(args.drive))
    if drive is None:
        log("The backup drive is not connected: photos are only corrected once their originals are on it.")
    dirs = backup.album_backup_dirs(drive, args.topic, args.album) if drive else []

    def confirm(question: str) -> bool:
        return display.ask(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")

    clock.apply(album_dir, dirs, log=log, confirm=confirm, dry_run=args.dry_run)
    return 0


def looks_like_path(text: str) -> bool:
    """'.', './foo', '~/x', '/x', '\\x', 'C:...' are paths; anything else is a loose album query."""
    return text.startswith((".", "~", "/", "\\")) or bool(re.match(r"^[A-Za-z]:", text))


def as_path(text: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(text)))


def _annex_album_candidates() -> list[album_choice.Album]:
    """The albums in config.PICTURES_NEW that are currently in the annex."""
    found = []
    for folder in annex.annexed_folders(config.PICTURES_NEW):
        rel = folder.relative_to(config.PICTURES_NEW)
        if len(rel.parts) == 2:
            found.append(album_choice.Album(rel.parts[0], rel.parts[1], os.lstat(annex.link_of(folder)).st_mtime))
    return found


def to_annex_folder(target: str) -> Path:
    """The folder to move to the annex: a path, or a loose album query."""
    if looks_like_path(target):
        path = as_path(target)
        if annex.is_annex_link(path):
            raise AlbumflowError(f"{path.name} is already in the annex")
        return path
    topic, album = choose_album(target, create=False)
    return config.album_dir(topic, album)


def from_annex_folder(target: str) -> Path:
    """The folder to move back: its link or annex path, or a loose query among annexed albums."""
    if looks_like_path(target):
        path = as_path(target)
        if path.name.endswith(config.ANNEX_SUFFIX):
            return annex.folder_of(path)
        drive = annex.find_annex()
        if drive:
            try:
                return annex.home() / path.relative_to(Path(os.path.abspath(drive)))
            except ValueError:
                pass  # not a path in the annex
        return path
    candidates = _annex_album_candidates()
    if not candidates:
        raise AlbumflowError("No albums in the annex; for other folders, give their path")
    topic, album = choose_album(target, create=False, candidates=candidates)
    return config.album_dir(topic, album)


def _annex_bar(display: ProgressDisplay, direction: str):
    drive = annex.find_annex()
    label = f"{direction} {drive_description(drive)}" if drive else direction
    return display.bar(label)


def check_lightroom(paths: list[Path]) -> lightroom.Report:
    """Refuse (naming the folders) if moving would leave work only in the Lightroom catalog."""
    annex.check_no_editors()  # Lightroom closed, so its catalog is consistent
    report = lightroom.check(paths)
    lightroom.refuse_if_unsafe(report, annex.home())
    return report


def lightroom_hint(report: lightroom.Report, log) -> None:
    if report.photos:
        log(f"Lightroom has {report.photos} photos of this folder; reconnect it there: "
            "Library > Folders > right-click it > Find Missing Folder.")


def move_to_annex(folder: Path, display: ProgressDisplay, dry_run: bool) -> None:
    report = check_lightroom([folder])
    annex.to_annex(folder, log=display.log, progress=_annex_bar(display, "to annex"), dry_run=dry_run)
    if not dry_run:
        lightroom_hint(report, display.log)


def cmd_to_annex(args) -> int:
    display = ProgressDisplay()
    if args.rejected:
        if args.target:
            raise AlbumflowError("Give either a folder or --rejected, not both")
        folders = annex.rejected_to_drain()
        if not folders:
            display.log(f"Nothing to move: no files in {config.REJECTED_TREE} on {annex.home().drive}")
        for folder in folders:
            move_to_annex(folder, display, args.dry_run)
        return 0
    if not args.target:
        return suggest_for_annex(display, args.dry_run)
    move_to_annex(to_annex_folder(args.target), display, args.dry_run)
    return 0


def ago(timestamp: float, today: datetime.date | None = None) -> str:
    """How long ago: 'today', 'yesterday', '5 days', '3 weeks', '7 months', '2 years'."""
    if not timestamp:
        return "-"
    days = ((today or datetime.date.today()) - datetime.date.fromtimestamp(timestamp)).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    for limit, unit, size in ((14, "day", 1), (60, "week", 7), (730, "month", 30.44)):
        if days < limit:
            return f"{round(days / size)} {unit}s"
    return f"{round(days / 365.25)} years"


def suggest_for_annex(display: ProgressDisplay, dry_run: bool, ask=input) -> int:
    """Propose the folders whose move frees most space, weighted by age; move the chosen ones."""
    home = annex.home()
    print("Looking for candidates in " + ", ".join(config.ANNEX_SUGGEST_ROOTS) + " ...")
    candidates = annex.suggestions([home / r for r in config.ANNEX_SUGGEST_ROOTS])[:config.ANNEX_SUGGESTIONS]
    if not candidates:
        print("No candidates found.")
        return 0
    try:
        reports = lightroom.check_each([c.folder for c in candidates])
    except AlbumflowError as e:
        print(f"Warning: {e}; Lightroom is checked again before each move.")
        reports = [None] * len(candidates)

    usage = shutil.disk_usage(home)
    print(f"\nSuggestions for the annex ({home.drive} has {usage.free / usage.total:.0%} free):\n")
    print(f"{'':>2} {'GB':>4}  {'idle':<9}  album")
    for i, (c, report) in enumerate(zip(candidates, reports), 1):
        warning = ""
        if report and report.unsaved:
            warning = f"  ! Lightroom: {sum(report.unsaved.values())} unsaved"
        if report and report.virtual:
            warning += f"  ! Lightroom: {sum(report.virtual.values())} virtual copies"
        print(f"{i:>2} {c.size / 1e9:4.0f}  {ago(c.newest):<9}  {c.folder.name}   "
              f"({c.folder.parent.relative_to(home)}){warning}")
    total = sum(c.size for c in candidates)
    print(f"\nMoving all {len(candidates)} would free {total / 1e9:.0f} GB "
          f"({home.drive} {usage.free / usage.total:.0%} -> {(usage.free + total) / usage.total:.0%} free).")
    answer = ask("Move which? (e.g. 1 3 or 1-4; Enter: none): ")
    selection = parse_selection(answer, len(candidates))
    if selection is None:
        print(f"Not understood: {answer!r}; nothing moved.")
        return 0
    failed = []
    for i in selection:
        folder = candidates[i - 1].folder
        try:
            move_to_annex(folder, display, dry_run)
        except AlbumflowError as e:
            display.log(f"Skipped {folder.relative_to(home)}: {e}")
            failed.append(folder)
    if failed:
        display.log(f"{len(failed)} of {len(selection)} not moved, see above.")
        return 2
    return 0


def cmd_from_annex(args) -> int:
    display = ProgressDisplay()
    folder = from_annex_folder(args.target)
    paths = [folder, annex.link_of(folder)]  # Lightroom may know it under either, or in the annex
    drive = annex.find_annex()
    if drive:
        paths.append(drive / annex.relative(folder))
    report = check_lightroom(paths)
    annex.from_annex(folder, log=display.log, progress=_annex_bar(display, "from annex"), dry_run=args.dry_run)
    if not args.dry_run:
        lightroom_hint(report, display.log)
    return 0


def print_entry(e: annex.Entry, prefix: str = "") -> None:
    """Three lines: what it is, then (indented) its two sides."""
    linked = e.kind in ("ok", "broken link", "missing link")
    home_side = annex.link_of(e.home_path) if linked else e.home_path
    print(f"{prefix}{e.kind}")
    print(f"  home:  {home_side}")
    print(f"  annex: {e.annex_path if e.annex_path else '(nothing there)'}")


def cmd_annex_status(args) -> int:
    drive = annex.require_annex()
    entries = annex.status(drive)
    ok = [e for e in entries if not e.problem]
    problems = [e for e in entries if e.problem]
    for e in (ok if args.verbose else []) + problems:
        print_entry(e)
        print()
    counts = {kind: sum(1 for e in problems if e.kind == kind) for kind in dict.fromkeys(e.kind for e in problems)}
    summary = f"Annex {drive_description(drive)}: {len(ok)} folders linked correctly"
    if not problems:
        print(f"{summary}, no problems.")
        return 0
    fixable = [e for e in problems if e.kind == "missing link"]
    hints = {
        "missing link": "in the annex, but no link at its place in the home folder (--fix creates it)",
        "broken link": "a link whose folder in the annex is gone or elsewhere",
        "in both places": ("a folder that directly contains regular files (not only subfolders), "
                           "both in the home folder and in the annex; check by hand"),
        "interrupted": "an interrupted move; run the same to-annex/from-annex again to continue",
        "no folder on C:": ("its parent folder doesn't exist in the home folder, or it is directly in the "
                            "home folder and contains only subfolders; never linked automatically "
                            "(layout not mirrored?)"),
    }
    for kind in counts:
        print(f"  {kind}: {hints[kind]}")
    print()
    print(f"{summary}; {len(problems)} problem(s): " + ", ".join(f"{n} {kind}" for kind, n in counts.items()) + ".")
    if args.fix and fixable:
        print()
        print(f"{len(fixable)} missing link(s). For each: y = create, n = skip, "
              "a = create this and all remaining, q = stop.")
        create_all = False
        for i, e in enumerate(fixable, 1):
            if not create_all:
                print()
                print_entry(e, f"[{i}/{len(fixable)}] ")
                answer = ask_key("Create the link? [y/n/a/q] ", "ynaq")
                if answer == "q":
                    break
                if answer == "n":
                    continue
                create_all = answer == "a"
            annex.fix_link(e)
            print(f"  linked {annex.link_of(e.home_path)}")
    return 1


def cmd_backup_new(args) -> int:
    display = ProgressDisplay()
    drive = _path(args.drive)
    if not args.dry_run:
        drive = backup.wait_for_backup_drive(drive, display.log)
    progress = _BackupBar(display, drive).progress
    if not args.all:
        backup.backup_new(args.topic, args.album, drive=drive, dry_run=args.dry_run, log=display.log,
                          progress=progress, remove_drive=not args.no_eject)
        remind_pending(display.log)
        return 0
    drive = drive or backup.find_backup_drive()
    if drive is None:  # dry run
        display.log("The backup drive is not connected; the backup would wait for it.")
        return 0
    albums = []
    for album in album_candidates(""):
        source = config.album_dir(album.topic, album.name)
        if backup.missing_on_backup(source, backup.backup_dir(drive, album.topic, album.name),
                                    [backup.copied_dir(drive, album.topic, album.name)]):
            albums.append(album)
    display.log(f"{len(albums)} albums with originals not yet on the backup drive"
                + (": " + ", ".join(map(str, albums)) if albums else "."))
    for album in albums:
        display.log(f"{album}:")
        backup.backup_new(album.topic, album.name, drive=drive, dry_run=args.dry_run, log=display.log,
                          progress=progress)
    if not args.no_eject and not args.dry_run:
        backup.remove_backup_drive(drive, display.log)
    remind_pending(display.log)
    return 0


def cmd_update_secondary(args) -> int:
    display = ProgressDisplay()
    backup.update_secondary(dry_run=args.dry_run, log=display.log, progress=display.bar("to the secondary", last=True),
                            remove_drives=not args.no_eject)
    remind_pending(display.log)
    return 0


def cmd_update_cloud(args) -> int:
    display = ProgressDisplay()
    backup.update_cloud(dry_run=args.dry_run, log=display.log, progress=display.bar("to the cloud", last=True))
    remind_pending(display.log)
    return 0


def cmd_backup_status(args) -> int:
    """What isn't safe yet: photos not on the backup drive, and what hasn't reached the secondary or cloud."""
    drive = backup.find_backup_drive()
    print("Not on the backup drive yet:")
    if drive is None:
        print("  (backup drive not connected: the albums marked as pending a backup)")
        lines = [f"  {t}/{a}" for t, a in backup.pending_albums()]
    else:
        lines = []
        for album in album_candidates(""):
            source = config.album_dir(album.topic, album.name)
            try:
                todo = backup.missing_on_backup(source, backup.backup_dir(drive, album.topic, album.name),
                                                [backup.copied_dir(drive, album.topic, album.name)])
            except AlbumflowError as e:
                lines.append(f"  {album}: {e}")
                continue
            if todo:
                size = sum(src.stat().st_size for src, _ in todo)
                lines.append(f"  {album}: {len(todo)} photos, {size / 1e9:.1f} GB")
    print("\n".join(lines) if lines else "  nothing")
    print("Not yet on the secondary backup drive or in the cloud:")
    if drive is None:
        print("  (backup drive not connected: only the albums pending a backup, as on the PC)")
    lines, elsewhere = [], []
    for album in backup.cloud_todo(drive):
        if album.todo:
            line = f"  {album.name}: {len(album.todo)} files, {album.size / 1e9:.1f} GB"
            if where := backup.safe_elsewhere(album):
                elsewhere.append(f"{line} ({where})")
            else:
                lines.append(line)
    print("\n".join(lines) if lines else "  nothing")
    if elsewhere:
        print("Not yet on the secondary backup drive or in the cloud, but safe elsewhere "
              f"(SAFE_ELSEWHERE in {config.__file__}):")
        print("\n".join(elsewhere))
    return 0


def cmd_purge_backup(args, ask=input) -> int:
    roots = backup.purge_roots()
    if not roots:
        raise AlbumflowError("Neither backup drive is connected")
    for description, root in roots:
        candidates = backup.purgeable(root)
        usage = shutil.disk_usage(root.anchor)
        print(f"\nFinished albums (in {config.ORIGINALS}) on {description} "
              f"({usage.free / 1e9:.0f} GB, {usage.free / usage.total:.0%} free):")
        if not candidates:
            print("  none")
            continue
        print(f"{'':>2} {'GB':>4}  {'idle':<9}  album")
        for i, c in enumerate(candidates, 1):
            print(f"{i:>2} {c.size / 1e9:4.0f}  {ago(c.newest):<9}  {c.name}")
        answer = ask("Delete which? (e.g. 1 3 or 1-4; Enter: none): ")
        selection = parse_selection(answer, len(candidates))
        if selection is None:
            print(f"Not understood: {answer!r}; nothing deleted.")
            continue
        if not selection:
            continue
        chosen = [candidates[i - 1] for i in selection]
        size = sum(c.size for c in chosen)
        if ask(f"Type delete to delete {len(chosen)} albums ({size / 1e9:.0f} GB) from {description}: ").strip() != "delete":
            print("Nothing deleted.")
            continue
        for c in chosen:
            backup.purge(c, root)
            print(f"  deleted {c.name}")
    return 0


def cmd_completion(args) -> int:
    script = completion.powershell(build_parser(), ["albumflow", "af"])
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(script, encoding="utf-8")
    else:
        sys.stdout.write(script)
    return 0


class _HelpFormatter(argparse.HelpFormatter):
    """Lists the commands directly, without a line for <command> above them (as git and pip do)."""

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return "".join(self._format_action(a) for a in action._get_subactions())
        return super()._format_action(action)

    # Wrap at spaces only, so names like backup-new aren't split at their hyphen
    def _split_lines(self, text, width):
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)

    def _fill_text(self, text, width, indent):
        return textwrap.fill(" ".join(text.split()), width, initial_indent=indent, subsequent_indent=indent,
                             break_on_hyphens=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="albumflow", formatter_class=_HelpFormatter,
                                     epilog="albumflow <command> -h shows the options of a command.")
    parser._positionals.title = "commands"  # instead of "positional arguments", listed before the options
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add_command(*args, **kwargs):
        return sub.add_parser(*args, formatter_class=_HelpFormatter, **kwargs)  # not inherited from parser

    def album_args(p, card=False, drive=False):
        p.add_argument("album_query", metavar="topic/album", nargs="?", default="",
                       help="e.g. party/secret-castle-2026; may be abbreviated or misspelled, "
                            "you get a choice if it isn't exact (none: most recent albums)")
        if card:
            p.add_argument("--card", help="card drive, e.g. E:\\ (default: every inserted card, one after the other; waits for one)")
            p.add_argument("-c", "--create-album", action="store_true",
                           help="create the album folder if it doesn't exist yet")
            p.add_argument("--force-prune", action="store_true",
                           help="when a full card offers to delete old backups on it, also delete those not "
                                "yet on the secondary backup drive or in the cloud")
        if drive:
            p.add_argument("--drive", help=f"backup drive (default: the one with a {config.BACKUP_MARKER} file)")
        what = " and ".join(w for w, used in (("cards", card), ("the backup drive", drive)) if used)
        if what:
            p.add_argument("--no-eject", action="store_true", help=f"don't eject {what} when done")
        p.add_argument("--dry-run", action="store_true", help="only show what would happen")

    p = add_command("import", aliases=["i"], help="import-only, then backup-new")
    album_args(p, card=True, drive=True)
    p.set_defaults(func=cmd_import)

    p = add_command("import-only", aliases=["io"], help="import photos from the camera card, without backup")
    album_args(p, card=True)
    p.set_defaults(func=cmd_import_only)

    p = add_command("backup-new", aliases=["bn"],
                       help="copy an album's raw originals (in the new tree) to the backup drive")
    album_args(p, drive=True)
    p.add_argument("--all", action="store_true",
                   help="instead of one album: every album with originals not yet on the backup drive")
    p.set_defaults(func=cmd_backup_new)

    p = add_command("update-secondary",
                       help="copy the albums waiting on the backup drive to the secondary backup drive",
                       description="Copy the albums waiting on the backup drive (to-secondary) to the secondary "
                                   "backup drive, then move them on the backup drive to copied-to-secondary, and "
                                   "delete from the cloud what is on the secondary now. The backup tool runs it as "
                                   "part of its update-secondary. Whether the backup tool's backup-all also updates "
                                   "the cloud (update-cloud) is its setting backup_all_includes_update_cloud, "
                                   "in %APPDATA%\\backup\\config.json: after this, the cloud is no longer needed "
                                   "as a stand-in for the secondary.")
    p.add_argument("--no-eject", action="store_true", help="don't remove both backup drives when done")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_update_secondary)

    p = add_command("update-cloud", help="copy what hasn't reached the secondary backup drive to the cloud, "
                                            f"standing in for it ({config.CLOUD_PICTURES}): the albums waiting on "
                                            "the backup drive, and albums pending a backup, from the PC")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_update_cloud)

    p = add_command("backup-status", help="what isn't safe yet: photos not on the backup drive, and what "
                                             "hasn't reached the secondary backup drive or the cloud")
    p.set_defaults(func=cmd_backup_status)

    p = add_command("purge-backup", help="delete finished albums (those in Originals) from the connected "
                                         "backup drives, to make room; you choose, then confirm by typing 'delete'",
                    description="Delete finished albums from the connected backup drives, to make room. An album "
                                "is finished once it is in Originals (also when moved to the annex): restic backs "
                                "up what was kept of it. Offered per drive, the backup drive first: on the backup "
                                "drive only the albums in copied-to-secondary (what waits in to-secondary must "
                                "reach the secondary first), on the secondary all of its pictures-new. Listed with "
                                "size and how long untouched, large and old first. Choose by number (e.g. 1 3 or "
                                "1-4; Enter: none), then type 'delete' to confirm. Deleting loses the photos kept "
                                "nowhere else: the rejected ones, and those in new\\OnlyForOthers, which then exist "
                                "only on the laptop. The cloud and the SD cards aren't touched. No other command "
                                "runs this one.")
    p.set_defaults(func=cmd_purge_backup)

    p = add_command("sync-clocks", aliases=["sc"],
                       help="record clock readings; correct photos so all cameras' photos are in order")
    album_args(p)
    p.add_argument("-r", "--reading", action="append", metavar="PHOTO=TIME",
                   help="a clock photo (part of its name is enough) and the time the clock in it shows, "
                        "e.g. _DSC7115=09:28:54; repeat for each camera. Without any: asked for")
    p.add_argument("--drive", help=f"backup drive (default: the one with a {config.BACKUP_MARKER} file)")
    p.add_argument("--readings-from", metavar="topic/album",
                   help="take over the clock readings of another album, e.g. of the same festival (loose "
                        "topic/album like the album itself); this album's own readings are kept")
    p.add_argument("--assume-in-sync", action="append", metavar="MODEL",
                   help="a camera that took no clock photo, treated as in sync with the master, e.g. ILCE-7M3")
    p.set_defaults(func=cmd_sync_clocks)

    folder_help = "a path ('.', './foo', '~/Music/x', 'C:\\...'), or else a loose album query like 'party/secret'"
    p = add_command("to-annex", help="move a folder to the annex drive, leaving a '(annex)' link in its place",
                       description="Move a folder to the annex drive, leaving a '(annex)' link in its place. If the "
                                   "annex has the folder already, its files are merged into it. Below "
                                   f"{config.REJECTED_TREE}, only the files move: the folder stays, as the drop "
                                   "target for rejected photos while culling.")
    p.add_argument("target", nargs="?", default="", metavar="folder",
                   help=folder_help + "; none: suggestions (large and long untouched first) to choose from")
    p.add_argument("--rejected", action="store_true",
                   help=f"instead of a folder: move the files of every folder below {config.REJECTED_TREE} to the annex")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_to_annex)

    p = add_command("from-annex", help="move a folder back from the annex drive, replacing its link")
    p.add_argument("target", nargs="?", default="", metavar="folder",
                   help=folder_help + "; or its '(annex)' link; none: the most recent albums in the annex")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_from_annex)

    p = add_command("annex-status", help="list what is in the annex, and problems such as missing links")
    p.add_argument("--fix", action="store_true", help="create missing links (asks first)")
    p.add_argument("-v", "--verbose", action="store_true", help="also list the folders that are fine")
    p.set_defaults(func=cmd_annex_status)

    p = add_command("google-login", help="log in to Google, for Google Photos (gphotos-*) and Google Drive "
                                            "(deleting cloud copies permanently); opens the browser")
    p.set_defaults(func=cmd_google_login)

    p = add_command("gphotos-albums", help="list the Google Photos albums created by albumflow")
    p.set_defaults(func=cmd_gphotos_albums)

    p = add_command("gphotos-sync", help="make a Google Photos album an exact mirror of a folder")
    p.add_argument("-f", "--folder", required=True, help="folder with the photos to mirror")
    p.add_argument("--title", help="album title (default: folder name)")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_gphotos_sync)

    p = add_command("completion", help="print a tab completion script for a shell (PowerShell)")
    p.add_argument("shell", choices=["powershell"])
    p.add_argument("-o", "--output", help="write it to this file instead")
    p.set_defaults(func=cmd_completion)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Album titles and filenames may contain characters the Windows console
    # code page can't show; never crash on output.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    if not (sys.argv[1:] if argv is None else argv):  # no command: the same as -h
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    try:
        if getattr(args, "all", False) and args.album_query:
            raise AlbumflowError("Give either an album or --all, not both")
        if hasattr(args, "album_query") and not getattr(args, "all", False):
            args.topic, args.album = choose_album(args.album_query, getattr(args, "create_album", False),
                                                  candidates=album_candidates(args.album_query))
        return args.func(args)
    except (gphotos_api.ApiError, AlbumflowError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Ctrl+C: no traceback; moves and imports can simply be run again
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

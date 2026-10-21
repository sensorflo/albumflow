"""Command line entry point."""

from __future__ import annotations

import argparse
import ctypes
import datetime
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from albumflow import album_choice, backup, card_import, config, gphotos_api, gphotos_sync
from albumflow.files import AlbumflowError, volume_label


def _api(interactive: bool = False) -> gphotos_api.GPhotos:
    return gphotos_api.GPhotos(gphotos_api.credentials(interactive=interactive))


def cmd_gphotos_login(args) -> int:
    _api(interactive=True)
    print("Logged in to Google Photos.")
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
    def __init__(self, label: str | Callable[[], str], last: bool):
        self.describe, self.last = label, last
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
        text = (f"{self.label:<{label_width}}  [{'#' * filled}{'-' * (width - filled)}] {frac:4.0%}  "
                f"{self.done / 1e9:.1f}/{self.total / 1e9:.1f} GB  {rate / 1e6:.0f} MB/s  "
                f"{eta // 60}:{eta % 60:02d} left")
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

    def bar(self, label: str | Callable[[], str], last: bool = False):
        """A progress callback for a new bar, shown once it reports.

        label may be a function, re-evaluated on each redraw (e.g. free space).
        Bars appear in creation order, those with last=True below all others.
        """
        bar = _Bar(label, last)
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

    def ask(self, prompt: str) -> str:
        """Ask a question below the messages; the bars keep their state but aren't redrawn meanwhile."""
        with self.lock:
            self._erase()
            self.paused = True
            sys.stderr.write(prompt)
            sys.stderr.flush()
        try:
            return input()
        except EOFError:
            return ""
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


def offer_pruning(card: Path, album: str, log=print, ask=input) -> None:
    """If the card is fuller than allowed, offer to delete old folders in its Backup folder."""
    free = card_import.free_fraction(card)
    if free >= config.CARD_MIN_FREE:
        return
    olds = card_import.old_backups(card, album)
    if not olds:
        log(f"{card} has only {free:.0%} free, but there are no old backups on it to delete.")
        return
    try:
        ssd = backup.find_backup_drive()
    except AlbumflowError:
        ssd = None
    log(f"{card} has only {free:.0%} free (less than {config.CARD_MIN_FREE:.0%}). "
        f"Old backups on the card, oldest first:")
    for i, old in enumerate(olds, 1):
        if ssd is None:
            status = "backup drive not connected, can't check"
        else:
            missing = card_import.missing_on(old, backup.backup_dir(ssd, old.path.name))
            status = ("all on backup drive" if not missing
                      else f"{missing} of {len(old.files)} files NOT on backup drive")
        date = datetime.date.fromtimestamp(old.mtime)
        log(f"  {i}  {date:%Y-%m-%d}  {old.size / 1e9:6.1f} GB  {old.path.name}  ({status})")
    answer = ask("Delete which? (e.g. 1 2 or 1-3; Enter: none): ")
    selection = parse_selection(answer, len(olds))
    if selection is None:
        log(f"Not understood: {answer!r}; nothing deleted.")
        return
    for i in selection:
        shutil.rmtree(olds[i - 1].path)
        log(f"Deleted {olds[i - 1].path}")
    if selection:
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


def cmd_import(args) -> int:
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


def _pruning(args, display: ProgressDisplay):
    return lambda card: offer_pruning(card, args.album, log=display.log, ask=display.ask)


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


def choose_album(query: str, create: bool, ask=None, out=print) -> tuple[str, str]:
    """Resolve a loosely typed "topic/album" to (topic, album), asking if it isn't exact."""
    candidates = album_choice.albums()
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


def cmd_backup(args) -> int:
    display = ProgressDisplay()
    drive = _path(args.drive)
    if not args.dry_run:
        drive = backup.wait_for_backup_drive(drive, display.log)
    backup.backup_raw(args.topic, args.album, drive=drive, dry_run=args.dry_run, log=display.log,
                      progress=_BackupBar(display, drive).progress, remove_drive=not args.no_eject)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Album titles and filenames may contain characters the Windows console
    # code page can't show; never crash on output.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="albumflow",
                                     epilog="albumflow <subcommand> -h shows the options of a subcommand.")
    sub = parser.add_subparsers(dest="target", required=True)

    def album_args(p, card=False, drive=False):
        p.add_argument("album_query", metavar="topic/album", nargs="?", default="",
                       help="e.g. party/secret-castle-2026; may be abbreviated or misspelled, "
                            "you get a choice if it isn't exact (none: most recent albums)")
        if card:
            p.add_argument("--card", help="card drive, e.g. E:\\ (default: every inserted card, one after the other; waits for one)")
            p.add_argument("-c", "--create-album", action="store_true",
                           help="create the album folder if it doesn't exist yet")
        if drive:
            p.add_argument("--drive", help=f"backup drive (default: the one with a {config.BACKUP_MARKER} file)")
        what = " and ".join(w for w, used in (("cards", card), ("the backup drive", drive)) if used)
        if what:
            p.add_argument("--no-eject", action="store_true", help=f"don't eject {what} when done")
        p.add_argument("--dry-run", action="store_true", help="only show what would happen")

    p = sub.add_parser("import", aliases=["i"], help="import-only, then backup-raw")
    album_args(p, card=True, drive=True)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("import-only", aliases=["io"], help="import photos from the camera card, without backup")
    album_args(p, card=True)
    p.set_defaults(func=cmd_import_only)

    p = sub.add_parser("backup-raw", aliases=["br"], help="copy an album's raw originals to the backup drive")
    album_args(p, drive=True)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("gphotos-login", help="log in to Google Photos (opens the browser)")
    p.set_defaults(func=cmd_gphotos_login)

    p = sub.add_parser("gphotos-albums", help="list the Google Photos albums created by albumflow")
    p.set_defaults(func=cmd_gphotos_albums)

    p = sub.add_parser("gphotos-sync", help="make a Google Photos album an exact mirror of a folder")
    p.add_argument("-f", "--folder", required=True, help="folder with the photos to mirror")
    p.add_argument("--title", help="album title (default: folder name)")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_gphotos_sync)

    if not (sys.argv[1:] if argv is None else argv):  # no subcommand: the same as -h
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "album_query"):
            args.topic, args.album = choose_album(args.album_query, getattr(args, "create_album", False))
        return args.func(args)
    except (gphotos_api.ApiError, AlbumflowError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Ctrl+C: no traceback; imports can simply be run again
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

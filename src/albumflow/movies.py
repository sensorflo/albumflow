"""Movies in config.MOVIES, most of them on the movie annex: an external HDD, often disconnected.

Like the annex (see annex.py), the movie annex mirrors the home folder, and a moved-out movie leaves a junction at
its place. Unlike there, the junction has the movie's own name (a movie always moves as a whole, so its folder and
the junction never coexist) and points to the HDD's volume, not to its drive letter. So a movie's path never
changes, wherever it is, and EMDB's Location can always point to it on C:. Moves are as crash-safe as the annex's,
with the same move notes and checks.
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from albumflow import annex, config, emdb
from albumflow.annex import MOVE_NOTE, PART, check_copy, copy_tree, delete_tree, files_of, has_note, is_link, \
    junctions_inside, write_note
from albumflow.files import AlbumflowError, Progress, drives


# --- where things are ---

def find_hdd() -> Path | None:
    """The movie annex's root, found by its marker file; None if not connected."""
    override = os.environ.get("ALBUMFLOW_MOVIE_ANNEX")
    if override:
        return Path(override) if Path(override).is_dir() else None
    found = [d for d in drives() if (d / config.MOVIE_ANNEX_MARKER).is_file()]
    if len(found) > 1:
        raise AlbumflowError(f"Several movie annex drives found: {', '.join(map(str, found))}")
    return found[0] if found else None


def require_hdd() -> Path:
    hdd = find_hdd()
    if hdd is None:
        raise AlbumflowError(f"The movie annex is not connected (no drive with a {config.MOVIE_ANNEX_MARKER} file)")
    return hdd


def movies_dir() -> Path:
    return annex.home() / config.MOVIES


def on_hdd(hdd: Path) -> Path:
    return hdd / config.MOVIES


def volume_path(path: Path) -> str:
    """path with its drive as a volume (\\\\?\\Volume{...}\\), so a link to it survives a new drive letter. A path
    not on a drive root's volume (as in tests) stays as it is."""
    path = Path(os.path.abspath(path))
    root = Path(path.anchor)
    if os.environ.get("ALBUMFLOW_MOVIE_ANNEX") or not os.path.ismount(root):
        return str(path)
    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(str(root), buffer, len(buffer)):
        raise AlbumflowError(f"Can't find the volume of {root}: {ctypes.WinError()}")
    return buffer.value + str(path.relative_to(root))


def same_target(link: Path, target: str) -> bool:
    def normal(path: str) -> str:
        if path.startswith("\\\\?\\") and path[5:6] == ":":  # \\?\C:\... is C:\...; \\?\Volume{...} stays
            path = path[4:]
        return os.path.normcase(path.rstrip("\\"))
    return normal(os.readlink(link)) == normal(target)


def is_movie(path: Path) -> bool:
    return path.name not in config.NOT_MOVIES and not path.name.endswith(PART)


def movie_names() -> list[str]:
    """The movies on C: (folders) and on the movie annex (links), by name."""
    folder = movies_dir()
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and is_movie(p)) if folder.is_dir() else [],
                  key=str.lower)


# --- junctions to a volume ---

def create_link(link: Path, target: str) -> None:
    """A junction at link to target, which may be a volume path (\\\\?\\Volume{...}\\...); _winapi.CreateJunction
    can't take those, and mklink would need cmd's quoting."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    substitute = "\\??\\" + (target[4:] if target.startswith("\\\\?\\") else target)
    sub, shown = substitute.encode("utf-16-le"), target.encode("utf-16-le")
    paths = sub + b"\0\0" + shown + b"\0\0"
    data = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0, 0, len(sub), len(sub) + 2, len(shown)) + paths
    os.mkdir(link)
    handle = kernel32.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x00200000 | 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        os.rmdir(link)
        raise AlbumflowError(f"Can't create the link {link}: {error}")
    try:
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(wintypes.HANDLE(handle), 0x000900A4, data, len(data), None, 0,
                                      ctypes.byref(returned), None)
        error = ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    if not ok:
        os.rmdir(link)
        raise AlbumflowError(f"Can't create the link {link}: {error}")


# --- moving ---

def check_space(src: Path, dest: Path) -> None:
    annex.check_space(src, Path(dest.anchor))


def to_hdd(name: str, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move a movie (its folder in config.MOVIES) to the movie annex, leaving a link with its name."""
    hdd = require_hdd()
    folder, target = movies_dir() / name, on_hdd(hdd) / name
    part = target.with_name(target.name + PART)
    resuming = has_note(part) or has_note(target)
    if not resuming:
        if is_link(folder):
            raise AlbumflowError(f"{name} is on the movie annex already")
        if not folder.is_dir():
            raise AlbumflowError(f"{folder} is not a folder")
        if inside := junctions_inside(folder):
            raise AlbumflowError(f"{folder} contains a junction ({inside[0]})")
        if target.exists() or part.exists():
            raise AlbumflowError(f"{target} already exists on the movie annex; please check by hand")
        check_space(folder, hdd)
    log(f"{'Continuing to move' if resuming else 'Moving'} {folder} -> {target}")
    if dry_run:
        return target
    if not target.exists():  # copy (again) into .part
        write_note(part, "movie-to-annex", folder)
        copy_tree(folder, part, progress)
        check_copy(folder, part)
        part.rename(target)
    if folder.is_dir() and not is_link(folder):
        check_left(folder, target)
        delete_tree(folder, annex.home())
    if not folder.exists():
        create_link(folder, volume_path(target))
    (target / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {name} is on the movie annex; the link {folder} stands for it.")
    return target


def from_hdd(name: str, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move a movie back from the movie annex, replacing its link."""
    hdd = require_hdd()
    folder, source = movies_dir() / name, on_hdd(hdd) / name
    part = folder.with_name(folder.name + PART)
    real = folder.is_dir() and not is_link(folder)
    resuming = has_note(part) or (real and has_note(folder))
    if not resuming:
        if not source.is_dir():
            raise AlbumflowError(f"{name} is not on the movie annex ({source})")
        if real:
            raise AlbumflowError(f"{folder} is a folder on C: already; please check by hand")
        if part.exists():
            raise AlbumflowError(f"{part} already exists; please check by hand")
        check_space(source, folder)
    log(f"{'Continuing to move' if resuming else 'Moving'} {source} -> {folder}")
    if dry_run:
        return folder
    if not real:  # copy (again) into .part
        write_note(part, "movie-from-annex", source)
        copy_tree(source, part, progress)
        check_copy(source, part)
        if is_link(folder):
            os.rmdir(folder)  # removes only the junction, never what it points to
        part.rename(folder)
    if source.exists():
        check_left(source, folder)
        delete_tree(source, hdd)
    (folder / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {name} is back on C:.")
    return folder


def check_left(src: Path, copy: Path) -> None:
    """Before deleting src: each file in it is in the complete copy with the same size. An earlier delete may have
    stopped halfway (e.g. a file open in a player), leaving only some."""
    copied = files_of(copy)
    for rel, size in files_of(src).items():
        if copied.get(rel) != size:
            raise AlbumflowError(f"{src / rel} isn't in {copy} with the same size; nothing was deleted")


# --- status and fixes ---

@dataclass
class Entry:
    kind: str  # see KINDS
    name: str  # the movie's (or loose file's) name in config.MOVIES
    detail: str = ""

    @property
    def problem(self) -> bool:
        return self.kind not in ("on C:", "on the movie annex", "on the movie annex (not connected)")


KINDS = {
    "old layout": f"the movie annex has its movies in \\{config.MOVIE_ANNEX_OLD_ROOT}, not in \\{config.MOVIES}; --fix "
                  "moves them",
    "loose file": "a movie file not in a folder of its own; --fix puts it into one, named like it",
    "missing link": "on the movie annex, but no link at its place on C:; --fix creates it",
    "broken link": "a link whose folder on the movie annex is gone, or that points elsewhere",
    "in both places": "a folder on C: and on the movie annex; check by hand",
    "interrupted": "an interrupted move; run the same movie-to-annex/movie-from-annex again to continue",
    "Location outdated": "EMDB's Location points elsewhere, but the movie is here; --fix changes it",
    "Location not found": "EMDB's Location points to a file that is nowhere to be found",
    "not in EMDB": "no EMDB Location points into it (add the movie in EMDB, or fix its Location)",
}


def loose_files(folder: Path) -> list[Path]:
    """Movie files directly in folder; each needs a folder of its own, as a link can only stand for a folder."""
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in config.MOVIE_EXTENSIONS) \
        if folder.is_dir() else []


def wrap(file: Path) -> Path:
    """Move a loose movie file into a folder named like it (without the extension), together with the files named
    like it (e.g. its subtitles, x.en.srt). Returns the folder."""
    folder = file.with_name(file.stem)
    if folder.exists():
        raise AlbumflowError(f"Can't put {file.name} into a folder: {folder} exists already")
    companions = [p for p in file.parent.iterdir() if p.is_file() and p.name.startswith(file.stem + ".")]
    folder.mkdir()
    for p in companions:
        p.rename(folder / p.name)
    return folder


def status(hdd: Path | None) -> list[Entry]:
    """Every movie, by where it is, and the problems with the movie annex's layout and links."""
    entries: list[Entry] = []
    home_dir = movies_dir()
    if hdd and not on_hdd(hdd).is_dir() and (hdd / config.MOVIE_ANNEX_OLD_ROOT).is_dir():
        entries.append(Entry("old layout", config.MOVIE_ANNEX_OLD_ROOT, str(hdd / config.MOVIE_ANNEX_OLD_ROOT)))
        hdd = None  # the rest only once it mirrors the home folder
    entries += [Entry("loose file", p.name, str(p)) for p in loose_files(home_dir)]
    if hdd:
        entries += [Entry("loose file", p.name, str(p)) for p in loose_files(on_hdd(hdd))]
    seen = set()
    for path in (sorted(home_dir.iterdir()) if home_dir.is_dir() else []):
        if not (path.is_dir() and is_movie(path)):
            continue
        if has_note(path):
            entries.append(Entry("interrupted", path.name, str(path)))
        elif not is_link(path):
            both = hdd and (on_hdd(hdd) / path.name).is_dir()
            entries.append(Entry("in both places" if both else "on C:", path.name))
        elif not hdd:
            entries.append(Entry("on the movie annex (not connected)", path.name))
        else:
            target = on_hdd(hdd) / path.name
            seen.add(path.name.lower())
            ok = target.is_dir() and same_target(path, volume_path(target))
            entries.append(Entry("on the movie annex" if ok else "broken link", path.name, os.readlink(path)))
    if hdd:
        for path in sorted(on_hdd(hdd).iterdir()) if on_hdd(hdd).is_dir() else []:
            if path.is_dir() and path.name.endswith(PART):
                entries.append(Entry("interrupted", path.name[:-len(PART)], str(path)))
            elif path.is_dir() and is_movie(path) and path.name.lower() not in seen \
                    and not (home_dir / path.name).exists():
                entries.append(Entry("missing link", path.name, str(path)))
    return entries


def location_fixes(movies: list[emdb.Movie], hdd: Path | None) -> tuple[dict[int, str], list[Entry]]:
    """New Locations for EMDB's movies (by line), pointing to config.MOVIES on C:, and the problems found. Only
    complete with the movie annex connected; without, the Locations to it are taken as they are."""
    home_dir, changes, entries, used = movies_dir(), {}, [], set()
    for movie in movies:
        if not movie.location:
            continue
        path = emdb.without_label(movie.location)
        rel = relative_movie_path(path)
        if rel is None:
            continue  # not a movie in config.MOVIES, nor where they were
        top = home_dir / rel.parts[0]
        if not hdd and not (top.is_dir() and not is_link(top)):
            if is_link(top):
                used.add(rel.parts[0].lower())
            continue  # (maybe) on the movie annex, not connected: can't check
        candidates = [home_dir / rel] + ([home_dir / Path(rel.name).stem / rel] if len(rel.parts) == 1 else [])
        found = next((c for c in candidates if c.is_file()), None)
        if found is None:
            entries.append(Entry("Location not found", movie.title, movie.location))
        else:
            used.add(found.relative_to(home_dir).parts[0].lower())
            if str(found) != movie.location:
                changes[movie.line] = str(found)
                entries.append(Entry("Location outdated", movie.title, f"{movie.location} -> {found}"))
    for name in movie_names():
        if name.lower() not in used:
            entries.append(Entry("not in EMDB", name))
    return changes, entries


def relative_movie_path(path: str) -> Path | None:
    """A Location's path relative to config.MOVIES (on C:) or to where the movies were (config.EMDB_OLD_MOVIE_ROOTS);
    None if it is neither."""
    for root in [str(movies_dir()), *config.EMDB_OLD_MOVIE_ROOTS]:
        if path.lower().startswith(root.lower().rstrip("\\") + "\\"):
            rel = Path(path[len(root.rstrip("\\")) + 1:])
            return rel if rel.parts else None
    return None


def fix_layout(hdd: Path, log=print) -> None:
    """Move the movie annex's movies from the old root to config.MOVIES (a rename, on the same drive)."""
    old, new = hdd / config.MOVIE_ANNEX_OLD_ROOT, on_hdd(hdd)
    if new.exists():
        raise AlbumflowError(f"Both {old} and {new} exist; please check by hand")
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    log(f"Moved {old} -> {new}")

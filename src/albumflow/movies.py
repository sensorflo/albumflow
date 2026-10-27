"""Movies: the store holds them as downloaded, the view names them; EMDB follows the view.

The store, config.MOVIE_STORE, holds each movie as downloaded, <store>\\<release name>, on C: or on the movie annex
(an external HDD, often disconnected) at the same path. Its folders are never renamed, so qBittorrent can go on
seeding them. The view, config.MOVIE_VIEW, holds only junctions, one per movie, named canonically
(movienames.py): to the store on C: by its path, to the movie annex by its volume, so a new drive letter doesn't
matter. EMDB's Locations point through the view; the files are the master, EMDB follows them.

A movie moved by qBittorrent ("Set location") leaves its junction pointing to the old place; movie-status --fix
points it to where the movie is now. Moves by albumflow are as crash-safe as the annex's (annex.py), and point the
junction to the new place before the old copy is deleted.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from albumflow import annex, config, emdb, mediainfo, movienames
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


def c_store() -> Path:
    return annex.home() / config.MOVIE_STORE


def hdd_store(hdd: Path) -> Path:
    return hdd / config.MOVIE_STORE


def view_dir() -> Path:
    return annex.home() / config.MOVIE_VIEW


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


def link_target(release: str, hdd: Path | None) -> str:
    """What the view's junction to a movie points to: in the store on C: (hdd None), or on the movie annex."""
    return volume_path(hdd_store(hdd) / release) if hdd else str(c_store() / release)


def normal(path: str) -> str:
    if path.startswith("\\\\?\\") and path[5:6] == ":":  # \\?\C:\... is C:\...; \\?\Volume{...} stays
        path = path[4:]
    return os.path.normcase(path.rstrip("\\"))


def same_target(link: Path, target: str) -> bool:
    return normal(os.readlink(link)) == normal(target)


def release_of(target: str) -> str:
    """The release name a junction's target ends with."""
    return PureWindowsPath(target.rstrip("\\")).name


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


def retarget(link: Path, target: str) -> None:
    os.rmdir(link)  # removes only the junction, never what it points to
    create_link(link, target)


# --- the movies ---

@dataclass
class Movie:
    release: str  # its folder's name in the store
    on_c: bool = False
    on_hdd: bool = False  # always False while the movie annex isn't connected
    link: str | None = None  # its junction's name in the view
    target: str = ""  # what that junction points to

    @property
    def here(self) -> bool:
        """Whether its files can be read now."""
        return self.on_c or self.on_hdd

    def folder(self, hdd: Path | None) -> Path:
        return (c_store() if self.on_c else hdd_store(hdd)) / self.release


@dataclass
class Entry:
    kind: str  # see KINDS
    name: str  # a release or view name, or an EMDB title
    detail: str = ""

    @property
    def problem(self) -> bool:
        return self.kind not in OK_KINDS and self.kind not in HINT_KINDS


OK_KINDS = ("on C:", "on the movie annex", "not on C: (movie annex not connected)")
HINT_KINDS = ("long path", "language tag doubtful")
KINDS = {
    "old layout": f"the movie annex has its movies in \\{config.MOVIE_ANNEX_OLD_ROOT}, not in \\{config.MOVIE_STORE}; "
                  "--fix moves them",
    "loose file": "a movie file not in a folder of its own in the store; --fix puts it into one, named like it (a "
                  "torrent seeding it loses it; set qBittorrent's Torrent content layout to Create subfolder)",
    "missing link": "in the store, but no link in the view; --fix creates it",
    "link to fix": "a link in the view pointing elsewhere than the movie (e.g. after qBittorrent moved it); --fix "
                   "points it to the movie",
    "movie gone": "a link in the view to a movie that is in neither store; --fix removes the link",
    "not a link": "something in the view that isn't a link to a movie; check by hand",
    "two links": "a second link in the view to the same movie; check by hand",
    "in both places": "in the store on C: and on the movie annex; check by hand",
    "interrupted": "an interrupted move; run the same movie-to-annex/movie-from-annex again to continue",
    "name to change": "the link's name isn't the movie's canonical name (yet); --fix renames it",
    "Location outdated": "EMDB's Location points elsewhere than through the view (an old place, or a name that "
                         "changed); --fix changes it",
    "Location to set": "a movie that has an EMDB entry (by title and year), without a Location; --fix sets it",
    "Location to clear": "EMDB's Location points to a movie that is gone; --fix clears it (the entry stays)",
    "not in EMDB": "no EMDB entry found by its title and year; --fix offers that year's entries to choose from "
                   "(or add it in EMDB, then run --fix again)",
    "EMDB duplicate": "a second EMDB entry whose Location points to the same movie; check by hand",
    "no movie file": "a movie folder without a movie file",
    "unreadable": "MediaInfo finds no video in the movie file (damaged, or not a movie); not renamed",
    "long path": f"its Location is longer than {config.EMDB_MAX_PATH} characters, so EMDB can't play it",
    "language tag doubtful": "a track's language tag is missing or contradicts its title; fix it with MKVToolNix",
}


def releases_in(store: Path) -> list[Path]:
    return sorted(p for p in store.iterdir() if p.is_dir() and not is_link(p) and not p.name.endswith(PART)
                  and not has_note(p)) if store.is_dir() else []


def collect(hdd: Path | None) -> tuple[dict[str, Movie], list[Entry]]:
    """The movies in the stores and the view, by release name (lower case), and the problems found doing so."""
    movies: dict[str, Movie] = {}
    entries: list[Entry] = []
    for folder in releases_in(c_store()):
        movies[folder.name.lower()] = Movie(folder.name, on_c=True)
    for folder in releases_in(hdd_store(hdd)) if hdd else []:
        movies.setdefault(folder.name.lower(), Movie(folder.name)).on_hdd = True
    for path in sorted(view_dir().iterdir()) if view_dir().is_dir() else []:
        if not is_link(path):
            if path.name.lower() not in ("desktop.ini", "readme.txt"):
                entries.append(Entry("not a link", path.name, str(path)))
            continue
        target = os.readlink(path)
        movie = movies.setdefault(release_of(target).lower(), Movie(release_of(target)))
        if movie.link:
            entries.append(Entry("two links", path.name, f"{movie.link} points to {movie.release} too"))
            continue
        movie.link, movie.target = path.name, target
    return movies, entries


def status(hdd: Path | None) -> tuple[dict[str, Movie], list[Entry]]:
    """Every movie, by where it is, and the problems with the stores and the view."""
    entries: list[Entry] = []
    if hdd and not hdd_store(hdd).is_dir() and (hdd / config.MOVIE_ANNEX_OLD_ROOT).is_dir():
        entries.append(Entry("old layout", config.MOVIE_ANNEX_OLD_ROOT, str(hdd / config.MOVIE_ANNEX_OLD_ROOT)))
        hdd = None  # the rest only once it holds the store
    for store in [c_store()] + ([hdd_store(hdd)] if hdd else []):
        entries += [Entry("loose file", p.name, str(p)) for p in loose_files(store)]
        for path in sorted(store.iterdir()) if store.is_dir() else []:
            if path.is_dir() and (path.name.endswith(PART) or has_note(path)):
                entries.append(Entry("interrupted", path.name.removesuffix(PART), str(path)))
    movies, found = collect(hdd)
    entries += found
    for movie in sorted(movies.values(), key=lambda m: m.release.lower()):
        if movie.on_c and movie.on_hdd:
            entries.append(Entry("in both places", movie.release))
        elif not movie.link:
            entries.append(Entry("missing link", movie.release))
        elif not movie.here:
            entries.append(Entry("movie gone" if hdd else "not on C: (movie annex not connected)", movie.link,
                                 movie.target))
        elif not same_target(view_dir() / movie.link, link_target(movie.release, hdd if movie.on_hdd else None)):
            entries.append(Entry("link to fix", movie.link, movie.target))
        else:
            entries.append(Entry("on C:" if movie.on_c else "on the movie annex", movie.link))
    return movies, entries


def fix_links(movies: dict[str, Movie], entries: list[Entry], hdd: Path | None, log=print) -> None:
    """Create the missing links (named like the release, until a name is made), point links to where their movie
    is, and remove those of movies gone."""
    view_dir().mkdir(parents=True, exist_ok=True)
    by_link = {m.link: m for m in movies.values() if m.link}
    for e in entries:
        if e.kind == "missing link":
            movie = movies[e.name.lower()]
            link = free_name(view_dir(), movie.release)
            create_link(link, link_target(movie.release, hdd if movie.on_hdd else None))
            log(f"  linked {link.name}")
        elif e.kind == "link to fix":
            movie = by_link[e.name]
            retarget(view_dir() / e.name, link_target(movie.release, hdd if movie.on_hdd else None))
            log(f"  pointed {e.name} to {movie.folder(hdd)}")
        elif e.kind == "movie gone":
            os.rmdir(view_dir() / e.name)
            log(f"  removed {e.name}")


def free_name(folder: Path, name: str) -> Path:
    path, n = folder / name, 2
    while path.exists() or is_link(path):
        path, n = folder / f"{name} ({n})", n + 1
    return path


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


def fix_layout(hdd: Path, log=print) -> None:
    """Move the movie annex's movies from the old root to the store (a rename, on the same drive)."""
    old, new = hdd / config.MOVIE_ANNEX_OLD_ROOT, hdd_store(hdd)
    if new.exists():
        raise AlbumflowError(f"Both {old} and {new} exist; please check by hand")
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    log(f"Moved {old} -> {new}")


# --- moving ---

def link_to(release: str, movies: dict[str, Movie]) -> Path | None:
    movie = movies.get(release.lower())
    return view_dir() / movie.link if movie and movie.link else None


def to_hdd(release: str, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move a movie from the store on C: to the movie annex; its link in the view then points there."""
    hdd = require_hdd()
    folder, target = c_store() / release, hdd_store(hdd) / release
    part = target.with_name(target.name + PART)
    resuming = has_note(part) or has_note(target)
    if not resuming:
        if not folder.is_dir() or is_link(folder):
            raise AlbumflowError(f"{release} is not in the store on C: ({folder})")
        if inside := junctions_inside(folder):
            raise AlbumflowError(f"{folder} contains a junction ({inside[0]})")
        if target.exists() or part.exists():
            raise AlbumflowError(f"{target} already exists on the movie annex; please check by hand")
        annex.check_space(folder, Path(hdd.anchor))
    log(f"{'Continuing to move' if resuming else 'Moving'} {folder} -> {target}")
    if dry_run:
        return target
    if not target.exists():  # copy (again) into .part
        write_note(part, "movie-to-annex", folder)
        copy_tree(folder, part, progress)
        check_copy(folder, part)
        part.rename(target)
    if link := link_to(release, collect(hdd)[0]):
        retarget(link, link_target(release, hdd))  # before deleting, so the view never points to nothing
    if folder.is_dir():
        check_left(folder, target)
        delete_tree(folder, annex.home())
    (target / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {release} is on the movie annex" + (f"; {link.name} points there." if link else "."))
    return target


def from_hdd(release: str, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move a movie from the movie annex back to the store on C:; its link in the view then points there."""
    hdd = require_hdd()
    folder, source = c_store() / release, hdd_store(hdd) / release
    part = folder.with_name(folder.name + PART)
    resuming = has_note(part) or has_note(folder)
    if not resuming:
        if not source.is_dir():
            raise AlbumflowError(f"{release} is not on the movie annex ({source})")
        if folder.exists() or part.exists():
            raise AlbumflowError(f"{folder} already exists; please check by hand")
        annex.check_space(source, Path(folder.anchor))
    log(f"{'Continuing to move' if resuming else 'Moving'} {source} -> {folder}")
    if dry_run:
        return folder
    if not folder.exists():  # copy (again) into .part
        write_note(part, "movie-from-annex", source)
        copy_tree(source, part, progress)
        check_copy(source, part)
        part.rename(folder)
    if link := link_to(release, collect(hdd)[0]):
        retarget(link, link_target(release, None))
    if source.exists():
        check_left(source, folder)
        delete_tree(source, hdd)
    (folder / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {release} is back on C:" + (f"; {link.name} points there." if link else "."))
    return folder


def check_left(src: Path, copy: Path) -> None:
    """Before deleting src: each file in it is in the complete copy with the same size. An earlier delete may have
    stopped halfway (e.g. a file open in a player), leaving only some."""
    copied = files_of(copy)
    for rel, size in files_of(src).items():
        if copied.get(rel) != size:
            raise AlbumflowError(f"{src / rel} isn't in {copy} with the same size; nothing was deleted")


# --- EMDB and the names ---

@dataclass
class Sync:
    """What makes the view's names and EMDB match the movies, which are the master."""
    renames: dict[str, str] = field(default_factory=dict)  # link name in the view -> its canonical name
    changes: dict[int, str] = field(default_factory=dict)  # EMDB line of a movie -> its new Location ("" clears)
    entries: list[Entry] = field(default_factory=list)
    unmatched: list[tuple[str, list[emdb.Movie]]] = field(default_factory=list)  # release, entries to choose from


def location_parts(path: str) -> tuple[str, str, Path] | None:
    """Where a Location points: ("view", link name, path inside) or ("store", release name, path inside), also from
    the old places (config.EMDB_OLD_MOVIE_ROOTS); None if it's none of these."""
    def inside(root: str) -> Path | None:
        root = root.rstrip("\\") + "\\"
        return Path(path[len(root):]) if path.lower().startswith(root.lower()) and path[len(root):] else None

    if rel := inside(str(view_dir())):
        return "view", rel.parts[0], Path(*rel.parts[1:])
    if m := re.search(r"\\" + re.escape(config.MOVIE_STORE) + r"\\(.+)", path, re.IGNORECASE):  # C: or the HDD
        rel = Path(m.group(1))
        return "store", rel.parts[0], Path(*rel.parts[1:])
    for root in config.EMDB_OLD_MOVIE_ROOTS:
        if rel := inside(root):
            return "store", rel.parts[0], Path(*rel.parts[1:])
    return None


def emdb_sync(movies: dict[str, Movie], entries: list[emdb.Movie], hdd: Path | None, reader: mediainfo.Reader,
              chosen: dict[str, emdb.Movie] | None = None) -> Sync:
    """Each movie gets the EMDB entry whose Location points to it (through the view, into the store or an old
    place); a renamed one is found by its file's name; one without gets the entry with its title and year (from the
    release name) that has no Location, or the one chosen (by release, lower case); the Location of a movie that is
    gone is cleared (the entry stays, as for movies seen without a file). Then each movie's canonical name, and its
    Location through the view. Without the movie annex, the movies on it keep their names, unless MediaInfo's
    findings are kept from before, and Locations that may point there stay."""
    sync = Sync()
    by_link = {m.link.lower(): m for m in movies.values() if m.link}
    linked: dict[str, tuple[emdb.Movie, Path]] = {}  # release (lower case) -> its entry, the file in it
    gone: list[emdb.Movie] = []

    def link(movie: Movie, entry: emdb.Movie, rel: Path) -> None:
        if movie.release.lower() in linked:
            other = linked[movie.release.lower()][0]
            sync.entries.append(Entry("EMDB duplicate", entry.title, f"{movie.release}, like {other.title}"))
        else:
            linked[movie.release.lower()] = (entry, rel)

    for entry in entries:
        found = location_parts(emdb.without_label(entry.location)) if entry.location else None
        if not found:
            continue  # no Location, or one that isn't a movie's
        kind, name, rel = found
        movie = (by_link if kind == "view" else movies).get(name.lower())
        if movie is None and kind == "store" and not rel.parts \
                and Path(name).suffix.lower() in config.MOVIE_EXTENSIONS:
            movie, rel = movies.get(Path(name).stem.lower()), Path(name)  # a loose file, put into a folder since
        if movie is None or not movie.here:
            if hdd:
                gone.append(entry)
            elif movie:
                link(movie, entry, rel)  # on the movie annex, not connected: taken as it is
            continue  # else maybe on the movie annex, not connected: can't check
        if not (rel.parts and (movie.folder(hdd) / rel).is_file()):
            main = main_file(movie.folder(hdd))
            if main is None:
                sync.entries.append(Entry("no movie file", movie.release))
                continue
            rel = main.relative_to(movie.folder(hdd))
        link(movie, entry, rel)

    free = [m for m in movies.values() if m.here and m.release.lower() not in linked]
    by_file_name: dict[str, list[tuple[Movie, Path]]] = {}
    for movie in free:
        for file in movie_files(movie.folder(hdd)):
            by_file_name.setdefault(file.name.lower(), []).append((movie, file.relative_to(movie.folder(hdd))))
    for entry in list(gone):  # renamed: the file is in a movie without an EMDB entry
        hits = by_file_name.get(PureWindowsPath(emdb.without_label(entry.location)).name.lower(), [])
        if len(hits) == 1 and hits[0][0].release.lower() not in linked:
            link(hits[0][0], entry, hits[0][1])
            gone.remove(entry)

    taken = {e.line for e, _ in linked.values()}
    without = [e for e in entries if not e.location] + gone  # entries free to take a movie
    for movie in sorted(free, key=lambda m: m.release.lower()):
        if movie.release.lower() in linked:
            continue
        main = main_file(movie.folder(hdd))
        if main is None:
            sync.entries.append(Entry("no movie file", movie.release))
            continue
        free_entries = [e for e in without if e.line not in taken]
        match = (chosen or {}).get(movie.release.lower()) or match_title(movie.release, free_entries)
        if match:
            link(movie, match, main.relative_to(movie.folder(hdd)))
            taken.add(match.line)
            if match in gone:
                gone.remove(match)
        else:
            years = [y for _, y in title_years(movie.release)]
            choices = sorted((e for e in free_entries if e.year.isdigit()
                              and any(abs(int(e.year) - y) <= 1 for y in years)), key=lambda e: e.title.lower())
            sync.unmatched.append((movie.release, choices))
            sync.entries.append(Entry("not in EMDB", movie.release))
    for entry in gone:
        sync.changes[entry.line] = ""
        sync.entries.append(Entry("Location to clear", entry.title, entry.location))

    names = {m.link.lower() for m in movies.values() if m.link and m.release.lower() not in linked}
    for release, (entry, rel) in sorted(linked.items()):
        movie = movies[release]
        name = movie.link or movie.release
        tracks = reader.tracks(movie.release, rel, movie.folder(hdd) / rel) if movie.here \
            else reader.cached(movie.release, rel)
        if tracks is not None:
            tech = mediainfo.describe(tracks)
            if tech is None:
                sync.entries.append(Entry("unreadable", movie.release, str(rel)))
            else:
                name = movienames.canonical(entry.title, entry.original, entry.year, movie.release, tech)
                for doubt in mediainfo.doubtful_languages(tracks):
                    sync.entries.append(Entry("language tag doubtful", name, doubt))
        name = unique(name, names)
        names.add(name.lower())
        if movie.link and movie.link != name:
            sync.renames[movie.link] = name
            sync.entries.append(Entry("name to change", movie.link, f"-> {name}"))
        location = str(view_dir() / name / rel)
        if location != entry.location:
            sync.changes[entry.line] = location
            sync.entries.append(Entry("Location to set" if not entry.location else "Location outdated", entry.title,
                                      f"{entry.location or '(none)'} -> {location}"))
        if len(location) > config.EMDB_MAX_PATH:
            sync.entries.append(Entry("long path", name, f"{len(location)} characters"))
    return sync


def unique(name: str, taken: set[str]) -> str:
    """name, or with " (2)" etc. if another movie has it (variants the same in all the name says)."""
    result, n = name, 2
    while result.lower() in taken:
        result, n = f"{name} ({n})", n + 1
    return result


def rename_links(renames: dict[str, str], log=print) -> None:
    """Rename the view's links; via temporary names, so swapped names work too."""
    temporary = {}
    for i, old in enumerate(renames):
        temp = view_dir() / f".albumflow-rename-{i}"
        os.rename(view_dir() / old, temp)  # renames the junction itself, not what it points to
        temporary[old] = temp
    for old, new in renames.items():
        os.rename(temporary[old], view_dir() / new)
        log(f"  {old} -> {new}")


def movie_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in config.MOVIE_EXTENSIONS]


def main_file(folder: Path) -> Path | None:
    """The file EMDB's Location points to: the movie's largest movie file (e.g. of a Blu-ray's BDMV\\STREAM, which
    EMDB can read the codecs of)."""
    return max(movie_files(folder), key=lambda p: p.stat().st_size, default=None)


def normal_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def title_years(name: str) -> list[tuple[str, int]]:
    """The ways to read a release name as a title followed by its year, e.g. "1917.2019.BDREMUX" -> ("1917.", 2019)."""
    found = []
    for m in re.finditer(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", name):
        if normal_words(name[:m.start()]):
            found.append((name[:m.start()], int(m.group())))
    return found


def match_title(name: str, movies: list[emdb.Movie]) -> emdb.Movie | None:
    """The EMDB movie a release name stands for: its title, as whole words, before a year that is the movie's (±1,
    as release years differ); the longest such title, if only one is the longest."""
    hits = []
    for prefix, year in title_years(name):
        words = normal_words(prefix)
        for movie in movies:
            title = normal_words(movie.title)
            if title and movie.year.isdigit() and abs(int(movie.year) - year) <= 1 and any(
                    words[i:i + len(title)] == title for i in range(len(words) - len(title) + 1)):
                hits.append((len(title), movie))
    longest = [m for n, m in hits if n == max((n for n, _ in hits), default=0)]
    return longest[0] if len({m.line for m in longest}) == 1 else None

"""The annex: a drive holding moved-out folders of the home folder.

The annex mirrors the home folder: <annex>/<path> is what would be
<home>/<path>. A folder moved to the annex leaves a junction at its place in
the home folder, named "<name> (annex)", so everything still appears in one
tree, and the suffix makes clear that it isn't a normal folder.

Moving is never a direct move. The folder is copied to "<name>.part" at the
destination, together with a move note; the copy is checked (file list and
sizes), renamed to its final name, and only then is the source deleted. A
folder with a move note is an interrupted move, which running the same
command again continues. The only way a junction is ever removed is
os.rmdir, and recursive deletes refuse to go through junctions, so deleting a
link can never delete what it points to.

Only depends on generic helpers, so it could become its own tool later.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from albumflow import config
from albumflow.files import AlbumflowError, ByteCounter, Progress, copy_file, drives

MOVE_NOTE = ".albumflow-move.json"
PART = ".part"


# --- where things are ---

def find_annex() -> Path | None:
    """The annex drive's root, found by its marker file; None if not connected."""
    override = os.environ.get("ALBUMFLOW_ANNEX")
    if override:
        return Path(override) if Path(override).is_dir() else None
    found = [d for d in drives() if any((d / m).is_file() for m in config.ANNEX_MARKERS)]
    if len(found) > 1:
        raise AlbumflowError(f"Several annex drives found: {', '.join(map(str, found))}")
    return found[0] if found else None


def require_annex() -> Path:
    annex = find_annex()
    if annex is None:
        raise AlbumflowError(f"The annex drive is not connected (no drive with a {config.ANNEX_MARKERS[0]} file)")
    return annex


def home() -> Path:
    return Path(os.path.abspath(config.HOME_FOLDER))


def relative(path: Path) -> Path:
    """path relative to the home folder; error if it isn't inside."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(home())
    except ValueError:
        raise AlbumflowError(f"{path} is not inside the home folder {home()}") from None
    if not rel.parts:
        raise AlbumflowError("The home folder itself can't be moved to the annex")
    return rel


def link_of(folder: Path) -> Path:
    """The junction that stands for folder while it is in the annex."""
    return folder.with_name(folder.name + config.ANNEX_SUFFIX)


def folder_of(link: Path) -> Path:
    """The folder a '<name> (annex)' junction stands for."""
    if not link.name.endswith(config.ANNEX_SUFFIX):
        raise AlbumflowError(f"{link} is not an annex link (no '{config.ANNEX_SUFFIX}' at the end)")
    return link.with_name(link.name[: -len(config.ANNEX_SUFFIX)])


def is_link(path: Path) -> bool:
    return os.path.isjunction(path)


def is_annex_link(path: Path) -> bool:
    return path.name.endswith(config.ANNEX_SUFFIX) and is_link(path)


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def has_files(folder: Path) -> bool:
    return any(p.is_file() for p in folder.iterdir())


def link_target(link: Path) -> Path:
    target = os.readlink(link)
    return Path(target[4:] if target.startswith("\\\\?\\") else target)


# --- safety helpers ---

def running_editors() -> list[str]:
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    running = {line.split(",")[0].strip('"').lower() for line in out.splitlines() if line}
    return sorted(name for name in config.APPS_MODIFYING_FILES if name.lower() in running)


def check_no_editors() -> None:
    if apps := running_editors():
        raise AlbumflowError(f"Not moving folders while {', '.join(apps)} may be modifying files. Close them first.")


def junctions_inside(folder: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, _ in os.walk(folder):
        for name in list(dirnames):
            if is_link(Path(dirpath) / name):
                found.append(Path(dirpath) / name)
                dirnames.remove(name)  # never descend into a junction
    return found


def delete_tree(folder: Path, root: Path) -> None:
    """Recursively delete folder, which must be a real folder below root, with no junction on the way or inside."""
    folder, root = Path(os.path.abspath(folder)), Path(os.path.abspath(root))
    try:
        rel = folder.relative_to(root)
    except ValueError:
        raise AlbumflowError(f"Refusing to delete {folder}: not below {root}") from None
    if not rel.parts:
        raise AlbumflowError(f"Refusing to delete {folder}: it is {root} itself")
    step = root
    for part in rel.parts:
        step = step / part
        if is_link(step):
            raise AlbumflowError(f"Refusing to delete {folder}: {step} is a junction")
    if inside := junctions_inside(folder):
        raise AlbumflowError(f"Refusing to delete {folder}: it contains the junction {inside[0]}")
    shutil.rmtree(folder)


def create_link(link: Path, target: Path) -> None:
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError):  # CPython internal; fall back to mklink
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# --- copying ---

def files_of(folder: Path) -> dict[Path, int]:
    """Relative path -> size of every file below folder, without the move note."""
    found = {}
    for dirpath, _, filenames in os.walk(folder):
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(folder)
            if rel != Path(MOVE_NOTE):
                found[rel] = path.stat().st_size
    return found


def copy_tree(src: Path, dest: Path, progress: Progress | None = None) -> None:
    """Copy src into dest; files already there with the right size are kept (a resumed copy)."""
    todo = [(rel, size) for rel, size in files_of(src).items()
            if not ((dest / rel).is_file() and (dest / rel).stat().st_size == size)]
    counter = ByteCounter(sum(size for _, size in todo), progress)
    for dirpath, _, _ in os.walk(src):
        (dest / Path(dirpath).relative_to(src)).mkdir(parents=True, exist_ok=True)  # also empty folders
    for rel, _ in todo:
        counter.start(rel.name)
        copy_file(src / rel, dest / rel, counter.add)


def check_copy(src: Path, dest: Path) -> None:
    src_files, dest_files = files_of(src), files_of(dest)
    if src_files != dest_files:
        missing = sorted(set(src_files) - set(dest_files))
        different = sorted(r for r in set(src_files) & set(dest_files) if src_files[r] != dest_files[r])
        extra = sorted(set(dest_files) - set(src_files))
        detail = (missing and f"missing {missing[0]}") or (different and f"different size {different[0]}") \
            or f"unexpected {extra[0]}"
        raise AlbumflowError(f"The copy in {dest} doesn't match {src} ({detail}); nothing was deleted")


def write_note(folder: Path, direction: str, src: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    note = {"move": direction, "from": str(src), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    (folder / MOVE_NOTE).write_text(json.dumps(note, indent=1), encoding="utf-8")


def has_note(folder: Path) -> bool:
    return (folder / MOVE_NOTE).is_file()


def check_space(src: Path, dest_drive: Path) -> None:
    needed = sum(files_of(src).values())
    free = shutil.disk_usage(dest_drive).free
    if needed > free:
        raise AlbumflowError(f"Not enough space on {dest_drive.anchor}: need {needed / 1e9:.1f} GB, "
                             f"free {free / 1e9:.1f} GB")


# --- moving ---

LIGHTROOM_HINT = "If Lightroom knows this folder: Library > Folders > right-click it > Find Missing Folder."


def to_annex(folder: Path, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move folder (inside the home folder) to the annex; returns the annex copy."""
    folder = Path(os.path.abspath(folder))
    annex = require_annex()
    target = annex / relative(folder)
    part = target.with_name(target.name + PART)
    link = link_of(folder)

    if is_annex_link(link) and not folder.exists() and not has_note(target):
        raise AlbumflowError(f"{folder.name} is already in the annex ({link.name})")
    resuming = has_note(part) or has_note(target)
    if not resuming:
        if is_link(folder):
            raise AlbumflowError(f"{folder} is a junction, not a folder")
        if not folder.is_dir():
            raise AlbumflowError(f"{folder} is not a folder")
        if inside := junctions_inside(folder):
            raise AlbumflowError(f"{folder} contains a junction ({inside[0]}); move that one back first")
        if target.exists() or part.exists():
            raise AlbumflowError(f"{target} already exists in the annex; please check by hand")
        if link.exists():
            raise AlbumflowError(f"{link} already exists")
        check_space(folder, annex)
    check_no_editors()
    log(f"{'Continuing to move' if resuming else 'Moving'} {folder} -> {target}")
    if dry_run:
        return target

    if not target.exists():  # copy (again) into .part
        write_note(part, "to-annex", folder)
        copy_tree(folder, part, progress)
        check_copy(folder, part)
        part.rename(target)
    if folder.exists():
        check_copy(folder, target)
        delete_tree(folder, home())
    if not link.exists():
        create_link(link, target)
    (target / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {folder.name} is in the annex; {link.name} stands for it.")
    log(LIGHTROOM_HINT)
    return target


def from_annex(folder: Path, log=print, progress: Progress | None = None, dry_run: bool = False) -> Path:
    """Move folder (its home-folder path, without the suffix) back from the annex."""
    folder = Path(os.path.abspath(folder))
    annex = require_annex()
    source = annex / relative(folder)
    part = folder.with_name(folder.name + PART)
    link = link_of(folder)

    resuming = has_note(part) or has_note(folder)
    if not resuming:
        if not source.is_dir():
            raise AlbumflowError(f"{source} is not in the annex")
        if folder.exists():
            raise AlbumflowError(f"{folder} already exists; please check by hand")
        if part.exists():
            raise AlbumflowError(f"{part} already exists; please check by hand")
        check_space(source, folder.parent)
    check_no_editors()
    log(f"{'Continuing to move' if resuming else 'Moving'} {source} -> {folder}")
    if dry_run:
        return folder

    if not folder.exists():  # copy (again) into .part
        write_note(part, "from-annex", source)
        copy_tree(source, part, progress)
        check_copy(source, part)
        if is_link(link):
            os.rmdir(link)  # removes only the junction, never what it points to
        part.rename(folder)
    if source.exists():
        check_copy(source, folder)
        delete_tree(source, annex)
    (folder / MOVE_NOTE).unlink(missing_ok=True)
    log(f"Done: {folder.name} is back at {folder.parent}.")
    log(LIGHTROOM_HINT)
    return folder


# --- status ---

@dataclass
class Entry:
    kind: str  # "ok", "missing link", "broken link", "in both places", "interrupted", "no folder on C:"
    home_path: Path  # where it is (or would be) in the home folder
    annex_path: Path | None = None

    @property
    def problem(self) -> bool:
        return self.kind != "ok"


def status(annex: Path | None = None) -> list[Entry]:
    """Every folder in the annex and every annex link in the home folder, with problems."""
    annex = annex or require_annex()
    entries: list[Entry] = []
    seen_links: set[Path] = set()

    def walk(annex_dir: Path, home_dir: Path) -> None:
        for child in sorted(p for p in annex_dir.iterdir() if p.is_dir() and not is_link(p)):
            if child.name.lower() == "system volume information" or child.name.startswith("$"):
                continue
            name = child.name[: -len(PART)] if child.name.endswith(PART) else child.name
            home_child, link = home_dir / name, link_of(home_dir / name)
            if child.name.endswith(PART) or has_note(child):
                entries.append(Entry("interrupted", home_child, child))
            elif is_link(link):
                seen_links.add(link)
                ok = same_path(link_target(link), child)
                entries.append(Entry("ok" if ok else "broken link", home_child, child))
            elif home_child.is_dir() and not is_link(home_child):
                if has_files(child):
                    entries.append(Entry("in both places", home_child, child))
                else:
                    walk(child, home_child)  # a parent folder that exists in both places
            elif home_dir.is_dir() and (has_files(child) or not same_path(home_dir, home())):
                entries.append(Entry("missing link", home_child, child))
            else:
                # Only folders inside, directly in the home folder, and nothing like it
                # there (e.g. a layout that doesn't mirror the home folder yet).
                # Never linked automatically.
                entries.append(Entry("no folder on C:", home_child, child))

    walk(annex, home())
    # Links whose target is gone: look in the home-folder parents of what's in the annex.
    for top in sorted(p for p in annex.iterdir() if p.is_dir()):
        home_top = home() / top.name
        if home_top.is_dir() and not is_link(home_top):
            for dirpath, dirnames, _ in os.walk(home_top):
                for name in list(dirnames):
                    path = Path(dirpath) / name
                    if is_link(path):
                        dirnames.remove(name)
                        if name.endswith(config.ANNEX_SUFFIX) and path not in seen_links:
                            entries.append(Entry("broken link", folder_of(path), None))
    return sorted(entries, key=lambda e: str(e.home_path).lower())


def fix_link(entry: Entry) -> None:
    """Create the missing link for a folder that is in the annex."""
    if entry.kind != "missing link":
        raise AlbumflowError(f"Can't fix '{entry.kind}' automatically: {entry.home_path}")
    create_link(link_of(entry.home_path), entry.annex_path)


def annexed_folders(below: Path) -> list[Path]:
    """The home-folder paths (without suffix) of the annex links below a folder, e.g. the albums."""
    found = []
    for dirpath, dirnames, _ in os.walk(below):
        for name in list(dirnames):
            path = Path(dirpath) / name
            if is_link(path):
                dirnames.remove(name)
                if name.endswith(config.ANNEX_SUFFIX):
                    found.append(folder_of(path))
    return sorted(found)

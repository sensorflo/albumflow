"""A small index on the primary drive (config.INDEX_FILE), for what albumflow can't see when it isn't connected.

Today one part: the photos on the secondary drive (its pictures-new), per album name -> size, as of the last
scan. update-secondary and purge scan the secondary drive whenever it is connected, so changes made there
outside albumflow are corrected at the next scan. Without the secondary drive connected, albumflow asks
the index whether a photo is on the secondary drive: e.g. after its album was purged from the primary
drive's copied-to-secondary, backup-new doesn't copy it to the primary drive again.

JSON: {"secondary": {"scanned": <time>, "albums": {"<topic>\\\\<album>": {"<name>": <size>}}}}
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from albumflow import config


def path(drive: Path) -> Path:
    return drive / config.INDEX_FILE


_cache: dict[Path, tuple[float, dict]] = {}  # file -> (modification time, data): asked once per album


def load(drive: Path) -> dict:
    file = path(drive)
    if not file.is_file():
        return {}
    mtime = file.stat().st_mtime
    if file not in _cache or _cache[file][0] != mtime:
        _cache[file] = (mtime, json.loads(file.read_text(encoding="utf-8")))
    return _cache[file][1]


def save(drive: Path, data: dict) -> None:
    file = path(drive)
    file.parent.mkdir(parents=True, exist_ok=True)
    part = file.with_name(file.name + ".part")
    part.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    part.replace(file)


def scan(root: Path) -> dict[str, dict[str, int]]:
    """<topic>\\<album> -> {name: size} of the files of the albums below root."""
    albums = {}
    for folder in sorted(root.glob("*/*")) if root.is_dir() else []:
        if folder.is_dir():
            albums[str(folder.relative_to(root))] = {p.name: p.stat().st_size for p in folder.rglob("*")
                                                     if p.is_file() and p.suffix != ".part"}
    return albums


def record_secondary(drive: Path, secondary: Path) -> None:
    """Scan the secondary drive's pictures-new into the primary drive's index."""
    data = dict(load(drive))
    data["secondary"] = {"scanned": datetime.datetime.now().isoformat(timespec="seconds"),
                         "albums": scan(secondary / config.SECONDARY_DIR)}
    save(drive, data)


def on_secondary(drive: Path, topic: str | None, album: str) -> dict[str, int]:
    """name -> size of the album's photos on the secondary drive as of the last scan (topic None: any)."""
    albums = load(drive).get("secondary", {}).get("albums", {})
    found = {}
    for name, files in albums.items():
        t, _, a = name.partition("\\")
        if a == album and (topic is None or t == topic):
            found.update(files)
    return found


def record_laptop(drive: Path) -> None:
    """Record what only the laptop knows, for purging on another computer: the albums in ORIGINALS (finished),
    and those in the new tree with clock corrections pending (which need their originals on the primary drive)."""
    from albumflow import annex, clock, purge  # not at the top: they import this module's users
    now = datetime.datetime.now().isoformat(timespec="seconds")
    finished = sorted({f"{p.parent.name}\\{p.name.removesuffix(config.ANNEX_SUFFIX)}"
                       for p in config.ORIGINALS.glob("*/*") if p.is_dir() or annex.is_link(p)})
    pending = sorted(f"{p.parent.name}\\{p.name}" for p in config.PICTURES_NEW.glob("*/*")
                     if p.is_dir() and not annex.is_link(p) and purge.corrections_pending(p.parent.name, p.name))
    data = dict(load(drive))
    data["originals"] = {"scanned": now, "albums": finished}
    data["corrections_pending"] = {"scanned": now, "albums": pending}
    save(drive, data)


def laptop_part(drive: Path | None, part: str) -> tuple[set[str], str] | None:
    """(albums, when scanned) of the "originals" or "corrections_pending" part; None if never recorded."""
    entry = load(drive).get(part) if drive else None
    return (set(entry["albums"]), entry["scanned"]) if entry else None

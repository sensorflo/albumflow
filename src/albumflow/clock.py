"""Camera clock correction: put the photos of all cameras in the order they were taken.

Each camera photographs a clock at the start of a festival. That gives each
camera's offset from the clock, and so its offset from the master camera
(config.MASTER_CAMERA). A photo's "master time" is the time the master
camera's clock would have shown when it was taken.

Only photos that would otherwise be out of order are corrected: their capture
time (in the file) becomes their master time, and they are renamed to
<master time>-<import name>. The master's photos are never changed. A photo is
only corrected once its original is on the backup drive, which keeps the
originals (import name and content).
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from albumflow import config
from albumflow.exiftool import ExifTool
from albumflow.files import AlbumflowError

STATE_FILE = ".albumflow.json"
TIME_FORMAT = "%Y%m%dT%H%M%S"
_NAME = re.compile(r"^(\d{8}T\d{6})-(.+)$")


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, TIME_FORMAT)


def format_time(t: datetime.datetime) -> str:
    return t.strftime(TIME_FORMAT)


def split_name(name: str) -> tuple[str | None, str]:
    """'<corrected>-<import name>' -> (corrected time, import name); an import name -> (None, name)."""
    m = _NAME.match(name)
    if m and _NAME.match(m.group(2)):
        return m.group(1), m.group(2)
    return None, name


def import_time(import_name: str) -> datetime.datetime:
    m = _NAME.match(import_name)
    if not m:
        raise AlbumflowError(f"Not an imported photo name (no capture time in front): {import_name}")
    return parse_time(m.group(1))


def model(camera: str) -> str:
    return camera.split(" ")[0]


# --- the album's state file: clock measurements and a cache of each photo's camera ---

class AlbumState:
    def __init__(self, album_dir: Path):
        self.path = album_dir / STATE_FILE
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        # camera -> {"offset": camera clock minus real clock, in s; "photo": ...; "clock": ...}
        self.clock: dict[str, dict] = data.get("clock", {})
        self.cameras: dict[str, str] = data.get("cameras", {})  # import stem -> camera
        # cameras without a clock reading, treated as in sync with the master (or, for
        # the master, as showing the right time)
        self.assumed: list[str] = data.get("assume_in_sync", [])

    def save(self) -> None:
        data = {"clock": self.clock, "assume_in_sync": self.assumed, "cameras": self.cameras}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def master_offset(self) -> int | None:
        """The master's offset from the clock; None while it has no reading (and isn't assumed in sync)."""
        for camera, m in self.clock.items():
            if model(camera) == config.MASTER_CAMERA:
                return m["offset"]
        return 0 if any(model(c) == config.MASTER_CAMERA for c in self.assumed) else None

    def offsets(self) -> dict[str, int]:
        """camera -> seconds its clock is ahead of the master's; empty while the master's is unknown."""
        master = self.master_offset()
        if master is None:
            return {}
        offsets = {camera: m["offset"] - master for camera, m in self.clock.items()}
        for camera in self.assumed:
            offsets.setdefault(camera, 0)
        return offsets

    def unknown_cameras(self, cameras: set[str]) -> list[str]:
        """Those of cameras with neither a clock reading nor assumed in sync."""
        return sorted(c for c in cameras if c not in self.clock and c not in self.assumed)


# --- the photos of an album ---

@dataclass(eq=False)  # compared by identity
class Shot:
    """One photo: its files (e.g. ARW + JPG) share the same import name stem."""
    stem: str  # import name without extension, e.g. 20260828T120736-_DSC8045
    files: list[Path] = field(default_factory=list)
    camera: str = ""
    corrected: str | None = None  # current correction prefix, if any
    offset: int = 0  # camera ahead of master, in s
    plan: bool = False  # to be corrected

    @property
    def import_time(self) -> datetime.datetime:
        return import_time(self.stem)

    @property
    def master_time(self) -> datetime.datetime:
        return self.import_time - datetime.timedelta(seconds=self.offset)

    def key(self, planned: bool = False) -> str:
        """The name it sorts by, now or after the planned correction."""
        if planned and self.plan:
            return f"{format_time(self.master_time)}-{self.stem}"
        return f"{self.corrected}-{self.stem}" if self.corrected else self.stem


def photo_files(album_dir: Path) -> list[Path]:
    return sorted(p for p in album_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in config.CAMERA_SUFFIXES)


def load_shots(album_dir: Path, state: AlbumState, exif: ExifTool | None = None, log=print,
               progress=None) -> list[Shot]:
    """All photos of the album, with their cameras.

    The import records each photo's camera in the state file; for photos
    imported before that (or otherwise), it is read here, saved as it goes.
    progress(done, total, name) reports that reading.
    """
    shots: dict[str, Shot] = {}
    for path in photo_files(album_dir):
        corrected, import_name = split_name(path.name)
        stem = Path(import_name).stem
        shot = shots.setdefault(stem, Shot(stem))
        shot.files.append(path)
        shot.corrected = corrected
    unknown = [s for s in shots.values() if s.stem not in state.cameras]
    if unknown:
        log(f"Reading the camera of {len(unknown)} photos (only needed once)...")
        own = exif is None
        exif = exif or ExifTool()
        try:
            for i, shot in enumerate(unknown, 1):
                state.cameras[shot.stem] = exif.camera(shot.files[0])
                if progress:
                    progress(i, len(unknown), shot.stem)
                if i % 100 == 0:
                    state.save()
        finally:
            state.save()
            if own:
                exif.close()
    offsets = state.offsets()
    for shot in shots.values():
        shot.camera = state.cameras[shot.stem]
        shot.offset = offsets.get(shot.camera, 0)
    return sorted(shots.values(), key=lambda s: s.key())


# --- deciding what to correct ---

def plan(shots: list[Shot]) -> list[Shot]:
    """Mark the shots to correct so that sorting by name gives master-time order. Returns them.

    Repeatedly sorts by (planned) name and corrects the non-master photos of
    every neighbouring pair whose master times are the other way round, until
    no such pair is left. Already corrected photos stay corrected (their
    prefix follows the current measurement). Photos of the master, and of
    cameras in sync with it, can't be corrected and never need to be.
    """
    for shot in shots:
        shot.plan = shot.corrected is not None and shot.offset != 0
    while True:
        ordered = sorted(shots, key=lambda s: s.key(planned=True))
        changed = False
        for a, b in zip(ordered, ordered[1:]):
            if a.master_time > b.master_time:
                for shot in (a, b):
                    if shot.offset != 0 and not shot.plan:
                        shot.plan = changed = True
        if not changed:
            break
    return [s for s in shots if s.plan and s.key(planned=True) != s.key()]


# --- measuring and applying ---

def measure(album_dir: Path, photo: Path, clock_time: datetime.time, exif: ExifTool) -> tuple[str, int]:
    """Record that photo shows a clock reading clock_time. Returns (camera, offset in s)."""
    state = AlbumState(album_dir)
    _, import_name = split_name(photo.name)
    taken = import_time(import_name)  # the camera's own clock, also if the photo was corrected since
    # The date: the one that puts the clock reading closest to the camera's time.
    candidates = [datetime.datetime.combine(taken.date() + datetime.timedelta(days=d), clock_time) for d in (-1, 0, 1)]
    real = min(candidates, key=lambda t: abs((t - taken).total_seconds()))
    stem = Path(import_name).stem
    camera = state.cameras.get(stem) or exif.camera(photo)
    state.cameras[stem] = camera
    offset = int((taken - real).total_seconds())
    state.clock[camera] = {"offset": offset, "photo": import_name, "clock": clock_time.strftime("%H:%M:%S")}
    state.save()
    return camera, offset


def has_measurements(album_dir: Path) -> bool:
    state = AlbumState(album_dir)
    return bool(state.clock or state.assumed)


def is_master(camera: str) -> bool:
    return model(camera) == config.MASTER_CAMERA


def camera_label(camera: str) -> str:
    return f"{model(camera)} (master)" if is_master(camera) else model(camera)


def offset_text(state: AlbumState, camera: str) -> str:
    """E.g. '+1:17 relative to the master, +0:29 relative to true time' (+: camera ahead)."""
    true_time = f"{_seconds(state.clock[camera]['offset'])} relative to true time"
    if is_master(camera):
        return true_time
    rel = state.offsets().get(camera)
    master = f"{_seconds(rel)} relative to the master" if rel is not None else "master not measured yet"
    return f"{master}, {true_time}"


def copy_readings(source_dir: Path, target_dir: Path, source_name: str) -> tuple[list[str], list[str]]:
    """Take over another album's clock readings (and assumptions), e.g. of the same festival.

    The target's own readings are kept. Returns (cameras taken over, cameras kept).
    """
    source, target = AlbumState(source_dir), AlbumState(target_dir)
    if not (source.clock or source.assumed):
        raise AlbumflowError(f"{source_name} has no clock readings")
    taken, kept = [], []
    for camera, reading in source.clock.items():
        if camera in target.clock:
            kept.append(camera)
        else:
            target.clock[camera] = {**reading, "from": reading.get("from", source_name)}
            target.assumed = [c for c in target.assumed if c != camera]
            taken.append(camera)
    for camera in source.assumed:
        if camera not in target.clock and camera not in target.assumed:
            target.assumed.append(camera)
            taken.append(camera)
    target.save()
    return taken, kept


def describe(state: AlbumState) -> list[str]:
    """Human-readable lines about the measurements, the master first."""
    lines = []
    for camera in sorted(state.clock, key=lambda c: (not is_master(c), model(c))):
        m = state.clock[camera]
        source = f" (from {m['from']})" if m.get("from") else ""
        lines.append(f"  {camera_label(camera)}: clock photo {m['photo']}{source} showed {m['clock']}; "
                     f"{offset_text(state, camera)}")
    for camera in sorted(state.assumed, key=lambda c: (not is_master(c), model(c))):
        lines.append(f"  {camera_label(camera)}: no clock reading, assumed in sync with the master")
    return lines


def _seconds(s: int) -> str:
    sign = "+" if s >= 0 else "-"
    m, sec = divmod(abs(s), 60)
    h, m = divmod(m, 60)
    return f"{sign}{h}:{m:02d}:{sec:02d}" if h else f"{sign}{m}:{sec:02d}"


def cameras_without_reading(album_dir: Path, shots: list[Shot], state: AlbumState) -> list[str]:
    """Lines naming the album's cameras that have no clock reading (and aren't assumed in sync)."""
    unknown = state.unknown_cameras({s.camera for s in shots})
    if state.master_offset() is None and not any(model(c) == config.MASTER_CAMERA for c in unknown):
        unknown.append(f"{config.MASTER_CAMERA} (master)")
    counts = {c: sum(1 for s in shots if s.camera == c) for c in unknown}
    return [f"{model(c)} ({counts[c]} photos)" if counts.get(c) else c for c in unknown]


def apply(album_dir: Path, backup_dir: Path | None, log=print, confirm=None, dry_run: bool = False,
          progress=None) -> int:
    """Correct the photos that need it and whose originals are on the backup. Returns how many.

    Only when every camera of the album has a clock reading (or is assumed in
    sync); until then the corrections are paused. backup_dir is the album's
    folder on the backup drive (None: not connected, so nothing can be
    corrected yet). confirm(text) -> bool may veto.
    """
    state = AlbumState(album_dir)
    if not (state.clock or state.assumed):
        return 0
    with ExifTool() as exif:
        shots = load_shots(album_dir, state, exif, log, progress)
        missing = cameras_without_reading(album_dir, shots, state)
        if missing:
            log(f"Clock correction paused: no clock reading yet for {', '.join(missing)}.")
            log(f'  Add one with: albumflow sync-clocks -r <clock photo>=<time on the clock> '
                f'"{album_dir.parent.name}/{album_dir.name}"')
            return 0
        todo = plan(shots)
        if not todo:
            log("Clock correction: all photos are in order.")
            return 0
        ready = [s for s in todo if backup_dir is not None
                 and all((backup_dir / split_name(f.name)[1]).is_file() for f in s.files)]
        waiting = len(todo) - len(ready)
        by_camera: dict[str, int] = {}
        for shot in ready:
            by_camera[model(shot.camera)] = by_camera.get(model(shot.camera), 0) + 1
        summary = ", ".join(f"{n} of {cam}" for cam, n in sorted(by_camera.items()))
        if waiting:
            log(f"Clock correction: {waiting} photos wait until their originals are on the backup drive.")
        if not ready:
            return 0
        if dry_run:
            log(f"Clock correction: would correct {len(ready)} photos ({summary}).")
            for shot in sorted(ready, key=lambda s: s.key()):
                log(f"  {shot.key()} -> {shot.key(planned=True)}")
            return 0
        if confirm and not confirm(f"Correct the clock of {len(ready)} photos ({summary}) so they are in order?"):
            return 0
        for shot in ready:
            correct(shot, exif)
    log(f"Clock correction: corrected {len(ready)} photos ({summary}).")
    return len(ready)


def correct(shot: Shot, exif: ExifTool) -> None:
    """Set the shot's capture time to its master time, in its files and sidecar, and rename them."""
    target = format_time(shot.master_time)
    new_paths = [f.with_name(f"{target}-{split_name(f.name)[1]}") for f in shot.files]
    for path in new_paths:
        if path.exists():
            raise AlbumflowError(f"Can't correct {shot.stem}: {path} already exists")
    for old, new in zip(shot.files, new_paths):
        current = exif.capture_time(old)
        if not current:
            raise AlbumflowError(f"No capture time in {old}")
        # Shift by what is still missing, so an interrupted run can simply be repeated.
        exif.shift_times(old, int((shot.master_time - parse_time(current)).total_seconds()))
        if exif.capture_time(old) != target:
            raise AlbumflowError(f"Correcting the capture time of {old} failed")
        old.rename(new)
    # Sidecars (e.g. FastRawViewer's ratings) follow; found by the import name, whatever their prefix.
    album_dir = shot.files[0].parent
    for sidecar in album_dir.glob(f"*{shot.stem}.xmp"):
        corrected, import_name = split_name(sidecar.name)
        if import_name != f"{shot.stem}.xmp" or corrected == target:
            continue
        sidecar_time = parse_time(corrected) if corrected else shot.import_time
        exif.shift_times(sidecar, int((shot.master_time - sidecar_time).total_seconds()))
        sidecar.rename(sidecar.with_name(f"{target}-{import_name}"))
    shot.files, shot.corrected = new_paths, target

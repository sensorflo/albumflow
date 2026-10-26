"""albumflow.pyz on the primary and secondary drive: albumflow for another computer, e.g. the office laptop,
where only the drives, Python and the backup tool's backup.pyz (which runs it) are available.

Written, read-only, whenever albumflow runs on the laptop with a drive connected; only rewritten when it
changed. The Google libraries aren't in it: what needs them (Google Photos, deleting from the cloud) runs on
the laptop only.
"""

from __future__ import annotations

import importlib.resources
import io
import stat
import zipfile
from pathlib import Path

from albumflow import config

MIN_PYTHON = (3, 12)  # e.g. os.path.isjunction
MAIN = f'''import sys

if sys.version_info < {MIN_PYTHON!r}:
    sys.exit(f"albumflow needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, this is {{sys.version.split()[0]}}")
from albumflow.cli import main

raise SystemExit(main())
'''


def build() -> bytes:
    """The zipapp: albumflow's modules, with fixed timestamps, so an unchanged albumflow gives the same bytes."""
    modules = {m.name: m.read_bytes() for m in importlib.resources.files("albumflow").iterdir()
               if m.name.endswith(".py")}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in [("__main__.py", MAIN.encode()), *((f"albumflow/{n}", modules[n]) for n in sorted(modules))]:
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def write(drive: Path) -> bool:
    """Write albumflow.pyz to the drive, unless it is the same already or this is running from it. Returns
    whether it was written."""
    target = drive / config.SELF_COPY
    if target in Path(__file__).parents:
        return False  # running from that very copy
    data = build()
    if target.is_file() and target.read_bytes() == data:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    if target.exists():
        target.chmod(stat.S_IWRITE)
    part.replace(target)
    target.chmod(stat.S_IREAD)
    return True


def write_to_connected() -> None:
    """albumflow.pyz to the connected primary and secondary drive."""
    from albumflow import backup
    for drive in (backup.find_backup_drive(), backup.find_secondary_drive()):
        if drive:
            write(drive)

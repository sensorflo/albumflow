"""Machine-specific settings. Hard-coded for now; may move to a config file later."""

import os
import shutil
from pathlib import Path

# Albums being worked on: <PICTURES_NEW>\<topic>\<album>
PICTURES_NEW = Path(os.environ.get("ALBUMFLOW_PICTURES_NEW", r"C:\Users\senso\Large\Pictures\Pictures\new"))
# Finished albums: <ORIGINALS>\<topic>\<album>. restic backs them up, so the backup drives may drop them.
ORIGINALS = Path(os.environ.get("ALBUMFLOW_ORIGINALS", r"C:\Users\senso\Large\Pictures\Originals"))

# Album folders matching any of these (re.search on the folder name, ignoring case) are never
# offered as import / backup targets.
EXCLUDED_ALBUM_PATTERNS = [
    r"\s*\(lightroom\)$",
    r"\s*\(rejected\)$",
]

# Files imported from the camera card
CAMERA_SUFFIXES = {".arw", ".jpg", ".jpeg"}

# Marker files in the root of the (primary) backup drive and of the secondary one. The backup tool
# (~/src/backup) uses the same markers; it owns the restic repos on these drives, albumflow the album raws.
BACKUP_MARKER = ".backup-destination"
SECONDARY_BACKUP_MARKER = ".secondary-backup-destination"
# On the backup drive, an album's raw originals go to <BACKUP_DIR>\<topic>\<album>, mirroring PICTURES_NEW.
# update-secondary copies them to <SECONDARY_DIR>\<topic>\<album> on the secondary backup drive, then moves
# them on the backup drive to COPIED_DIR.
BACKUP_DIR = r"Backup\pictures-new\to-secondary"
COPIED_DIR = r"Backup\pictures-new\copied-to-secondary"
SECONDARY_DIR = r"Backup\pictures-new"
# While the secondary backup drive is out of reach (e.g. on holiday), update-cloud copies the same albums to
# <CLOUD_PICTURES>\<topic>\<album> instead; the backup tool runs it then and names the folder in its README.
CLOUD_PICTURES = Path(os.environ.get("ALBUMFLOW_CLOUD_PICTURES", r"G:\My Drive\Backup\pictures-new"))

# Folder on the camera card that receives the imported (renamed) originals
CARD_BACKUP_DIR = "Backup"

# After a card's import, if less than this fraction of the card is free,
# albumflow offers to delete old folders in <card>\Backup.
CARD_MIN_FREE = 0.5

# The annex: a drive holding moved-out folders of the home folder, mirroring it:
# <annex>\<path> is what would be <HOME_FOLDER>\<path>. At the folder's place on
# C: there is a junction named "<name><ANNEX_SUFFIX>" pointing to the annex.
HOME_FOLDER = Path(os.environ.get("ALBUMFLOW_HOME_FOLDER", r"C:\Users\senso"))
# Found by a marker file in its root; backup.py uses the same marker (keep both in sync).
# ".backup-source-sd" is the old name, accepted until the card has been switched.
ANNEX_MARKERS = [".annex", ".backup-source-sd"]
ANNEX_SUFFIX = " (annex)"
# Where `to-annex` without a folder looks for candidates (relative to HOME_FOLDER). A
# candidate is a folder that directly contains files, with everything below it.
ANNEX_SUGGEST_ROOTS = [
    R"Large\Pictures\Pictures\new",
    R"Large\Pictures\Pictures\new-old",
    R"Large\Pictures\Rejected",
    R"Large\Pictures\Originals",
]
ANNEX_SUGGESTIONS = 20
# Suggestion score = size in GB * (1 - e^(-age / ANNEX_AGE_DAYS)), age = days since the
# newest file in the folder was modified: recently worked on scores near 0.
ANNEX_AGE_DAYS = 180
# Folders are not moved while one of these may be modifying files (like backup.py's list, but without
# digikam.exe, which may run during moves).
APPS_MODIFYING_FILES = {"darktable.exe", "ansel.exe", "Lightroom.exe", "emdb.exe"}

# Lightroom catalogs, read (never written) before folders are moved to or from the annex.
LIGHTROOM_CATALOGS = [Path(r"C:\Users\senso\Large\Pictures\Lightroom\Lightroom Catalog.lrcat")]

# Camera clock correction: the other cameras' photos are put in order relative
# to this camera (model, as exiftool reports it). Its own photos are never changed.
MASTER_CAMERA = "ILCE-9M2"

_EXIFTOOL_CANDIDATES = [r"C:\Program Files\digiKam\exiftool\exiftool.exe"]


def exiftool() -> str:
    found = os.environ.get("ALBUMFLOW_EXIFTOOL") or shutil.which("exiftool")
    if found:
        return found
    for candidate in _EXIFTOOL_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("exiftool not found; install it or set ALBUMFLOW_EXIFTOOL")


def album_dir(topic: str, album: str) -> Path:
    return PICTURES_NEW / topic / album

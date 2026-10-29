"""Machine-specific settings. Hard-coded for now; may move to a config file later."""

import os
import shutil
from pathlib import Path

# Albums being worked on: <PICTURES_NEW>\<topic>\<album>
PICTURES_NEW = Path(os.environ.get("ALBUMFLOW_PICTURES_NEW", r"C:\Users\senso\Large\Pictures\Pictures\new"))
# Albums just as unfinished, which I may never find time for (the split is only psychological)
PICTURES_NEW_OLD = PICTURES_NEW.parent / "new-old"
# A sub-topic, not a topic: <PICTURES_NEW>\<ONLY_FOR_OTHERS>\<album> holds the photos of a finished album
# kept only for others (tagged NotForPublic/OnlyForOthers), as long as I want to spend the space.
ONLY_FOR_OTHERS = "OnlyForOthers"
# Finished albums: <ORIGINALS>\<topic>\<album>. restic backs them up, so the primary and secondary drives may drop them.
ORIGINALS = Path(os.environ.get("ALBUMFLOW_ORIGINALS", r"C:\Users\senso\Large\Pictures\Originals"))

# Album folders matching any of these (re.search on the folder name, ignoring case) are never
# offered as import / backup targets.
EXCLUDED_ALBUM_PATTERNS = [
    r"\s*\(lightroom\)$",
    r"\s*\(rejected\)$",
]

# Files imported from the camera card
CAMERA_SUFFIXES = {".arw", ".jpg", ".jpeg"}

# Marker files in the root of the primary drive and of the secondary one. The backup tool
# (~/src/backup) uses the same markers; it owns the restic repos on these drives, albumflow the album raws.
BACKUP_MARKER = ".backup-destination"
SECONDARY_BACKUP_MARKER = ".secondary-backup-destination"
# On the primary drive, an album's raw originals go to <BACKUP_DIR>\<topic>\<album>, mirroring PICTURES_NEW.
# update-secondary copies them to <SECONDARY_DIR>\<topic>\<album> on the secondary drive, then moves
# them on the primary drive to COPIED_DIR.
BACKUP_DIR = r"Backup\pictures-new\to-secondary"
COPIED_DIR = r"Backup\pictures-new\copied-to-secondary"
SECONDARY_DIR = r"Backup\pictures-new"
# On the primary drive: albumflow's index of what it can't see while the secondary drive isn't connected
# (see index.py)
INDEX_FILE = r"Backup\pictures-new\index.json"
# On the primary and secondary drive: a copy of albumflow for another computer (see selfcopy.py)
SELF_COPY = r"Backup\albumflow.pyz"
# While the secondary drive is out of reach (e.g. on holiday), update-cloud copies the same albums to
# <CLOUD_PICTURES>\<topic>\<album> instead; the backup tool runs it then and names the folder in its README.
CLOUD_PICTURES = Path(os.environ.get("ALBUMFLOW_CLOUD_PICTURES", r"G:\My Drive\Backup\pictures-new"))
# Next to CLOUD_PICTURES, the same for the rejected photos (in REJECTED_TREE): <it>\<topic>\<album>. When space is
# short, update-cloud deletes from here.
CLOUD_REJECTED = "pictures-rejected"
# The local folder of Google Drive's "My Drive" (Google Drive for desktop), containing CLOUD_PICTURES: deleting
# there goes through the Drive API, permanently, instead of to the trash
CLOUD_MY_DRIVE = Path(r"G:\My Drive")
# update-cloud always leaves this much free in the cloud, for its other uses
CLOUD_RESERVE = 2 * 10**9
# Albums not on the secondary drive yet, but safe elsewhere: update-cloud doesn't copy them to the
# cloud, backup-status names where they are. <topic>\<album> -> where. Remove an entry once the album is
# on the secondary (from then on it doesn't matter anyway). Card pruning ignores this: it may be the very
# card named here.
SAFE_ELSEWHERE = {
    R"party\Pan Tau Okt 2026": "SD card at home",
    # Albums from before albumflow, backed up to the primary on 2026-10-23
    R"hollidays\Borm TU Event 21": "SD card A1 I",
    R"hollidays\Borm TU Event 22": "SD card A1 I",
    R"hollidays\Bormday Sommer 21": "SD card A1 I",
    R"hollidays\Comersee Aug 2021 Handyphotos": "SD card A1 I",
    R"hollidays\Italien Frühling 2025": "SD card A1 I",
    R"hollidays\Maennerreisli Wallis 25": "SD card A1 I",
    R"hollidays\Mannereisli Domi 50 Gitschital 2026": "SD card A1 I",
    R"hollidays\Misox Shankra 2023": "SD card A1 I",
    R"hollidays\Ozora rundherum 2024": "SD card A1 I",
    R"hollidays\Rhäzüns Aug 2020": "SD card A1 I",
    R"hollidays\Savoie Sept 2020 Sonjas Cam": "SD card A1 I",
    R"hollidays\Suedfrankreich Nov 2025": "SD card A1 I",
    R"hollidays\Suedfrankreich Sommer 2021": "SD card A1 I",
    R"hollidays\Sueditalien Juni 2021 Handyphotos": "SD card A1 I",
    R"hollidays\Suedostasien 2022 Handyphotos": "SD card A1 I",
    R"hollidays\Ägypten Sommer 2021": "SD card A1 I",
    R"misc my life\Maert Uerzlikon Ok 22": "SD card A1 I",
    R"misc my life\Unsorted": "SD card A1 I",
    R"outdoor\Ausflug Brunnen Gersau Sep 2023": "SD card A1 I",
    R"outdoor\Baldeggersee March 2024": "SD card A1 I",
    R"outdoor\Chiemen Okt 23": "SD card A1 I",
    R"outdoor\Hedinger Weiher Eisbaden Feb 2026": "SD card A1 I",
    R"outdoor\Ibergeregg Maerz 2026": "SD card A1 I",
    R"outdoor\Reha Wald April 2023": "SD card A1 I",
    R"outdoor\Seleger Moor March 2024": "SD card A1 I",
    R"outdoor\Wäggital April 2025": "SD card A1 I",
    R"party\Alpine Gathering 23 Handyphotos": "SD card A1 I",
    R"party\Alpine Gathering 24 Handyphotos": "SD card A1 I",
    R"party\Back2Life Aug 21": "SD card A1 I",
    R"party\Baile Leon II 2026": "SD card A1 I",
    R"party\Burgklang 2025": "SD card A1 I",
    R"party\Burning Mountain 2023 OnlyForMe": "SD card A1 I",
    R"party\Circus of love 2025": "SD card A1 I",
    R"party\Evolution 2022 Handyphotos": "SD card A1 I",
    R"party\Freunde am Tanzen Sep 23 (Baden)": "SD card A1 I",
    R"party\Göle Traufer Letzigrund 22": "SD card A1 I",
    R"party\Hai in den Mai 24 Handyphotos": "SD card A1 I",
    R"party\Hochzeit": "SD card A1 I",
    R"party\Indian Spirit 2022 Handyphotos": "SD card A1 I",
    R"party\Mountain Calling 21 2023 rundherum": "SD card A1 I",
    R"party\Neujahr 2023-2024": "SD card A1 I",
    R"party\Outdoor Selection 2025": "SD card A1 I",
    R"party\Secret Castle 2026": "SD card A1 I",
    R"party\Shankra 2022 Handyphotos": "SD card A1 I",
    R"party\Streetparade 2026": "SD card A1 I",
    R"party\Tribe of us Sep 22": "SD card A1 I",
    R"party\USA Erinnerung 2023": "SD card A1 I",
    R"party\Zugermesse 2024": "SD card A1 I",
    R"places\Derendingen": "SD card A1 I",
    R"places\Mettmenstetten": "SD card A1 I",
    R"places\Mikas": "SD card A1 I",
    R"places\Solothurn": "SD card A1 I",
    R"places\Wilen": "SD card A1 I",
    R"places\Zug": "SD card A1 I",
}

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
# Rejected photos, <REJECTED_TREE>\<topic>\<album> (relative to HOME_FOLDER). While culling they are moved
# (e.g. by FastRawViewer) to the album's folder on C:, for speed; to-annex moves its files to the annex but
# keeps the folder, as the drop target, until the album is finished.
REJECTED_TREE = R"Large\Pictures\Rejected"
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

# Movies (relative to HOME_FOLDER). The store holds them as downloaded, <MOVIE_STORE>\<release name>, never renamed
# (qBittorrent may still be seeding them): on C:, or on the movie annex, an external HDD that is often disconnected,
# at the same path, <movie annex>\<MOVIE_STORE>\<release name>. The view, <MOVIE_VIEW>\<canonical name>, holds only
# junctions to them, named from EMDB and the files (movienames.py); EMDB's Locations point through the view. A
# junction to the movie annex points to its volume rather than its drive letter. qBittorrent downloads into
# Large\Videos\Downloading, moving a complete download into the store; albumflow ignores Downloading.
MOVIE_STORE = R"Large\Videos\Store"
MOVIE_VIEW = R"Large\Videos\Videos"
MOVIE_ANNEX_MARKER = ".movie-annex"
MOVIE_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm"}
# EMDB (Eric's Movie Database): its database, and the folder the backups made in EMDB go to
EMDB_DATABASE = Path(os.environ.get("ALBUMFLOW_EMDB", R"C:\Users\senso\AppData\Roaming\EMDB\emdb.dat"))
EMDB_BACKUPS = Path(R"C:\Users\senso\Documents\EMDB\Backups")
# EMDB doesn't show a Location longer than this as playable
EMDB_MAX_PATH = 259
# Where the movie annex had the movies before it held the store (as EMDB's Locations still say); movie-status --fix
# moves them to <movie annex>\<MOVIE_STORE> and points the Locations to the view. Locations into the old places,
# <old root>\<release name>\..., are taken as pointing into the store.
MOVIE_ANNEX_OLD_ROOT = "Videos"
EMDB_OLD_MOVIE_ROOTS = [R"D:\Videos", R"S:\media\sensorflo\7e0fb0f5-7af1-4290-8fe5-e6a35b6c12b8\Videos",
                        R"C:\Users\senso\Large\Videos"]
# MediaInfo's command line version, and what albumflow keeps of its findings, so names can be made while the movie
# annex is disconnected
MEDIAINFO = Path(os.environ.get("ALBUMFLOW_MEDIAINFO", R"C:\Program Files\MediaInfo CLI\MediaInfo.exe"))
MEDIAINFO_CACHE = Path(R"C:\Users\senso\AppData\Local\albumflow\mediainfo.json")
# The languages a movie's name lists, after its original language (its first audio track's); others are counted
MOVIE_LANGUAGES = ["en", "de"]

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

"""What a movie file holds, by MediaInfo (command line version), condensed to what its name says.

The findings are kept in config.MEDIAINFO_CACHE, by path, size and modification time, so names can be made while the
movie annex is disconnected, and a run over hundreds of movies doesn't read each file again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from albumflow import config
from albumflow.files import AlbumflowError

KEEP = ["@type", "Format", "Format_Commercial_IfAny", "Format_AdditionalFeatures", "Format_Profile", "Width", "Height",
        "HDR_Format", "BitRate", "BitRate_Nominal", "OverallBitRate", "StreamSize", "Duration", "Channels", "Language",
        "Title", "Default", "Forced", "Compression_Mode"]

LOSSLESS = ("TrueHD", "DTS-HD MA", "DTS:X", "FLAC", "PCM", "ALAC")
LANGUAGE_NAMES = {"english": "en", "german": "de", "deutsch": "de", "french": "fr", "francais": "fr", "italian": "it",
                  "italiano": "it", "spanish": "es", "espanol": "es", "russian": "ru", "japanese": "ja",
                  "korean": "ko", "chinese": "zh", "swedish": "sv", "portuguese": "pt", "dutch": "nl"}


# --- reading ---

def source(main_file: Path) -> Path:
    """What to ask MediaInfo: for a Blu-ray folder (the main file in BDMV\\STREAM) its largest playlist, which
    knows the tracks' languages, unlike the stream file; else the file itself."""
    if main_file.parent.name.upper() == "STREAM" and main_file.parent.parent.name.upper() == "BDMV":
        playlists = list((main_file.parent.parent / "PLAYLIST").glob("*.mpls"))
        if playlists:
            return max(playlists, key=lambda p: p.stat().st_size)
    return main_file


def run(path: Path) -> list[dict]:
    if not config.MEDIAINFO.is_file():
        raise AlbumflowError(f"MediaInfo (command line version) not found at {config.MEDIAINFO}")
    out = subprocess.run([str(config.MEDIAINFO), "--Output=JSON", str(path)], capture_output=True, text=True,
                         encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    data = json.loads(out or "{}")
    media = (data[0] if isinstance(data, list) and data else data).get("media") or {}
    return [{k: t[k] for k in KEEP if k in t} for t in media.get("track", [])]


def load_cache() -> dict:
    try:
        return json.loads(config.MEDIAINFO_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    config.MEDIAINFO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temp = config.MEDIAINFO_CACHE.with_name(config.MEDIAINFO_CACHE.name + ".new")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(temp, config.MEDIAINFO_CACHE)


class Reader:
    """MediaInfo's tracks of movie files, from the cache where it still matches; save() keeps new findings.
    Keyed by the release name and the file's path in it, so a movie keeps its entry wherever it is."""

    def __init__(self):
        self.cache = load_cache()
        self.changed = False

    def key(self, release: str, rel: Path) -> str:
        return f"{release}\\{rel}"

    def cached(self, release: str, rel: Path) -> list[dict] | None:
        entry = self.cache.get(self.key(release, rel))
        return entry["tracks"] if entry else None

    def tracks(self, release: str, rel: Path, file: Path) -> list[dict]:
        st = file.stat()
        entry = self.cache.get(self.key(release, rel))
        if entry and entry["size"] == st.st_size and entry["mtime"] == int(st.st_mtime):
            return entry["tracks"]
        tracks = run(source(file))
        self.cache[self.key(release, rel)] = {"size": st.st_size, "mtime": int(st.st_mtime), "tracks": tracks}
        self.changed = True
        return tracks

    def save(self) -> None:
        if self.changed:
            save_cache(self.cache)
            self.changed = False


# --- describing ---

def of_type(tracks: list[dict], kind: str) -> list[dict]:
    return [t for t in tracks if t.get("@type") == kind]


def number(value) -> float:
    try:
        return float(str(value).split(" / ")[0])
    except ValueError:
        return 0.0


def resolution(video: dict) -> str:
    width, height = number(video.get("Width")), number(video.get("Height"))
    for name, w, h in (("2160p", 3800, 2100), ("1080p", 1900, 1060), ("720p", 1260, 700)):
        if width >= w or height >= h:  # cropped movies keep their width, e.g. 1920x800 is 1080p
            return name
    return f"{int(height)}p" if height else ""


def hdr(video: dict) -> str:
    text = video.get("HDR_Format", "")
    found = []
    if "Dolby Vision" in text:
        found.append("DV")
    if "HDR10+" in text or "2094" in text:
        found.append("HDR10+")
    elif "HDR10" in text or "2086" in text:
        found.append("HDR10")
    elif "HLG" in text:
        found.append("HLG")
    return " ".join(found)


def video_codec(video: dict) -> str:
    fmt = video.get("Format", "")
    return {"MPEG Video": "MPEG-2", "MPEG-4 Visual": "MPEG-4", "VC-1": "VC-1"}.get(fmt, fmt)


def video_mbps(video: dict, general: dict) -> str:
    rate = number(video.get("BitRate")) or number(video.get("BitRate_Nominal"))
    if not rate and number(video.get("StreamSize")) and number(video.get("Duration")):
        rate = number(video["StreamSize"]) * 8 / number(video["Duration"])
    rate = rate or number(general.get("OverallBitRate"))
    return f"{round(rate / 1e6)}Mbps" if rate else ""


def audio_format(track: dict) -> str:
    commercial = track.get("Format_Commercial_IfAny", "")
    features = track.get("Format_AdditionalFeatures", "")
    fmt = track.get("Format", "")
    if "TrueHD" in commercial or fmt == "MLP FBA":
        return "TrueHD Atmos" if "Atmos" in commercial or "16-ch" in features else "TrueHD"
    if "DTS-HD Master Audio" in commercial or "XLL" in features:
        return "DTS:X" if "XLL X" in features or "DTS:X" in commercial else "DTS-HD MA"
    if fmt == "DTS":
        return "DTS"
    if fmt == "E-AC-3":
        return "EAC3 Atmos" if "JOC" in features or "Atmos" in commercial else "EAC3"
    if fmt == "AC-3":
        return "AC3"
    if fmt == "MPEG Audio":
        return "MP3" if track.get("Format_Profile", "").endswith("3") else "MP2"
    return fmt


def channels(track: dict) -> str:
    n = int(number(track.get("Channels")))
    return f"{n - 1}.1" if n >= 6 else f"{n}.0" if n else ""


def lossless(track: dict) -> bool:
    return audio_format(track).startswith(LOSSLESS) or track.get("Compression_Mode") == "Lossless"


def describe_audio(track: dict) -> str:
    text = f"{audio_format(track)} {channels(track)}".strip()
    rate = number(track.get("BitRate"))
    return f"{text} {round(rate / 1000)}kbps" if rate and not lossless(track) else text


def language(track: dict) -> str:
    return (track.get("Language") or "und").split("-")[0].lower()


def is_commentary(track: dict) -> bool:
    return "comment" in track.get("Title", "").lower()


def is_forced(track: dict) -> bool:
    return track.get("Forced") == "Yes" or "forced" in track.get("Title", "").lower()


def best(tracks: list[dict]) -> dict:
    """The best of a language's audio tracks: lossless, then most channels, then Atmos or DTS:X, then the default."""
    return max(tracks, key=lambda t: (lossless(t), number(t.get("Channels")),
                                      audio_format(t) in ("TrueHD Atmos", "DTS:X", "EAC3 Atmos"),
                                      t.get("Default") == "Yes"))


def ordered(languages: list[str], original: str) -> tuple[list[str], int]:
    """The named languages (original first, then config.MOVIE_LANGUAGES) among languages, and how many others."""
    named = [lang for lang in dict.fromkeys([original, *config.MOVIE_LANGUAGES]) if lang in languages]
    return named, len(set(languages) - set(named))


def describe(tracks: list[dict]) -> str | None:
    """E.g. "1080p AVC 29Mbps, ko TrueHD Atmos 7.1, en de AC3 5.1 640kbps, +1, sub en de +3"; None without a video
    track (e.g. a download not complete yet)."""
    videos = of_type(tracks, "Video")
    if not videos:
        return None
    general = (of_type(tracks, "General") or [{}])[0]
    video = videos[0]
    parts = [" ".join(p for p in (resolution(video), hdr(video), video_codec(video), video_mbps(video, general)) if p)]
    audios = [t for t in of_type(tracks, "Audio") if not is_commentary(t)]
    if audios:
        original = language(audios[0])
        by_language: dict[str, list[dict]] = {}
        for t in audios:
            by_language.setdefault(language(t), []).append(t)
        named, others = ordered(list(by_language), original)
        groups: list[tuple[list[str], str]] = []
        for lang in named:
            text = describe_audio(best(by_language[lang]))
            if groups and groups[-1][1] == text:
                groups[-1][0].append(lang)
            else:
                groups.append(([lang], text))
        parts += [f"{' '.join(langs)} {text}" for langs, text in groups]
        if others:
            parts.append(f"+{others}")
        subs = list(dict.fromkeys(language(t) for t in of_type(tracks, "Text") if not is_forced(t)))
        if subs:
            named_subs, other_subs = ordered(subs, original)
            parts.append(" ".join(["sub", *named_subs] + ([f"+{other_subs}"] if other_subs else [])))
    return ", ".join(parts)


def doubtful_languages(tracks: list[dict]) -> list[str]:
    """Audio and subtitle tracks whose language tag is missing, or contradicts their title (e.g. "German DD 5.1"
    tagged en)."""
    found = []
    for t in of_type(tracks, "Audio") + of_type(tracks, "Text"):
        lang = language(t)
        named = {code for name, code in LANGUAGE_NAMES.items()
                 if re.search(rf"\b{name}\b", t.get("Title", "").lower())}
        if lang == "und":
            found.append(f"{t['@type'].lower()} track without a language ({t.get('Title', '')!r})")
        elif named and lang not in named:
            found.append(f"{t['@type'].lower()} track tagged {lang}, titled {t.get('Title', '')!r}")
    return found

"""Mirror a local folder to a Google Photos album.

After a sync the album contains exactly the folder's files, identified by
filename. Photos that leave the album are moved to the "To trash" album,
because the API can't trash anything; emptying that album is a manual step.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from albumflow.gphotos_api import ApiError, GPhotos, MediaItem
from albumflow.paths import state_dir

TRASH_TITLE = "🗑 To trash (albumflow)"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}


@dataclass
class Plan:
    keep: dict[str, str] = field(default_factory=dict)  # filename -> media id
    upload: list[str] = field(default_factory=list)  # filenames
    trash: list[str] = field(default_factory=list)  # media ids


def make_plan(local: dict[str, str], remote: list[MediaItem], known_hashes: dict[str, str]) -> Plan:
    """Decide what to do. local: filename -> sha256; known_hashes: media id -> sha256."""
    plan = Plan()
    for item in remote:
        name = item.filename
        if name in local and name not in plan.keep and known_hashes.get(item.id) == local[name]:
            plan.keep[name] = item.id
        else:
            plan.trash.append(item.id)
    plan.upload = sorted(name for name in local if name not in plan.keep)
    return plan


def local_files(folder: Path) -> dict[str, Path]:
    files = {}
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            files[path.name] = path
    return files


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class State:
    """Local memory of an album: its id and the content hash of each item.

    The album's actual contents are always read from Google; the state only
    adds what Google can't tell us (which file version an item was made from).
    """

    def __init__(self, key: str):
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", key).strip(" .") or "_"
        self.path = state_dir("gphotos") / f"{safe}.json"
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.album_id: str | None = data.get("album_id")
        self.items: dict[str, dict] = data.get("items", {})  # media id -> {filename, sha256}

    def save(self) -> None:
        data = {"album_id": self.album_id, "items": self.items}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def resolve_album(api: GPhotos, state: State, title: str, create: bool) -> tuple[str | None, bool]:
    """Find the album by stored id, else by title, else create it. Returns (id, created)."""
    if state.album_id and api.get_album(state.album_id):
        return state.album_id, False
    matches = [a for a in api.list_albums() if a.get("title") == title]
    if len(matches) > 1:
        raise ApiError(f'There are {len(matches)} albums titled "{title}" created by albumflow; '
                       "remove the extra ones in Google Photos.")
    if matches:
        album_id, created = matches[0]["id"], False
    elif create:
        album_id, created = api.create_album(title)["id"], True
    else:
        return None, False
    state.album_id = album_id
    state.save()
    return album_id, created


def sync(api: GPhotos, folder: Path, title: str, dry_run: bool = False, log=print) -> int:
    files = local_files(folder)
    if not files:
        raise ApiError(f"No images found in {folder}")
    log(f'Syncing {len(files)} photos from {folder} to Google Photos album "{title}"')
    local = {name: sha256(path) for name, path in files.items()}

    state = State(title)
    album_id, created = resolve_album(api, state, title, create=not dry_run)
    remote = api.album_items(album_id) if album_id else []
    known = {mid: info["sha256"] for mid, info in state.items.items()}
    plan = make_plan(local, remote, known)

    log(f"  unchanged: {len(plan.keep)}, to upload: {len(plan.upload)}, to trash: {len(plan.trash)}")
    if dry_run:
        if not album_id:
            log("  (album does not exist yet and would be created)")
        for name in plan.upload:
            log(f"  upload  {name}")
        names = {m.id: m.filename for m in remote}
        for mid in plan.trash:
            log(f"  trash   {names[mid]}")
        return 0

    # 1. Upload first, so the album is never missing a photo mid-sync.
    tokens = []
    for i, name in enumerate(plan.upload, 1):
        log(f"  [{i}/{len(plan.upload)}] uploading {name}")
        tokens.append((name, api.upload(files[name])))
    created_ids, errors = api.create_items(album_id, tokens) if tokens else ({}, [])

    final = {**plan.keep, **created_ids}  # filename -> media id
    state.items = {mid: {"filename": name, "sha256": local[name]} for name, mid in final.items()}
    state.save()

    # Google may deduplicate an identical upload into an item we were about
    # to trash; never trash anything that is still in use.
    final_ids = set(final.values())
    to_trash = [mid for mid in plan.trash if mid not in final_ids]

    # 2. Move outdated items: into "To trash" first, then out of the album,
    #    so an interrupted sync never leaves an item in no album at all.
    trash_state = State("_trash")
    trash_id, _ = resolve_album(api, trash_state, TRASH_TITLE, create=True)
    in_trash = {m.id for m in api.album_items(trash_id)}
    rescued = sorted(final_ids & in_trash)
    if rescued:
        api.remove_from_album(trash_id, rescued)
        log(f"  took {len(rescued)} photo(s) back out of the trash album (re-used by Google)")
    if to_trash:
        api.add_to_album(trash_id, to_trash)
        api.remove_from_album(album_id, to_trash)

    trash_count = len((in_trash - set(rescued)) | set(to_trash))
    log(f"Done: {len(plan.keep)} unchanged, {len(created_ids)} uploaded, {len(to_trash)} moved to trash.")

    todos = []
    if created:
        todos.append(f'Share the new album "{title}" in Google Photos, and set its sort order to "Oldest first".')
    if trash_count:
        todos.append(f'Empty "{TRASH_TITLE}" in Google Photos ({trash_count} photos): '
                     "open it, select all, move to trash.")
    if errors:
        todos.append("Run the sync again; these photos failed:\n      " + "\n      ".join(errors))
    if todos:
        log("To do:")
        for todo in todos:
            log(f"  - {todo}")
    return 1 if errors else 0

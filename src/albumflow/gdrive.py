"""Thin client for the Google Drive API: deleting files of the cloud folder permanently.

The cloud folder (config.CLOUD_PICTURES) is written through Google Drive for desktop (G:). Deleting a
file there moves it to Drive's trash, where it still counts against the storage for 30 days. Deleting it
through the API instead frees the space right away; Drive for desktop then removes the local copy.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import AuthorizedSession

from albumflow import gphotos_api

BASE = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"


def _quote(name: str) -> str:
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Drive:
    def __init__(self, creds):
        self.s = AuthorizedSession(creds)

    def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        r = self.s.request(method, f"{BASE}/{path}", params=params, timeout=120)
        if r.status_code >= 400:
            raise gphotos_api.ApiError(f"Google Drive {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def _list(self, query: str) -> list[dict]:
        found, token = [], None
        while True:
            params = {"q": query, "fields": "nextPageToken, files(id, name, size)", "pageSize": 1000,
                      **({"pageToken": token} if token else {})}
            data = self._call("GET", "files", params)
            found += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return found

    def folder(self, parts: tuple[str, ...]) -> str | None:
        """The ID of the folder at this path below My Drive; None if it isn't there (or ambiguous)."""
        folder = "root"
        for name in parts:
            hits = self._list(f"{_quote(folder)} in parents and name = {_quote(name)} "
                              f"and mimeType = '{FOLDER}' and trashed = false")
            if len(hits) != 1:
                return None
            folder = hits[0]["id"]
        return folder

    def files(self, folder: str) -> dict[str, tuple[str, int]]:
        """name -> (ID, size) of the files directly in the folder (names occurring twice are left out)."""
        hits = self._list(f"{_quote(folder)} in parents and mimeType != '{FOLDER}' and trashed = false")
        names = [h["name"] for h in hits]
        return {h["name"]: (h["id"], int(h.get("size", -1))) for h in hits if names.count(h["name"]) == 1}

    def delete(self, file_id: str) -> None:
        """Delete permanently, not to the trash."""
        self._call("DELETE", f"files/{file_id}")


def drive() -> Drive | None:
    """A Drive client, or None if not logged in with the Drive permission (see google-login)."""
    try:
        return Drive(gphotos_api.credentials(interactive=False))
    except gphotos_api.ApiError:
        return None


def delete_permanently(client: Drive, my_drive: Path, files: list[Path]) -> list[Path]:
    """Delete these local files of one folder below my_drive (the local folder of My Drive) permanently via the
    API, those it has with the same size. Returns those it deleted; the local copies are left to the caller."""
    folder = client.folder(files[0].parent.relative_to(my_drive).parts) if files else None
    known = client.files(folder) if folder else {}
    deleted = []
    for path in files:
        file_id, size = known.get(path.name, (None, None))
        if file_id and size == path.stat().st_size:
            client.delete(file_id)
            deleted.append(path)
    return deleted

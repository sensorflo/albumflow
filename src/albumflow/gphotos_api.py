"""Thin client for the Google Photos Library API.

Since 2025-03-31 the API only sees albums and media items created by this app,
and it can neither trash, delete nor share anything.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from albumflow.files import AlbumflowError
from albumflow.paths import config_dir

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
    # Google Drive: deleting cloud copies permanently (gdrive.py); files written through Google Drive for
    # desktop aren't the app's own, so the narrower drive.file scope wouldn't see them
    "https://www.googleapis.com/auth/drive",
]
BASE = "https://photoslibrary.googleapis.com/v1"
BATCH = 50  # API limit for batchCreate / batchAdd / batchRemove


class ApiError(AlbumflowError):
    pass


@dataclass
class MediaItem:
    id: str
    filename: str


def _chunks(items: list, size: int = BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def credentials(interactive: bool = True) -> Credentials:
    token_file = config_dir() / "google_token.json"
    secret_file = config_dir() / "google_client_secret.json"
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and not creds.has_scopes(SCOPES):  # logged in before a permission was added
        creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise ApiError("Not logged in to Google (or not with all permissions). Run: albumflow google-login")
        if not secret_file.exists():
            raise ApiError(f"Google client file missing: {secret_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


class GPhotos:
    def __init__(self, creds: Credentials):
        self.s = AuthorizedSession(creds)

    def _call(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        r = self.s.request(method, f"{BASE}/{path}", json=json, params=params, timeout=120)
        if not r.ok:
            raise ApiError(f"{method} {path} failed: HTTP {r.status_code} {r.text}")
        return r.json() if r.content else {}

    # --- albums ---

    def list_albums(self) -> list[dict]:
        albums, token = [], None
        while True:
            params = {"pageSize": 50, **({"pageToken": token} if token else {})}
            data = self._call("GET", "albums", params=params)
            albums += data.get("albums", [])
            token = data.get("nextPageToken")
            if not token:
                return albums

    def get_album(self, album_id: str) -> dict | None:
        r = self.s.get(f"{BASE}/albums/{album_id}", timeout=60)
        if r.status_code in (400, 403, 404):
            return None
        if not r.ok:
            raise ApiError(f"GET album failed: HTTP {r.status_code} {r.text}")
        return r.json()

    def create_album(self, title: str) -> dict:
        return self._call("POST", "albums", json={"album": {"title": title}})

    def album_items(self, album_id: str) -> list[MediaItem]:
        items, token = [], None
        while True:
            body = {"albumId": album_id, "pageSize": 100, **({"pageToken": token} if token else {})}
            data = self._call("POST", "mediaItems:search", json=body)
            items += [MediaItem(m["id"], m["filename"]) for m in data.get("mediaItems", [])]
            token = data.get("nextPageToken")
            if not token:
                return items

    def add_to_album(self, album_id: str, media_ids: list[str]) -> None:
        for chunk in _chunks(media_ids):
            self._call("POST", f"albums/{album_id}:batchAddMediaItems", json={"mediaItemIds": chunk})

    def remove_from_album(self, album_id: str, media_ids: list[str]) -> None:
        for chunk in _chunks(media_ids):
            self._call("POST", f"albums/{album_id}:batchRemoveMediaItems", json={"mediaItemIds": chunk})

    # --- uploads ---

    def upload(self, path: Path) -> str:
        """Upload the bytes of one file, return an upload token."""
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        r = self.s.post(
            f"{BASE}/uploads",
            data=path.read_bytes(),
            headers={
                "Content-type": "application/octet-stream",
                "X-Goog-Upload-Content-Type": mime,
                "X-Goog-Upload-Protocol": "raw",
                "X-Goog-Upload-File-Name": path.name,
            },
            timeout=600,
        )
        if not r.ok:
            raise ApiError(f"Upload of {path.name} failed: HTTP {r.status_code} {r.text}")
        return r.text

    def create_items(self, album_id: str, uploads: list[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
        """Turn (filename, upload token) pairs into media items in the album.

        Returns (filename -> media item id, error messages).
        """
        created, errors = {}, []
        for chunk in _chunks(uploads):
            body = {
                "albumId": album_id,
                "newMediaItems": [
                    {"simpleMediaItem": {"uploadToken": token, "fileName": name}} for name, token in chunk
                ],
            }
            data = self._call("POST", "mediaItems:batchCreate", json=body)
            for (name, _), result in zip(chunk, data.get("newMediaItemResults", [])):
                status = result.get("status", {})
                item = result.get("mediaItem")
                if item and status.get("code", 0) == 0:
                    created[name] = item["id"]
                else:
                    errors.append(f"{name}: {status.get('message', 'unknown error')}")
        return created, errors

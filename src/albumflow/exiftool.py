"""One long-running exiftool process, queried file by file.

Starting exiftool takes a few tenths of a second on Windows, which adds up to
minutes over thousands of photos. With -stay_open it starts once and then
answers each request in milliseconds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from albumflow import config
from albumflow.files import AlbumflowError


class ExifTool:
    def __init__(self):
        self.proc = subprocess.Popen(
            [config.exiftool(), "-stay_open", "True", "-@", "-"],
            # Binary pipes: a text-mode pipe would send Windows line endings,
            # and exiftool would not recognize "-execute" followed by "\r".
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def __enter__(self) -> ExifTool:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"-stay_open\nFalse\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()

    def _run(self, *args: str) -> str:
        """Run one exiftool command, return its output."""
        try:
            self.proc.stdin.write(("\n".join(args) + "\n-execute\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise AlbumflowError(f"exiftool stopped unexpectedly: {e}") from None
        lines = []
        while (line := self.proc.stdout.readline()) and line.strip() != b"{ready}":
            lines.append(line)
        if not line:
            raise AlbumflowError("exiftool stopped unexpectedly")
        return b"".join(lines).decode("utf-8", errors="replace")

    def capture_time(self, path: Path) -> str:
        """Capture time as YYYYMMDDTHHMMSS, or "" if the file has none."""
        # No -q here: in -stay_open mode it also suppresses the {ready} line.
        out = self._run("-j", "-fast2", "-charset", "filename=utf8",
                        "-DateTimeOriginal", "-d", "%Y%m%dT%H%M%S", str(path))
        entries = json.loads(out) if out.strip() else []
        value = entries[0].get("DateTimeOriginal", "") if entries else ""
        return value if len(value) == 15 else ""

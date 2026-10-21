"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from albumflow import gphotos_api, gphotos_sync


def _api(interactive: bool = False) -> gphotos_api.GPhotos:
    return gphotos_api.GPhotos(gphotos_api.credentials(interactive=interactive))


def cmd_gphotos_login(args) -> int:
    _api(interactive=True)
    print("Logged in to Google Photos.")
    return 0


def cmd_gphotos_albums(args) -> int:
    for album in _api().list_albums():
        print(f'{album.get("mediaItemsCount", "0"):>6}  {album.get("title", "")}')
    return 0


def cmd_gphotos_sync(args) -> int:
    folder = Path(args.folder)
    title = args.title or folder.name
    return gphotos_sync.sync(_api(), folder, title, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    # Album titles and filenames may contain characters the Windows console
    # code page can't show; never crash on output.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="albumflow",
                                     epilog="albumflow <subcommand> -h shows the options of a subcommand.")
    sub = parser.add_subparsers(dest="target", required=True)

    p = sub.add_parser("gphotos-login", help="log in to Google Photos (opens the browser)")
    p.set_defaults(func=cmd_gphotos_login)

    p = sub.add_parser("gphotos-albums", help="list the Google Photos albums created by albumflow")
    p.set_defaults(func=cmd_gphotos_albums)

    p = sub.add_parser("gphotos-sync", help="make a Google Photos album an exact mirror of a folder")
    p.add_argument("-f", "--folder", required=True, help="folder with the photos to mirror")
    p.add_argument("--title", help="album title (default: folder name)")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.set_defaults(func=cmd_gphotos_sync)

    if not (sys.argv[1:] if argv is None else argv):  # no subcommand: the same as -h
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except gphotos_api.ApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Ctrl+C: no traceback
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

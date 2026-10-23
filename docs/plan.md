# albumflow – plan

Status: draft for review (2026-10-10). Nothing is implemented yet.

## Goal

Automate the life of a photo album in Lightroom Classic: import, publishing to
several targets with per-target export settings, and keeping those targets up to
date when photos change. Semi-automatic is fine; manual steps are acceptable as
long as they are explicit and could be automated later.

## Concepts

### Album

The generic unit. An album is identified by its **folder name** (e.g.
`secret-castle-2026`), regardless of where that folder lives. All folders with
that name under the configured picture roots together form the album. This
keeps it robust against moving folders (e.g. `new\party\<album>` →
`Originals\party\<album>`).

### Photo identity and syncing

Within an album, a photo is identified by its **exported filename** – the same
name visitors see and order by. Lightroom's internal IDs are not used.

Every sync makes the target an **exact mirror** of the album:

1. Lightroom freshly exports all photos of the album that belong to the target
   into a staging folder.
2. albumflow compares that folder with the target, by filename:
   - new name → upload
   - same name, different file → upload new version, old one goes to trash
   - name only on the target → goes to trash
   - same name, same file → nothing

So removing and re-adding an album in Lightroom, moving folders, renaming
photos or even losing the catalog does no harm: the next sync makes the target
match again. A renamed photo (e.g. faked capture date) is a new photo to the
outside, so old name → trash, new name → upload, which is correct.

Priorities: reliability, then simplicity; efficiency is a distant last
(re-exporting the whole album on every sync is fine).

Two photos with the same filename in one album (e.g. a virtual copy) stop the
sync with a clear message.

To check in milestone 1: whether Lightroom exports an unchanged photo
byte-identically. If not, compare image content instead, so unchanged photos
aren't re-uploaded.

### Profile

Each album has a profile, defined in a config file, not in code. A profile says
what happens at import (keyword, develop preset) and which publish targets
apply. Only the `party` profile is built for now; others (`hiking`, `holiday`,
...) are added when needed, ideally as config only.

| Profile | Import keyword / preset   | Publish targets                                      |
|---------|---------------------------|------------------------------------------------------|
| party   | `Party/Goa`, party preset | Google official, Google unprocessed, WordPress, private |
| others  | later                     | later                                                |

### Targets and keyword rules

Keyword names live in config (the `NotForPublic` tree may be renamed to
`NotForMainAlbum` later).

| Target                         | Contains                                                    | Export settings              |
|--------------------------------|-------------------------------------------------------------|------------------------------|
| Google Photos – official album | photos without any `NotForPublic/*` keyword                 | full quality                 |
| Google Photos – "unprocessed"  | photos with `NotForPublic/OnlyForOthers`                    | very low resolution          |
| WordPress – Modula gallery     | photos without any `NotForPublic/*` keyword                 | max size / max resolution    |
| WordPress – overview page      | one cover per album                                         | 16:10, watermarked           |
| Private local album            | photos without keyword + photos with `NotForPublic/OnlyForMe` | watermark chosen per album |

A photo with both `OnlyForOthers` and `OnlyForMe` goes to both the
"unprocessed" and the private album.

### Checks before publishing

The tool stops with a clear message rather than guessing:

- A photo tagged with plain `NotForPublic` (only its children are valid).
- A photo whose Title is not the album (folder) name. For now: check and list
  only. Later maybe: set it automatically.

### Filenames

Visitors order photos by filename (see "Wo ist mein Photo?"), so the filename
`YYYYMMDDTHHMMSS-_DSCxxxx` must be identical in every target and stable across
updates.

## Architecture

- **Lightroom plugin (Lua)**: menu commands such as "sync album to target"
  (later "sync all targets", "import"). It finds all folders of the album,
  runs the checks, picks the photos for the target by keyword, exports them
  with the target's export settings into a staging folder and calls the Python
  tool. No Lightroom publish services are needed for the mirror approach.
- **Per-album settings** (e.g. homepage URL, Google share links, watermark
  choice) are stored in a plain file per album. Editing them via the publish
  service GUI may come later.
- **Credentials, tokens and state** live outside the repo in
  `%APPDATA%\albumflow` (e.g. `google_client_secret.json`).
- **Python tool**: mirrors a staging folder to a target. It can also be run
  by hand on any folder, independent of Lightroom. It keeps a state file per
  album and target (filename → remote ID + content hash), but treats the
  target itself as the truth wherever the target's API allows listing it.
- **Manual steps as to-dos**: every step that is manual for now is reported
  by the tool as an explicit to-do (e.g. "share album X and paste the link",
  "empty 🗑 To trash (12 items)"). Automating one later means replacing just
  that spot.

## Targets in detail

### Google Photos

- The Library API only allows managing albums and media created by the app
  itself. Existing, manually created albums are left untouched.
- The tool creates the album, uploads new photos, and replaces changed photos
  by uploading the new version.
- **Trashing**: the API cannot trash or delete. Photos removed in Lightroom or
  replaced by a newer version are removed from the album and moved into an
  app-managed album "🗑 To trash". Manual step: open it, select all, move to
  trash. Invariant: every uploaded photo is in exactly one album or in
  "🗑 To trash", so nothing is unaccounted for.
- **Sharing**: the API no longer offers sharing (removed 2025-03-31). Manual step:
  share the album once in Google Photos and paste the link into the album's
  settings file. The link is later used on the WordPress page.
- Google Photos may deduplicate an identical re-upload into an existing media
  item. If such an item sits in "🗑 To trash", albumflow takes it out of there
  again, so emptying the trash never removes a photo that's still in use.
- Setup (once, by the user): Google Cloud project, enable the Photos Library
  API, create a "Desktop app" OAuth client.

### Private local album

Lightroom's built-in "Hard Drive" behaviour, or the same plugin writing to a
local folder. Watermark is chosen per album.

### WordPress (sensorflo.ch, hosted at Hostpoint)

- Normal WordPress install; authentication via application password.
- Uses Modula (Pro) for galleries and "Media Library Folders" for physical
  folders (`uploads/albums/<album>`, `uploads/covers`).
- A small own helper plugin on the WordPress side is likely the most reliable
  way to update Modula galleries and Media Library Folders records.
- **Album page** (generated from a template): title = album name; short German
  text linking to the event homepage and the official Google Photos album; the
  Modula gallery; closing note linking to "Wo ist mein Photo?" and the
  "unprocessed" Google Photos album. Example:
  https://sensorflo.ch/home/secret-castle-2026/
- **Overview page**: regular WordPress gallery, one item per album (cover
  image linking to the album page), kept up to date by the tool.

### Covers

For now: made manually (export, darktable, crop to 16:10, three watermark
modules) and picked up from a fixed location. Later maybe: a `cover` keyword
or virtual copy, cropped in Lightroom, exported by its own target, with the
three watermarks merged into one PNG overlay.

## Import from the camera card

Workflow: during a multi-day event or a holiday, the card is imported every
now and then (e.g. each morning) into the same album. Culling happens next in
FastRawViewer; Lightroom comes much later.

Files: `.ARW`, plus `.JPG` if the camera wrote any. Everything below `DCIM`.

- **`albumflow import-only <topic>/<album>`** (`io`) – for each photo, in this order:
  1. copy from `DCIM` to `<new>\<topic>\<album>\<original name>.part`;
  2. read its capture time with exiftool `-fast2`, on the PC copy (fast);
     one exiftool process (`-stay_open`) serves all photos, since starting
     exiftool per photo would cost minutes;
  3. rename the copy to `<capture time>-<original name>`
     (e.g. `20260828T120736-_DSC8045.ARW`);
  4. move the original on the card to `<card>:\Backup\<album>\<same new name>`.

  The topic folder must exist; the album folder too, unless `--create-album`.
  Without `--card`, it imports every inserted card (a drive with a `DCIM`
  folder; an empty one simply has nothing to do), one after the other. If no
  card is inserted at the start, it waits for one (polling every 2 s). The next
  card is only looked for once the current one is done, so it can be inserted
  while the current one is being imported. Finished cards are recognized by
  their volume serial number, so one left in the reader isn't imported again.
  Each finished card is ejected (unless `--no-eject`); a failed eject is only
  a warning. Before that, if less than 50% of the card is free, albumflow
  lists the other folders in `<card>:\Backup` (oldest first, with date, size
  and whether their files are on the backup SSD) and offers to delete some.
  A photo leaves `DCIM` only once a complete copy is on the PC. Re-running after
  an interruption is safe: a photo already on the PC with identical content is
  just moved on the card; different content stops the import.
- **`albumflow backup-raw <topic>/<album>`** (`br`) – separate, since the
  SSD isn't always connected. Copies the album folder from the PC to
  `<drive>:\for-backup-disc\<album>\` on the drive with a `.backup-destination`
  file in its root. Only adds files, never deletes or overwrites, so photos
  culled later stay backed up. At the end, the backup drive is safely removed
  (like "Safely Remove Hardware"; `--no-eject` keeps it), also after `import`.
- **`albumflow import <topic>/<album>`** (`i`) – `import-only` with `backup-raw`
  running in parallel: a background thread repeatedly backs up whatever is on
  the PC but not yet on the SSD, until the last card is done. The SSD may be
  connected later: the backup starts when it appears, and at the end albumflow
  waits for it (`backup-raw` alone waits too). If the backup fails,
  the import goes on and the backup error is reported at the end. Two progress
  bars; the backup total also counts photos still on the current card.
- `<topic>/<album>` (or `\`) may be abbreviated or misspelled: an exact match
  (ignoring case and space/`-`/`_`) is used directly, otherwise a numbered list
  of the best matches is shown (words contained in the name rank highest, then
  `difflib` similarity; ties by folder modification time, i.e. last import).
  Enter takes #1; with `-c` and no exact match, Enter creates the new album.
  No album given → the most recently modified albums.
- Python API with a thin command line on top, so a GUI can be added later.
- Importing when the primary backup drive is out of reach (done): photos
  count as safe once they reach the secondary or the cloud; until then they
  go to the primary and, in delta mode, the cloud.
  - `import` without the drive asks: wait for it, or go on without the
    backup (like `import-only`).
  - Importing marks the album "backup pending" (`backup_pending` in its
    `.albumflow.json`); a complete backup clears it. `import`, `backup-raw`
    and `update-*` print one reminder line naming such albums.
  - `albumflow backup-status`: the photos not on the backup drive (exact
    with the drive connected, else the pending albums), and what hasn't
    reached the secondary or the cloud.
  - `update-cloud` also copies the photos of pending albums from the PC
    (not the clock-corrected ones: those only exist once their original is
    on the drive), and works without the backup drive.
  - A full card offers to delete only old folders that reached the
    secondary or the cloud; others only with `--force-prune`.

## Camera clock correction

Goal: the photos of all cameras of an album sort in the order they were
taken. Exact time doesn't matter.

- Each camera photographs a clock at the start of a festival:
  `albumflow sync-clocks -r <photo>=<time on the clock> -r ... <topic>/<album>`
  (alias `sc`; without `-r` the clock readings are asked for, pair by pair:
  the photo suggested is the album's first photo, then the one after the
  previous clock photo (Enter takes it); the time is pre-filled with the photo's hour; any separator
  between hours, minutes and seconds)
  records each camera's offset from the clock (in `<album>\.albumflow.json`).
  Cameras are identified by model and serial number; only the model is shown.
- The import records each photo's camera in that file (same exiftool call as
  the capture time; ~6 ms per photo for the serial). For older albums,
  `sync-clocks` reads them once, with a progress bar.
- Corrections only happen while every camera of the album has a clock
  reading. A camera added later by an import pauses them, with a hint, until
  it has one. `--assume-in-sync MODEL` records that a camera without a clock
  photo is treated as in sync with the master.
- `--readings-from <topic/album>` takes over the readings of another album, e.g.
  of the same festival (offsets are constant per festival); loose topic/album
  with a list unless unambiguous; the album's own readings are kept.
- **Master time**: the time the master camera (`MASTER_CAMERA` in config,
  ILCE-9M2) would have shown. A camera's offset from the master is its offset
  from the clock minus the master's. Offsets are constant per festival.
- **Only photos that would be out of order are corrected**: sort by name, and
  for every neighbouring pair whose master times are the other way round,
  correct its non-master photo(s); repeat until no such pair is left. The
  master's photos are never changed; a photo far off but not mixed up stays.
- A correction shifts the capture time inside the RAW/JPEG (exiftool, whole
  seconds) to the master time and renames to `<master time>-<import name>`;
  sidecars (`.xmp`) follow. Correcting again replaces the prefix.
- A photo is only corrected once its original is on the backup SSD, which
  keeps the originals (import name and content), as does the card's Backup
  folder. `backup-raw` treats `<t>-X` as backed up if the SSD has `X`.
- Once an album has clock readings, `import` and `backup-raw` apply them after
  the backup, so later imports end up as if corrected afterwards.

Later:

- **Card inserted → imported automatically** (asks for topic and album).
- **Smarter card-space threshold**: instead of the fixed 50%, keep enough
  free space for one more day of shooting with that camera (each card is
  named after its camera). Estimate a camera's daily volume from past
  shooting, e.g. a high percentile of bytes per day from capture times and
  file sizes of the raws still kept for a couple of events.
- Lightroom import (Title, keyword, preset).

## The annex (more room for the home folder)

C: is getting full; S: (a 1 TB microSD that stays in the laptop) holds large,
rarely used folders, mostly albums. The annex **mirrors the home folder**:
`S:\<path>` is what would be `C:\Users\senso\<path>`. Its purpose is extra
room, not speed, hence the name.

- `albumflow to-annex [folder]` / `albumflow from-annex [folder]`; the folder is
  a path when it looks like one (`.`, `./foo`, `~/Music/x`, `C:\...`), else a
  loose album query; none: the most recent albums (for `from-annex`: among
  those in the annex). `--dry-run`, progress bar.
- A moved folder leaves a junction `<name> (annex)` at its place, so
  everything appears in one tree and a link is never mistaken for a folder.
  Lightroom loses such a folder once ("Find Missing Folder"; a hint is shown).
  Switching to identical names later is possible (rename the junctions).
- Never a direct move: copy to `<name>.part` with a move note, check file list
  and sizes, rename, delete the source, create/remove the link; a re-run
  continues an interrupted move. Junctions are only removed with `os.rmdir`;
  recursive deletes refuse to go through a junction. Refused while editors
  (Lightroom, darktable, ...) run, or without enough free space.
- `import`, `backup-raw` and `sync-clocks` don't offer albums in the annex.
- `to-annex` without a folder suggests what to move: candidates are the folders
  below `ANNEX_SUGGEST_ROOTS` that directly contain files (with everything
  below them); score = size in GB × (1 − e^(−age/180 days)), age = since the
  newest file was modified. The top 20 (`ANNEX_SUGGESTIONS`) are listed with Lightroom blockers and
  how much space moving them frees; the chosen ones are moved right away
  (Enter: none); a refused one is skipped, the others still moved.
- Lightroom: before moving, the catalog(s) in `LIGHTROOM_CATALOGS` are read
  (never written). The move is refused if any photo below the folder has
  metadata not saved to its file (`Adobe_images.sidecarStatus = 1`, the
  "metadata file needs to be updated" badge), or if there are virtual copies,
  whose edits exist only in the catalog; the affected folders are named,
  relative to the home folder. So the moved files hold everything on their
  own. Afterwards, a "Find Missing Folder" hint only if Lightroom knows the
  folder. A `--force` may come later. darktable: to be looked at (its XMP is
  the primary store).
- `albumflow annex-status [--fix]`: what's in the annex, and problems (missing
  link, broken link, in both places, interrupted move, no matching folder);
  `--fix` creates missing links after asking.
- Implemented in albumflow as a self-contained module (`annex.py`, only generic
  helpers), so it could become its own tool if it grows beyond photos.

To do:

- Reorganize S: to mirror the home folder: `S:\new` →
  `S:\Large\Pictures\Pictures\new`, then `annex-status --fix` links the
  existing albums; sort the other top-level folders (`Music`, `new-old`,
  `OnlyForOthers`, `rejected`) into place one by one.
- Marker file: rename `S:\.backup-source-sd` to `.annex`, with a short
  note describing itself as content; switch `backup.py` to it too (albumflow
  accepts both names until then).
- The annex treats every folder alike, `new\OnlyForOthers` included: it only
  exists because C: is too small; semantically there is one home folder tree.

## Responsibilities: albumflow and the backup tool

Decided 2026-10-23. The background:

- Conceptually the laptop has one disk; the annex only exists because C: is
  too small. albumflow solving that is good enough, since most of the data
  is albums (music, with a slight stretch, too).
- No disk is large enough to keep all raws forever (2k–5k per festival day),
  only those I know I keep: `Originals`. The raws in `new`, `new-old` and
  `OnlyForOthers` are kept only for a while, so restic excludes them; plain
  copies on the backup drives cover them instead, and old albums are purged
  there as new ones come in.
- While the secondary drive is out of reach and the laptop and the primary
  drive are at increased risk (holidays), the cloud doubles for the
  secondary: a restic delta, plus plain copies of the same albums the
  secondary lacks (the primary's `to-secondary`, i.e. the complete imports;
  holidays mean only a few hundred photos a day).

So:

- **The backup tool is the overall backup.** `backup` is what I call; it
  decides what runs when, e.g. the delta while `backup_all_includes_delta` is
  set in its settings, and owns the restic repos (primary, secondary, cloud),
  their checks, and `backup.pyz` for restores, which never needs albumflow.
  For the album raws it runs albumflow's CLI: `backup update-secondary` runs
  `albumflow update-secondary`, `backup backup-delta` runs
  `albumflow update-cloud`. A missing or failing albumflow makes the backup
  fail, never skipped silently. albumflow never calls backup. No wrapper
  scripts.
- **albumflow** owns `Backup\pictures-new` on every destination, and all
  knowledge about albums:
  - the primary drive: `to-secondary\<topic>\<album>` (`backup-raw`) and
    `copied-to-secondary\<topic>\<album>`
  - the secondary drive: `pictures-new\<topic>\<album>` (`update-secondary`:
    copy `to-secondary` there, verify, move the album to
    `copied-to-secondary`)
  - the cloud: `G:\My Drive\Backup\pictures-new\<topic>\<album>`
    (`update-cloud`: copy `to-secondary` there, and albums pending a backup
    from the PC; never moves or deletes)
  - purging (below), only by hand.
- **Shared contract:** only the drive markers and the rule "`Backup\` on a
  drive belongs to the tools, everything else is mine". Each tool defines the
  markers itself; a comment says the other tool uses the same ones (without
  repeating the names, which would only go stale).
- Order: (1) `update-secondary` (done; `backup-raw`, `import` and the clock
  correction count the originals in `copied-to-secondary` as on the backup
  too); (2) `update-cloud`, run by backup (done); (3) purging (done); (4) refusing
  `OnlyForOthers` (done).

## Purging albums from the backup drives

Both drives are always more or less full, so finished albums have to go.

- An album `<topic>\<album>` may be purged from a drive only if
  `Originals\<topic>\<album>` exists on the laptop (the album is finished,
  restic covers what was kept).
- Accepted loss: the drives hold the complete import, so afterwards the raws
  in `new\OnlyForOthers\<album>` exist only on the laptop (restic excludes
  `new`), and the culled ones nowhere.
- Hard to do by accident, but no extra information: only an explicit purge
  command, never part of another command, no `--yes`. It lists the finished
  albums on the drive (largest and oldest first, with size and date); the
  chosen ones are deleted after typing `delete`.
- Done: `albumflow purge-backup`, for each connected drive in turn. On the
  primary only `copied-to-secondary` is offered (what waits in
  `to-secondary` must reach the secondary first), on the secondary all of
  `pictures-new`. Order: size weighted by age, as for the annex. An album in
  `Originals` that is in the annex counts as finished too.

## OnlyForOthers

When an album is finished it moves from `new\<topic>\<album>` to
`Originals\<topic>\<album>`; photos tagged NotForPublic/OnlyForOthers go to
`new\OnlyForOthers\<album>`, kept as long as I want to spend the space; those
edited for others go to `Originals\OnlyForOthers\<album>`.

So `OnlyForOthers` is a sub-topic (the leftovers of a finished album), not a
topic of its own. The commands that start or advance an album refuse it:
`import`, `import-only`, `backup-raw` (its raws are on the drives already,
under the album's topic), `sync-clocks`. The annex treats it like any folder.

## Milestones

1. **Import from the camera card** and raw backup (done first, by choice).
2. **Google Photos** – official + "unprocessed" album. Sharing stays manual.
   - 2a: Python tool mirroring a folder to a Google Photos album (create,
     upload, replace, move to "🗑 To trash"), usable by hand.
   - 2b: Lightroom plugin: find album folders, keyword and title checks,
     export per target, call the Python tool.
3. **Private local album.**
4. **WordPress album page + Modula gallery.**
5. **WordPress overview page + covers.**
6. **Later**: clock correction, automatic import on card insert, Lightroom import, "publish all targets" command, cover automation, setting titles
   automatically, further automation of manual to-dos, more profiles, possibly
   a fully automatic trigger.

## Out of scope for now

- Existing albums: they are not migrated or modified.
- The workflow for finishing an album (moving folders to `Originals`, splitting
  off `OnlyForOthers`, see above) – to be reconsidered later.

## Open points

- WordPress mechanics (uploads into Media Library Folders, Modula data) –
  milestone 3.
- Exact export settings per target (sizes, quality, watermarks).
- Details of the import step, including how renaming is done today.
- `sync-clocks --dry-run` changes no photos, but still writes the album's
  state file: the camera cache (fine, it is only a cache) and also readings
  and assumptions given with `-r`, `--assume-in-sync` or `--readings-from`
  (not fine: a later real run uses them). Proposal: in a dry run, keep the
  latter in memory only, and say in the help "doesn't change photos or clock
  readings".
- Album choice only looks in `Pictures\new`. `Pictures\new-old` is
  technically the same (albums there may never be worked on; the split is
  only psychological), so album choice should look there too, listing those
  albums after the ones in `new`, always with the topic (which tells e.g.
  `party\X` and `OnlyForOthers\X` apart).
- SD cards: for consistency, the card's root `Backup` folder should have the
  same tree as everywhere else, `Backup\<topic>\<album>`, not just
  `Backup\<album>` as the import writes it now. Affects the import, card
  pruning (`old_backups`, matching by name), and the existing card folders.
- The primary knows what reached the secondary only as long as albums stay
  in `copied-to-secondary` while unfinished: `purge-backup` only offers
  finished albums, but a folder deleted there by hand (or an album copied to
  the secondary by hand, as before albumflow) leaves no trace, and
  `backup-status` then reports its photos as not on the backup drive.

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from albumflow import backup, card_import, config
from albumflow.files import AlbumflowError



def photos(folder):
    """The folder's files, without albumflow's own state file."""
    return [p for p in folder.iterdir() if not p.name.startswith(".")]


def make_photo(path, capture_time, color=(255, 0, 0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)
    subprocess.run([config.exiftool(), "-q", "-overwrite_original", f"-DateTimeOriginal={capture_time}", str(path)],
                   check=True)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(card_import, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(backup, "POLL_SECONDS", 0.01)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    card = tmp_path / "card"
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    (tmp_path / "new" / "party").mkdir(parents=True)
    make_photo(card / "DCIM" / "100MSDCF" / "_DSC0002.JPG", "2026:08:28 12:07:36", (0, 255, 0))
    make_photo(card / "DCIM" / "100MSDCF" / "_DSC0001.JPG", "2026:08:28 12:07:30")
    return card, tmp_path


def test_import_copies_renames_and_moves(setup):
    card, tmp = setup
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)

    pc = tmp / "new" / "party" / "fest"
    names = ["20260828T120730-_DSC0001.JPG", "20260828T120736-_DSC0002.JPG"]
    assert sorted(p.name for p in photos(pc)) == names
    assert sorted(p.name for p in (card / "Backup" / "fest").iterdir()) == names
    assert list((card / "DCIM").rglob("*.JPG")) == []


def first_photo(card, tmp):
    """(source on the card, its destination on the PC) of the earliest test photo."""
    return (card / "DCIM" / "100MSDCF" / "_DSC0001.JPG",
            tmp / "new" / "party" / "fest" / "20260828T120730-_DSC0001.JPG")


def test_rerun_after_interruption(setup):
    card, tmp = setup
    source, pc = first_photo(card, tmp)
    # simulate: first photo copied to the PC, but not yet moved on the card
    pc.parent.mkdir(parents=True)
    pc.write_bytes(source.read_bytes())
    (pc.parent / "_DSC0002.JPG.part").write_bytes(b"half")  # an interrupted copy

    lines = []
    card_import.import_only("party", "fest", card=card, create_album=True, log=lines.append)
    assert len(list((card / "Backup" / "fest").iterdir())) == 2
    assert sorted(p.name for p in photos(pc.parent)) == [
        "20260828T120730-_DSC0001.JPG", "20260828T120736-_DSC0002.JPG"]
    assert any("1 were already on the PC" in line for line in lines)


def test_conflicting_file_on_pc_stops(setup):
    card, tmp = setup
    source, pc = first_photo(card, tmp)
    pc.parent.mkdir(parents=True)
    pc.write_bytes(b"something else")

    with pytest.raises(AlbumflowError, match="different content"):
        card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    assert source.exists()  # original stays in DCIM
    assert not list(pc.parent.glob("*.part"))  # no temporary copy left behind


def test_photo_without_capture_time_stops(setup):
    card, tmp = setup
    Image.new("RGB", (8, 8)).save(card / "DCIM" / "100MSDCF" / "_DSC0000.JPG")  # no EXIF, sorted first
    with pytest.raises(AlbumflowError, match="No usable capture time"):
        card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    assert (card / "DCIM" / "100MSDCF" / "_DSC0000.JPG").exists()
    assert not list(photos(tmp / "new" / "party" / "fest"))


def test_backup_only_adds(setup):
    card, tmp = setup
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    drive = tmp / "ssd"
    drive.mkdir()
    backup.backup_new("party", "fest", drive=drive, log=lambda *_: None)

    pc = tmp / "new" / "party" / "fest"
    culled = sorted(photos(pc))[0]
    culled.unlink()
    backup.backup_new("party", "fest", drive=drive, log=lambda *_: None)
    assert len(list((drive / config.BACKUP_DIR / "party" / "fest").iterdir())) == 2


def test_topic_must_exist(setup):
    card, tmp = setup
    with pytest.raises(AlbumflowError, match="Topic folder does not exist"):
        card_import.import_only("hiking", "fest", card=card, create_album=True, log=lambda *_: None)


def test_album_must_exist_without_flag(setup):
    card, tmp = setup
    with pytest.raises(AlbumflowError, match="--create-album"):
        card_import.import_only("party", "fest", card=card, log=lambda *_: None)
    (tmp / "new" / "party" / "fest").mkdir()
    card_import.import_only("party", "fest", card=card, log=lambda *_: None)
    assert len(list(photos(tmp / "new" / "party" / "fest"))) == 2


def test_progress_reports_bytes(setup):
    card, tmp = setup
    total = sum(p.stat().st_size for p in (card / "DCIM").rglob("*.JPG"))
    calls = []
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None,
                           card_progress=lambda card: lambda done, tot, name: calls.append((done, tot, name)))
    assert calls[-1][0] == calls[-1][1] == total
    assert {name for _, _, name in calls} == {"_DSC0001.JPG", "_DSC0002.JPG"}  # new names come after the copy


def test_warns_about_files_left_in_dcim(setup):
    card, tmp = setup
    (card / "DCIM" / "100MSDCF" / "CLIP0001.MP4").write_bytes(b"video")
    for dry_run in (True, False):
        lines = []
        card_import.import_only("party", "fest", card=card, create_album=True, dry_run=dry_run, log=lines.append)
        warnings = [line for line in lines if line.startswith("Warning:")]
        assert len(warnings) == 1 and "1 file(s)" in warnings[0]
        assert any("CLIP0001.MP4" in line for line in lines)


def test_no_warning_when_dcim_empty(setup):
    card, tmp = setup
    lines = []
    card_import.import_only("party", "fest", card=card, create_album=True, log=lines.append)
    assert not any(line.startswith("Warning:") for line in lines)


def test_import_and_backup(setup):
    card, tmp = setup
    drive = tmp / "ssd"
    drive.mkdir()
    card_import.import_and_backup("party", "fest", card=card, drive=drive, create_album=True, log=lambda *_: None)
    assert len(list(photos(tmp / "new" / "party" / "fest"))) == 2
    assert len(list((drive / config.BACKUP_DIR / "party" / "fest").iterdir())) == 2
    assert backup.pending_albums() == [], "backed up along"


def test_import_without_backup_marks_the_album_pending(setup):
    card, tmp = setup
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    assert backup.pending_albums() == [("party", "fest")]


def test_backup_drive_connected_later(setup, monkeypatch):
    card, tmp = setup
    ssd = tmp / "ssd"
    ssd.mkdir()
    polls = []

    def drives():
        polls.append(1)
        if len(polls) > 3:  # plugged in after a while
            (ssd / config.BACKUP_MARKER).touch()
            return [ssd]
        return []

    monkeypatch.setattr(backup, "drives", drives)
    lines, found = [], []
    card_import.import_and_backup("party", "fest", card=card, create_album=True, log=lines.append,
                                  on_backup_drive=found.append)
    assert found == [ssd]
    assert len(list((ssd / config.BACKUP_DIR / "party" / "fest").iterdir())) == 2
    assert any(line.startswith("Waiting for the primary drive") for line in lines)


def test_imports_several_cards_one_after_the_other(setup, monkeypatch):
    card1, tmp = setup
    card2 = tmp / "card2"
    make_photo(card2 / "DCIM" / "100MSDCF" / "_DSC0003.JPG", "2026:08:29 10:00:00")
    inserted = [card1]

    def candidates():
        # card 2 is "inserted" while card 1 is being imported
        result = list(inserted)
        if card2 not in inserted:
            inserted.append(card2)
        return result

    monkeypatch.setattr(card_import, "card_candidates", candidates)
    lines = []
    items = card_import.import_only("party", "fest", create_album=True, log=lines.append)
    assert len(items) == 3
    assert len(list(photos(tmp / "new" / "party" / "fest"))) == 3
    assert len(list((card2 / "Backup" / "fest").iterdir())) == 1
    assert lines[-1] == "No more cards."


def test_waits_for_a_card(setup, monkeypatch):
    card, tmp = setup
    polls = []

    def candidates():
        polls.append(1)
        return [card] if len(polls) > 3 else []  # inserted after a while

    monkeypatch.setattr(card_import, "card_candidates", candidates)
    lines = []
    items = card_import.import_only("party", "fest", create_album=True, log=lines.append)
    assert len(items) == 2
    assert lines.count("Waiting for a camera card (a drive with a DCIM folder)...") == 1


def test_empty_dcim_is_a_card_with_nothing_to_do(setup, monkeypatch):
    card, tmp = setup
    empty = tmp / "empty-card"
    (empty / "DCIM").mkdir(parents=True)
    monkeypatch.setattr(card_import, "card_candidates", lambda: [empty, card])
    ejected = []
    monkeypatch.setattr(card_import, "eject", ejected.append)
    items = card_import.import_only("party", "fest", create_album=True, eject_cards=True, log=lambda *_: None)
    assert len(items) == 2
    assert ejected == [empty, card]


def test_finished_card_left_in_reader_is_not_imported_again(setup, monkeypatch):
    card, tmp = setup
    monkeypatch.setattr(card_import, "card_candidates", lambda: [card])  # never removed
    items = card_import.import_only("party", "fest", create_album=True, log=lambda *_: None)
    assert len(items) == 2  # and it returned instead of looping


def test_ejects_each_finished_card(setup, monkeypatch):
    card1, tmp = setup
    card2 = tmp / "card2"
    make_photo(card2 / "DCIM" / "100MSDCF" / "_DSC0003.JPG", "2026:08:29 10:00:00")
    monkeypatch.setattr(card_import, "card_candidates", lambda: [card1, card2])
    ejected = []
    monkeypatch.setattr(card_import, "eject", ejected.append)
    card_import.import_only("party", "fest", create_album=True, eject_cards=True, log=lambda *_: None)
    assert ejected == [card1, card2]


def test_failed_eject_is_only_a_warning(setup, monkeypatch):
    card, tmp = setup

    def fail(drive):
        raise AlbumflowError(f"Could not eject {drive} (still in use)")

    monkeypatch.setattr(card_import, "eject", fail)
    lines = []
    card_import.import_only("party", "fest", card=card, create_album=True, eject_cards=True, log=lines.append)
    assert any(line.startswith("Warning: Could not eject") for line in lines)


def test_parallel_backup_covers_all_cards(setup, monkeypatch):
    card1, tmp = setup
    card2 = tmp / "card2"
    make_photo(card2 / "DCIM" / "100MSDCF" / "_DSC0003.JPG", "2026:08:29 10:00:00")
    sizes = sum(p.stat().st_size for c in (card1, card2) for p in (c / "DCIM").rglob("*.JPG"))
    inserted = [card1]

    def candidates():
        result = list(inserted)
        if card2 not in inserted:
            inserted.append(card2)
        return result

    monkeypatch.setattr(card_import, "card_candidates", candidates)
    drive = tmp / "ssd"
    drive.mkdir()
    calls = []
    card_import.import_and_backup("party", "fest", drive=drive, create_album=True, log=lambda *_: None,
                                  backup_progress=lambda done, total, name: calls.append((done, total)))
    assert len(list((drive / config.BACKUP_DIR / "party" / "fest").iterdir())) == 3
    assert calls[-1] == (sizes, sizes)
    assert all(done <= total <= sizes for done, total in calls)


def test_backup_total_includes_photos_still_on_card(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    pc = tmp_path / "new" / "party" / "fest"
    pc.mkdir(parents=True)
    (pc / "a.ARW").write_bytes(b"x" * 10)  # on the PC, not yet backed up
    dest = tmp_path / "ssd" / config.BACKUP_DIR / "party" / "fest"
    dest.mkdir(parents=True)
    (pc / "b.ARW").write_bytes(b"x" * 20)
    (dest / "b.ARW").write_bytes(b"x" * 20)  # already backed up before this run
    calls = []
    parallel = backup.ParallelBackup("party", "fest", tmp_path / "ssd", log=lambda *_: None,
                                     progress=lambda done, total, name: calls.append((done, total)))
    parallel.expect(30)  # a photo of 30 bytes is still on the card
    parallel.start()
    assert parallel.finish() is None
    assert calls[0] == (0, 10 + 30)  # a waiting on the PC + the photo still on the card; b not counted


def test_failing_backup_does_not_stop_import(setup):
    card, tmp = setup
    drive = tmp / "ssd"
    dest = drive / config.BACKUP_DIR / "party" / "fest"
    dest.mkdir(parents=True)
    (dest / "20260828T120730-_DSC0001.JPG").write_bytes(b"different size")  # makes the backup fail
    with pytest.raises(AlbumflowError, match="backup failed"):
        card_import.import_and_backup("party", "fest", card=card, drive=drive, create_album=True,
                                      log=lambda *_: None)
    assert len(list(photos(tmp / "new" / "party" / "fest"))) == 2  # import completed


def test_each_card_gets_its_own_progress(setup, monkeypatch):
    card1, tmp = setup
    card2 = tmp / "card2"
    make_photo(card2 / "DCIM" / "100MSDCF" / "_DSC0003.JPG", "2026:08:29 10:00:00")
    monkeypatch.setattr(card_import, "card_candidates", lambda: [card1, card2])
    calls = {}
    card_import.import_only("party", "fest", create_album=True, log=lambda *_: None,
                            card_progress=lambda card: lambda done, total, name: calls.setdefault(card, []).append(done))
    assert list(calls) == [card1, card2]


def test_backup_new_removes_drive_only_when_asked(setup, monkeypatch):
    card, tmp = setup
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    drive = tmp / "ssd"
    drive.mkdir()
    removed = []
    monkeypatch.setattr(backup, "remove_device", removed.append)
    backup.backup_new("party", "fest", drive=drive, log=lambda *_: None)
    assert removed == []
    backup.backup_new("party", "fest", drive=drive, log=lambda *_: None, remove_drive=True)
    assert removed == [Path(drive.anchor)]


def test_import_removes_backup_drive_after_backup(setup, monkeypatch):
    card, tmp = setup
    drive = tmp / "ssd"
    drive.mkdir()
    removed = []
    monkeypatch.setattr(backup, "remove_device", removed.append)
    card_import.import_and_backup("party", "fest", card=card, drive=drive, create_album=True,
                                  log=lambda *_: None, remove_drive=True)
    assert len(list((drive / config.BACKUP_DIR / "party" / "fest").iterdir())) == 2
    assert removed == [Path(drive.anchor)]


def test_card_done_hook_runs_before_eject(setup, monkeypatch):
    card, tmp = setup
    events = []
    monkeypatch.setattr(card_import, "eject", lambda c: events.append("eject"))
    card_import.import_only("party", "fest", card=card, create_album=True, eject_cards=True, log=lambda *_: None,
                            on_card_done=lambda c: events.append("done"))
    assert events == ["done", "eject"]

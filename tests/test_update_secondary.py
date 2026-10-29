from pathlib import Path

import pytest

from albumflow import annex, backup, cli, clock, config
from albumflow.files import AlbumflowError


@pytest.fixture
def drives(tmp_path, monkeypatch):
    """A backup drive and a secondary drive, with plenty of space."""
    primary, secondary = tmp_path / "P", tmp_path / "S"
    for drive, marker in ((primary, config.BACKUP_MARKER), (secondary, config.SECONDARY_BACKUP_MARKER)):
        drive.mkdir()
        (drive / marker).write_text("")
    monkeypatch.setattr(backup, "drives", lambda: [primary, secondary])
    monkeypatch.setattr(backup, "free_space", lambda drive: 10**12)
    return primary, secondary


def waiting(primary, album, files):
    folder = primary / config.BACKUP_DIR / album
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(content)
    return folder


def tree(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_copies_to_the_secondary_and_moves_to_copied(drives):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one", "2.ARW": b"two"})
    backup.update_secondary(log=lambda *_: None)
    assert tree(secondary / config.SECONDARY_DIR / "party" / "fest") == {"1.ARW": b"one", "2.ARW": b"two"}
    assert tree(primary / config.COPIED_DIR / "party" / "fest") == {"1.ARW": b"one", "2.ARW": b"two"}
    assert not (primary / config.BACKUP_DIR / "party").exists()  # the empty topic folder goes too


def test_photos_backed_up_later_join_the_earlier_ones(drives):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    backup.update_secondary(log=lambda *_: None)
    waiting(primary, r"party\fest", {"2.ARW": b"two"})
    backup.update_secondary(log=lambda *_: None)
    assert tree(secondary / config.SECONDARY_DIR / "party" / "fest") == {"1.ARW": b"one", "2.ARW": b"two"}
    assert tree(primary / config.COPIED_DIR / "party" / "fest") == {"1.ARW": b"one", "2.ARW": b"two"}


def test_continues_an_interrupted_run(drives):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one", "2.ARW": b"two", "sub\\3.ARW": b"three"})
    dest = secondary / config.SECONDARY_DIR / "party" / "fest"
    dest.mkdir(parents=True)
    (dest / "1.ARW").write_bytes(b"one")  # copied before the interruption
    (dest / "2.ARW.part").write_bytes(b"t")  # interrupted copy
    albums = backup.waiting_albums(primary, secondary / config.SECONDARY_DIR)
    assert [src.name for src, _ in albums[0].todo] == ["2.ARW", "3.ARW"]
    backup.update_secondary(log=lambda *_: None)
    assert tree(dest) == {"1.ARW": b"one", "2.ARW": b"two", "sub\\3.ARW": b"three"}
    assert tree(primary / config.COPIED_DIR / "party" / "fest") == {"1.ARW": b"one", "2.ARW": b"two",
                                                                   "sub\\3.ARW": b"three"}


def test_ignores_and_removes_incomplete_copies_on_the_backup_drive(drives):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one", "2.ARW.part": b"tw"})
    backup.update_secondary(log=lambda *_: None)
    assert tree(secondary / config.SECONDARY_DIR / "party" / "fest") == {"1.ARW": b"one"}
    assert not (primary / config.BACKUP_DIR / "party").exists()


def test_refuses_a_different_file_on_the_secondary(drives):
    primary, secondary = drives
    source = waiting(primary, r"party\fest", {"1.ARW": b"one"})
    dest = secondary / config.SECONDARY_DIR / "party" / "fest"
    dest.mkdir(parents=True)
    (dest / "1.ARW").write_bytes(b"other")
    with pytest.raises(AlbumflowError, match="different size"):
        backup.update_secondary(log=lambda *_: None)
    assert tree(source) == {"1.ARW": b"one"}  # nothing moved


def test_refuses_without_enough_space(drives, monkeypatch):
    primary, secondary = drives
    source = waiting(primary, r"party\fest", {"1.ARW": b"one"})
    monkeypatch.setattr(backup, "free_space", lambda drive: 2)
    with pytest.raises(AlbumflowError, match="Not enough space"):
        backup.update_secondary(log=lambda *_: None)
    assert tree(source) == {"1.ARW": b"one"}
    assert not (secondary / config.SECONDARY_DIR).exists()


def test_dry_run_changes_nothing(drives):
    primary, secondary = drives
    source = waiting(primary, r"party\fest", {"1.ARW": b"one"})
    lines = []
    backup.update_secondary(dry_run=True, log=lines.append)
    assert tree(source) == {"1.ARW": b"one"}
    assert not (secondary / config.SECONDARY_DIR).exists()
    assert any("party\\fest: 1 files" in line for line in lines)


def test_needs_the_primary_drive(drives, monkeypatch):
    _, secondary = drives
    monkeypatch.setattr(backup, "drives", lambda: [secondary])
    with pytest.raises(AlbumflowError, match="primary drive"):
        backup.update_secondary(log=lambda *_: None)


def test_cli(drives, capsys):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    assert cli.main(["update-secondary", "--no-eject"]) == 0
    assert (secondary / config.SECONDARY_DIR / "party" / "fest" / "1.ARW").is_file()


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    folder = tmp_path / "G" / "My Drive" / "Backup" / "pictures-new"
    monkeypatch.setattr(config, "CLOUD_PICTURES", folder)
    monkeypatch.setattr(config, "HOME_FOLDER", tmp_path / "home")  # its Rejected tree
    return folder


def test_update_cloud_copies_the_waiting_albums_and_leaves_them_waiting(drives, cloud):
    primary, secondary = drives
    source = waiting(primary, r"party\fest", {"1.ARW": b"one", "1.xmp": b"x"})
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud / "party" / "fest") == {"1.ARW": b"one", "1.xmp": b"x"}  # the complete import
    assert tree(source) == {"1.ARW": b"one", "1.xmp": b"x"}  # still waiting for the secondary
    waiting(primary, r"party\fest", {"2.ARW": b"two"})
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud / "party" / "fest") == {"1.ARW": b"one", "1.xmp": b"x", "2.ARW": b"two"}


def test_update_cloud_refuses_without_enough_space(drives, cloud, monkeypatch):
    primary, _ = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    monkeypatch.setattr(backup, "free_space", lambda drive: 2)
    with pytest.raises(AlbumflowError, match="Not enough space"):
        backup.update_cloud(log=lambda *_: None)
    assert not cloud.exists()


def test_update_cloud_cli_dry_run(drives, cloud):
    primary, _ = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    assert cli.main(["update-cloud", "--dry-run"]) == 0
    assert not cloud.exists()


def pending_album(new, name, files):
    """An album on the PC whose photos were imported without the backup."""
    folder = new / name
    folder.mkdir(parents=True)
    for file, content in files.items():
        (folder / file).write_bytes(content)
    state = clock.AlbumState(folder)
    state.backup_pending = True
    state.save()
    return folder


@pytest.fixture
def new(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    monkeypatch.setattr(config, "PICTURES_NEW_OLD", tmp_path / "new-old")
    monkeypatch.setattr(config, "HOME_FOLDER", tmp_path / "home")
    return tmp_path / "new"


def test_update_cloud_without_the_backup_drive_copies_pending_albums_from_the_pc(drives, cloud, new,
                                                                                 monkeypatch):
    monkeypatch.setattr(backup, "drives", lambda: [])
    pending_album(new, r"trip\day1", {"20261101T100000-1.ARW": b"one",
                                      "20261101T110000-20261101T100500-2.ARW": b"corrected"})
    (new / "trip" / "done").mkdir()
    (new / "trip" / "done" / "x.ARW").write_bytes(b"not pending")
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud) == {r"trip\day1\20261101T100000-1.ARW": b"one"}  # corrected ones are on the drive


def test_update_cloud_skips_pending_photos_already_waiting_on_the_drive(drives, cloud, new):
    primary, _ = drives
    waiting(primary, r"trip\day1", {"1.ARW": b"one"})
    pending_album(new, r"trip\day1", {"1.ARW": b"one", "2.ARW": b"two"})
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud / "trip" / "day1") == {"1.ARW": b"one", "2.ARW": b"two"}


def test_backup_new_clears_the_pending_mark(drives, new):
    folder = pending_album(new, r"trip\day1", {"1.ARW": b"one"})
    assert backup.pending_albums() == [("trip", "day1")]
    backup.backup_new("trip", "day1", log=lambda *_: None)
    assert backup.pending_albums() == []
    assert not clock.AlbumState(folder).backup_pending


def test_backup_status(drives, cloud, new, capsys, monkeypatch):
    primary, _ = drives
    pending_album(new, r"trip\day1", {"1.ARW": b"one", "2.ARW": b"two"})
    waiting(primary, r"trip\day1", {"1.ARW": b"one"})
    assert cli.main(["backup-status"]) == 0
    out = capsys.readouterr().out
    assert "trip/day1: 1 photos" in out  # 2.ARW is not on the primary drive
    assert "trip\\day1: 1 files" in out  # 1.ARW waits on the drive, not in the cloud
    assert "trip\\day1 (pending a backup, from the PC): 1 files" in out  # 2.ARW
    monkeypatch.setattr(backup, "drives", lambda: [])
    assert cli.main(["backup-status"]) == 0
    out = capsys.readouterr().out
    assert "primary drive not connected" in out and "  trip/day1" in out


def test_import_offers_to_go_on_without_the_backup_drive(new, monkeypatch, capsys):
    monkeypatch.setattr(backup, "drives", lambda: [])
    calls = []
    monkeypatch.setattr(cli, "cmd_import_only", lambda args: calls.append(args))
    args = cli.build_parser().parse_args(["import", "trip/day1"])
    args.topic, args.album = "trip", "day1"
    assert cli.cmd_import(args, ask=lambda prompt, keys: "c") == 0
    assert calls == [args]
    assert "run albumflow backup-new \"trip/day1\"" in capsys.readouterr().out
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_import(args, ask=lambda prompt, keys: "q")


@pytest.mark.parametrize("options, eject_cards, remove_drive", [
    ([], True, True), (["--no-eject-drive"], True, False), (["--no-eject"], False, False),
])
def test_import_ejects_cards_and_the_primary_drive_unless_told_not_to(drives, monkeypatch, options, eject_cards,
                                                                    remove_drive):
    calls = []
    monkeypatch.setattr(cli.card_import, "import_and_backup", lambda *args, **kwargs: calls.append(kwargs))
    args = cli.build_parser().parse_args(["import", "trip/day1", *options])
    args.topic, args.album = "trip", "day1"
    assert cli.cmd_import(args) == 0
    assert (calls[0]["eject_cards"], calls[0]["remove_drive"]) == (eject_cards, remove_drive)


def test_update_cloud_skips_albums_safe_elsewhere(drives, cloud, monkeypatch, capsys):
    primary, _ = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    waiting(primary, r"party\other", {"1.ARW": b"one"})
    monkeypatch.setattr(config, "SAFE_ELSEWHERE", {r"party\fest": "SD card at home"})
    lines = []
    backup.update_cloud(log=lines.append)
    assert tree(cloud) == {r"party\other\1.ARW": b"one"}
    assert r"  party\fest: skipped, safe elsewhere (SD card at home)" in lines
    waiting(primary, r"party\fest", {"2.ARW": b"two"})
    assert cli.main(["backup-status"]) == 0
    out = capsys.readouterr().out
    assert "but safe elsewhere" in out and "  party\\fest: 2 files, 0.0 GB (SD card at home)" in out


def test_backup_new_all(drives, new, capsys):
    primary, _ = drives
    for name in (r"trip\day1", r"trip\day2", r"OnlyForOthers\day0"):
        (new / name).mkdir(parents=True)
        (new / name / "1.ARW").write_bytes(b"one")
    waiting(primary, r"trip\day2", {"1.ARW": b"one"})  # already there
    assert cli.main(["backup-new", "--all", "--no-eject"]) == 0
    assert (primary / config.BACKUP_DIR / "trip" / "day1" / "1.ARW").is_file()
    assert not (primary / config.BACKUP_DIR / "OnlyForOthers").exists()
    assert "1 albums with originals not yet on the primary drive: trip/day1" in capsys.readouterr().err
    assert cli.main(["backup-new", "--all", "trip/day1"]) == 2


def test_update_secondary_deletes_from_the_cloud_what_is_on_the_secondary(drives, cloud):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"20261101T100000-_DSC1.ARW": b"one", "2.ARW": b"two"})
    backup.update_cloud(log=lambda *_: None)
    # an old copy from the PC, clock-corrected
    (cloud / "party" / "fest" / "20261101T100500-20261101T100000-_DSC1.ARW").write_bytes(b"corrected")
    (cloud / "trip" / "open").mkdir(parents=True)
    (cloud / "trip" / "open" / "3.ARW").write_bytes(b"three")  # not on the secondary
    (cloud / "party" / "fest" / "2.ARW").replace(cloud / "1.tmp")
    (backup.cloud_rejected() / "party" / "fest").mkdir(parents=True)
    (cloud / "1.tmp").replace(backup.cloud_rejected() / "party" / "fest" / "2.ARW")  # rejected
    lines = []
    backup.update_secondary(log=lines.append)
    assert not (cloud / "party").exists() and tree(backup.cloud_rejected()) == {}
    assert tree(cloud) == {r"trip\open\3.ARW": b"three"}
    assert r"Deleting from the cloud, on the secondary drive now: pictures-new\party\fest, 2 files, 0.0 GB" in lines
    assert r"Deleting from the cloud, on the secondary drive now: pictures-rejected\party\fest, 1 files, 0.0 GB" \
        in lines


def test_update_secondary_keeps_a_cloud_file_of_another_size(drives, cloud):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    (cloud / "party" / "fest").mkdir(parents=True)
    (cloud / "party" / "fest" / "1.ARW").write_bytes(b"different")
    backup.update_secondary(log=lambda *_: None)
    assert tree(cloud) == {r"party\fest\1.ARW": b"different"}


def culled(new, name, kept, rejected=()):
    """The album on the PC: the kept photos in the new tree, the rejected ones in the Rejected tree."""
    folder = new / name
    folder.mkdir(parents=True, exist_ok=True)
    for file in kept:
        (folder / file).write_bytes(b"k")
    there = annex.home() / config.REJECTED_TREE / name
    there.mkdir(parents=True, exist_ok=True)
    for file in rejected:
        (there / file).write_bytes(b"r")
    return folder


def space(monkeypatch, free):
    monkeypatch.setattr(config, "CLOUD_RESERVE", 10)
    monkeypatch.setattr(backup, "free_space", lambda drive: free + 10)


def test_update_cloud_with_enough_space_copies_rejected_photos_to_their_tree(drives, cloud, new, monkeypatch):
    primary, _ = drives
    waiting(primary, r"trip\day1", {"1.ARW": b"1111", "2.ARW": b"2222"})
    culled(new, r"trip\day1", ["1.ARW"], ["2.ARW"])
    space(monkeypatch, 8)
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud) == {r"trip\day1\1.ARW": b"1111"}
    assert tree(backup.cloud_rejected()) == {r"trip\day1\2.ARW": b"2222"}
    backup.update_cloud(log=lambda *_: pytest.fail("copied again") if "Copied" in _[0] else None)


def test_update_cloud_moves_photos_culled_since_and_back(drives, cloud, new, monkeypatch):
    primary, _ = drives
    waiting(primary, r"trip\day1", {"1.ARW": b"1111", "2.ARW": b"2222"})
    album = culled(new, r"trip\day1", ["1.ARW", "2.ARW"])
    (cloud / "trip" / "old").mkdir(parents=True)
    (cloud / "trip" / "old" / "x.ARW").write_bytes(b"xx")  # album no longer on the PC: kept
    backup.update_cloud(log=lambda *_: None)
    (album / "2.ARW").unlink()
    culled(new, r"trip\day1", [], ["2.ARW"])
    lines = []
    backup.update_cloud(log=lines.append)
    assert r"Moving to pictures-rejected\trip\day1: 1 files" in lines
    assert tree(cloud) == {r"trip\day1\1.ARW": b"1111", r"trip\old\x.ARW": b"xx"}
    assert tree(backup.cloud_rejected()) == {r"trip\day1\2.ARW": b"2222"}
    (annex.home() / config.REJECTED_TREE / "trip" / "day1" / "2.ARW").unlink()  # taken back
    culled(new, r"trip\day1", ["2.ARW"])
    backup.update_cloud(log=lambda *_: None)
    assert tree(cloud) == {r"trip\day1\1.ARW": b"1111", r"trip\day1\2.ARW": b"2222", r"trip\old\x.ARW": b"xx"}
    assert tree(backup.cloud_rejected()) == {}


def test_update_cloud_short_on_space_deletes_only_as_many_rejected_as_needed(drives, cloud, new, monkeypatch):
    primary, _ = drives
    waiting(primary, r"trip\day1", {"1.ARW": b"1111", "2.ARW": b"2222", "3.ARW": b"3333"})
    culled(new, r"trip\day1", ["1.ARW", "2.ARW", "3.ARW"])
    backup.update_cloud(log=lambda *_: None)
    (new / "trip" / "day1" / "1.ARW").unlink()
    (new / "trip" / "day1" / "2.ARW").unlink()
    culled(new, r"trip\day1", [], ["1.ARW", "2.ARW"])  # rejected since copied, not moved in the cloud yet
    waiting(primary, r"trip\day2", {"4.ARW": b"4444", "5.ARW": b"5555"})
    culled(new, r"trip\day2", ["20261102T100500-4.ARW"], ["5.ARW"])  # 4.ARW kept, and clock-corrected on the PC
    space(monkeypatch, 2)  # 4.ARW needs 2 more: 1.ARW goes
    lines = []
    backup.update_cloud(log=lines.append)
    assert r"Deleting from the cloud, rejected, to make room: pictures-new\trip\day1, 1 files, 0.0 GB" in lines
    assert tree(cloud) == {r"trip\day1\3.ARW": b"3333", r"trip\day2\4.ARW": b"4444"}
    assert tree(backup.cloud_rejected()) == {r"trip\day1\2.ARW": b"2222"}
    assert "1 rejected photos are not copied to the cloud: not enough room." in lines


def test_update_cloud_never_deletes_kept_photos(drives, cloud, new, monkeypatch):
    primary, _ = drives
    waiting(primary, r"trip\day1", {"1.ARW": b"1111", "2.ARW": b"2222"})
    culled(new, r"trip\day1", ["1.ARW", "2.ARW"])
    (cloud / "trip" / "day0").mkdir(parents=True)
    (cloud / "trip" / "day0" / "0.ARW").write_bytes(b"0")
    culled(new, r"trip\day0", [], ["0.ARW"])
    space(monkeypatch, 6)
    with pytest.raises(AlbumflowError, match="the kept photos need"):
        backup.update_cloud(log=lambda *_: None)
    assert tree(cloud) == {r"trip\day0\0.ARW": b"0"}


def test_update_cloud_dry_run_moves_and_deletes_nothing(drives, cloud, new, monkeypatch):
    primary, _ = drives
    (cloud / "trip" / "day0").mkdir(parents=True)
    (cloud / "trip" / "day0" / "0.ARW").write_bytes(b"0000")
    (cloud / "trip" / "day0" / "9.ARW").write_bytes(b"9999")
    culled(new, r"trip\day0", [], ["0.ARW", "9.ARW"])
    waiting(primary, r"trip\day1", {"1.ARW": b"1111"})
    space(monkeypatch, 0)
    lines = []
    backup.update_cloud(dry_run=True, log=lines.append)
    assert r"Would delete from the cloud, rejected, to make room: pictures-new\trip\day0, 1 files, 0.0 GB" in lines
    assert r"Would move to pictures-rejected\trip\day0: 1 files" in lines
    assert tree(cloud) == {r"trip\day0\0.ARW": b"0000", r"trip\day0\9.ARW": b"9999"}


def test_rejected_photos_on_c_and_in_the_annex_not_taken_back(new, tmp_path):
    culled(new, r"trip\day1", ["3.ARW"], ["1.ARW", "3.ARW"])  # 3.ARW: taken back to new
    target = tmp_path / "annex" / "day1"
    target.mkdir(parents=True)
    (target / "2.ARW").write_bytes(b"r")
    annex.create_link(annex.link_of(annex.home() / config.REJECTED_TREE / "trip" / "day1"), target)
    assert backup.rejected_photos(Path(r"trip\day1")) == {"1.ARW", "2.ARW"}
    assert backup.rejected_photos(Path(r"trip\none")) == set()


def test_kept_names_also_in_new_old_and_the_annex(new, tmp_path):
    culled(config.PICTURES_NEW_OLD, r"trip\old", ["1.ARW"])
    target = tmp_path / "annex" / "day1"
    target.mkdir(parents=True)
    (target / "2.ARW").write_bytes(b"k")
    (new / "trip").mkdir(parents=True)
    annex.create_link(annex.link_of(new / "trip" / "day1"), target)
    assert backup.kept_names(Path(r"trip\old")) == {"1.ARW"}
    assert backup.kept_names(Path(r"trip\day1")) == {"2.ARW"}
    assert backup.kept_names(Path(r"trip\renamed")) is None


def test_update_secondary_at_home_tidies_the_cloud_as_of_the_index(drives, cloud, monkeypatch):
    primary, secondary = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one", "2.ARW": b"two"})
    backup.update_cloud(log=lambda *_: None)  # on holiday: both photos to the cloud
    waiting(primary, r"party\later", {"3.ARW": b"three"})
    backup.update_cloud(log=lambda *_: None)
    monkeypatch.setattr(config, "CLOUD_PICTURES", cloud.parent / "not here")  # in the office: no cloud
    lines = []
    backup.update_secondary(log=lines.append)  # copies party\fest and party\later, scans into the index
    assert any("The cloud isn't here" in line for line in lines)
    monkeypatch.setattr(config, "CLOUD_PICTURES", cloud)  # back home, without the secondary drive
    monkeypatch.setattr(backup, "drives", lambda: [primary])
    (cloud / "trip" / "open").mkdir(parents=True)
    (cloud / "trip" / "open" / "9.ARW").write_bytes(b"nine")  # not on the secondary drive
    lines = []
    backup.update_secondary(log=lines.append)
    assert "The secondary drive is not connected: only the cloud is tidied up." in lines
    assert tree(cloud) == {r"trip\open\9.ARW": b"nine"}


def test_update_secondary_without_the_secondary_drive_nor_the_cloud(drives, monkeypatch):
    primary, _ = drives
    monkeypatch.setattr(backup, "drives", lambda: [primary])
    lines = []
    backup.update_secondary(log=lines.append)
    assert lines[0].startswith("Nothing to update: neither is the secondary drive")

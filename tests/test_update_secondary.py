import pytest

from albumflow import backup, cli, clock, config
from albumflow.files import AlbumflowError


@pytest.fixture
def drives(tmp_path, monkeypatch):
    """A backup drive and a secondary backup drive, with plenty of space."""
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


def test_needs_both_drives(drives, monkeypatch):
    primary, _ = drives
    monkeypatch.setattr(backup, "drives", lambda: [primary])
    with pytest.raises(AlbumflowError, match="secondary backup drive"):
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


def test_backup_raw_clears_the_pending_mark(drives, new):
    folder = pending_album(new, r"trip\day1", {"1.ARW": b"one"})
    assert backup.pending_albums() == [("trip", "day1")]
    backup.backup_raw("trip", "day1", log=lambda *_: None)
    assert backup.pending_albums() == []
    assert not clock.AlbumState(folder).backup_pending


def test_backup_status(drives, cloud, new, capsys, monkeypatch):
    primary, _ = drives
    pending_album(new, r"trip\day1", {"1.ARW": b"one", "2.ARW": b"two"})
    waiting(primary, r"trip\day1", {"1.ARW": b"one"})
    assert cli.main(["backup-status"]) == 0
    out = capsys.readouterr().out
    assert "trip/day1: 1 photos" in out  # 2.ARW is not on the backup drive
    assert "trip\\day1: 1 files" in out  # 1.ARW waits on the drive, not in the cloud
    assert "trip\\day1 (pending a backup, from the PC): 1 files" in out  # 2.ARW
    monkeypatch.setattr(backup, "drives", lambda: [])
    assert cli.main(["backup-status"]) == 0
    out = capsys.readouterr().out
    assert "backup drive not connected" in out and "  trip/day1" in out


def test_import_offers_to_go_on_without_the_backup_drive(new, monkeypatch, capsys):
    monkeypatch.setattr(backup, "drives", lambda: [])
    calls = []
    monkeypatch.setattr(cli, "cmd_import_only", lambda args: calls.append(args))
    args = cli.build_parser().parse_args(["import", "trip/day1"])
    args.topic, args.album = "trip", "day1"
    assert cli.cmd_import(args, ask=lambda prompt, keys: "c") == 0
    assert calls == [args]
    assert "run albumflow backup-raw \"trip/day1\"" in capsys.readouterr().out
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_import(args, ask=lambda prompt, keys: "q")


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
    assert r"party\fest: 2 files, 0.0 GB (safe elsewhere: SD card at home)" in capsys.readouterr().out

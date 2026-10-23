import pytest

from albumflow import backup, cli, config
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


def test_update_cloud_needs_the_backup_drive(drives, cloud, monkeypatch):
    monkeypatch.setattr(backup, "drives", lambda: [])
    with pytest.raises(AlbumflowError, match="backup drive"):
        backup.update_cloud(log=lambda *_: None)


def test_update_cloud_cli_dry_run(drives, cloud):
    primary, _ = drives
    waiting(primary, r"party\fest", {"1.ARW": b"one"})
    assert cli.main(["update-cloud", "--dry-run"]) == 0
    assert not cloud.exists()

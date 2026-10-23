import pytest

from albumflow import backup, card_import, config
from albumflow.files import AlbumflowError


def test_backup_dir_mirrors_topic_and_album(tmp_path):
    assert backup.backup_dir(tmp_path, "party", "fest") == tmp_path / "Backup" / "pictures-new" / "to-secondary" / "party" / "fest"


def test_album_backup_dirs_are_waiting_and_copied(tmp_path):
    assert backup.album_backup_dirs(tmp_path, "party", "fest") == [
        tmp_path / config.BACKUP_DIR / "party" / "fest", tmp_path / config.COPIED_DIR / "party" / "fest"]


def test_missing_on_backup_counts_what_was_copied_to_the_secondary(tmp_path):
    album, waiting, copied = tmp_path / "album", tmp_path / "waiting", tmp_path / "copied"
    for folder in (album, waiting, copied):
        folder.mkdir()
    for name in ("1.ARW", "2.ARW", "3.ARW"):
        (album / name).write_bytes(b"x")
    (copied / "1.ARW").write_bytes(b"x")  # moved on by update-secondary
    (waiting / "2.ARW").write_bytes(b"x")
    assert backup.missing_on_backup(album, waiting, [copied]) == [(album / "3.ARW", waiting / "3.ARW")]
    (copied / "3.ARW").write_bytes(b"xx")
    with pytest.raises(AlbumflowError, match="different size"):
        backup.missing_on_backup(album, waiting, [copied])


def test_backup_dirs_named_finds_album_in_any_topic_and_state(tmp_path):
    for folder in [config.BACKUP_DIR + r"\party\fest", config.COPIED_DIR + r"\trips\fest",
                   config.COPIED_DIR + r"\party\other"]:
        (tmp_path / folder).mkdir(parents=True)
    assert backup.backup_dirs_named(tmp_path, "fest") == [tmp_path / config.BACKUP_DIR / "party" / "fest",
                                                          tmp_path / config.COPIED_DIR / "trips" / "fest"]
    assert backup.backup_dirs_named(tmp_path, "missing") == []


def test_missing_on_counts_files_in_none_of_the_folders(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "1.ARW").write_bytes(b"x")
    (b / "2.ARW").write_bytes(b"yy")
    (b / "3.ARW").write_bytes(b"short")
    old = card_import.OldBackup(tmp_path / "card", 0.0, 14, {"1.ARW": 1, "2.ARW": 2, "3.ARW": 10, "4.ARW": 1})
    assert card_import.missing_on(old, [a, b]) == 2  # 3.ARW has another size, 4.ARW is nowhere
    assert card_import.missing_on(old, []) == 4

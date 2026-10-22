from albumflow import backup, card_import, config


def test_backup_dir_mirrors_topic_and_album(tmp_path):
    assert backup.backup_dir(tmp_path, "party", "fest") == tmp_path / "Backup" / "pictures-new" / "to-secondary" / "party" / "fest"


def test_album_backup_dir_prefers_to_secondary(tmp_path):
    waiting = tmp_path / config.BACKUP_DIR / "party" / "fest"
    copied = tmp_path / config.COPIED_DIR / "party" / "fest"
    assert backup.album_backup_dir(tmp_path, "party", "fest") == waiting  # neither exists yet
    copied.mkdir(parents=True)
    assert backup.album_backup_dir(tmp_path, "party", "fest") == copied
    waiting.mkdir(parents=True)  # more photos backed up after the copy to the secondary
    assert backup.album_backup_dir(tmp_path, "party", "fest") == waiting


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

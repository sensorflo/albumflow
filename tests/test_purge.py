import os

import pytest

from albumflow import annex, backup, cli, config
from albumflow.files import AlbumflowError


@pytest.fixture
def drives(tmp_path, monkeypatch):
    """A backup drive and a secondary drive, and the laptop's Originals."""
    primary, secondary = tmp_path / "P", tmp_path / "S"
    for drive, marker in ((primary, config.BACKUP_MARKER), (secondary, config.SECONDARY_BACKUP_MARKER)):
        drive.mkdir()
        (drive / marker).write_text("")
    monkeypatch.setattr(backup, "drives", lambda: [primary, secondary])
    monkeypatch.setattr(config, "ORIGINALS", tmp_path / "Originals")
    return primary, secondary


def album(root, name, size=1, age_days=0):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "1.ARW").write_bytes(b"x" * size)
    mtime = 1_800_000_000 - age_days * 86400
    os.utime(folder / "1.ARW", (mtime, mtime))
    return folder


def finish(name):
    (config.ORIGINALS / name).mkdir(parents=True)


def answers(*replies):
    replies = list(replies)
    return lambda prompt: replies.pop(0)


def test_only_finished_albums_are_purgeable(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    album(root, r"party\open")
    finish(r"party\done")
    assert [p.name for p in backup.purgeable(root)] == [r"party\done"]


def test_an_album_in_the_annex_counts_as_finished(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    target = config.ORIGINALS.parent / "annexed"
    target.mkdir(parents=True)
    config.ORIGINALS.joinpath("party").mkdir(parents=True)
    annex.create_link(annex.link_of(config.ORIGINALS / "party" / "done"), target)
    assert [p.name for p in backup.purgeable(root)] == [r"party\done"]


def test_large_and_old_first(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    for name, size, age in ((r"a\small-old", 1000, 400), (r"a\big-new", 10_000, 1), (r"a\big-old", 10_000, 400)):
        album(root, name, size, age)
        finish(name)
    names = [p.name for p in backup.purgeable(root, now=1_800_000_000)]
    assert names == [r"a\big-old", r"a\small-old", r"a\big-new"]


def test_on_the_backup_drive_only_what_is_on_the_secondary_too(drives):
    primary, secondary = drives
    album(primary / config.BACKUP_DIR, r"party\waiting")
    album(primary / config.COPIED_DIR, r"party\copied")
    finish(r"party\waiting")
    finish(r"party\copied")
    roots = dict((root, d) for d, root in backup.purge_roots())
    assert set(roots) == {primary / config.COPIED_DIR, secondary / config.SECONDARY_DIR}
    assert [p.name for p in backup.purgeable(primary / config.COPIED_DIR)] == [r"party\copied"]


def test_purge_deletes_after_typing_delete(drives, capsys):
    primary, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    album(root, r"party\done too")
    finish(r"party\done")
    finish(r"party\done too")
    assert cli.cmd_purge_backup(None, ask=answers("2", "delete")) == 0  # same score: by name
    assert [p.name for p in (root / "party").iterdir()] == ["done"]
    assert "deleted party\\done too" in capsys.readouterr().out


def test_purge_needs_the_word_delete(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    finish(r"party\done")
    for confirmation in ("y", "", "yes", "Delete"):
        assert cli.cmd_purge_backup(None, ask=answers("1", confirmation)) == 0
        assert (root / "party" / "done").is_dir()


def test_purge_enter_chooses_none(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    finish(r"party\done")
    assert cli.cmd_purge_backup(None, ask=answers("")) == 0  # no second question
    assert (root / "party" / "done").is_dir()


def test_purging_the_last_album_of_a_topic_removes_the_topic_folder(drives):
    _, secondary = drives
    root = secondary / config.SECONDARY_DIR
    album(root, r"party\done")
    finish(r"party\done")
    cli.cmd_purge_backup(None, ask=answers("1", "delete"))
    assert not (root / "party").exists()
    assert root.is_dir()


def test_purge_needs_a_drive(drives, monkeypatch):
    monkeypatch.setattr(backup, "drives", lambda: [])
    with pytest.raises(AlbumflowError, match="Neither the primary nor the secondary drive"):
        cli.cmd_purge_backup(None, ask=answers())

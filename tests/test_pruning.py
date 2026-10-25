import os
from collections import namedtuple

import pytest

from albumflow import backup, card_import, cli, config

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def card(tmp_path, monkeypatch):
    card = tmp_path / "card"
    for i, name in enumerate(["old-fest", "older-fest", "current"]):
        folder = card / "Backup" / name
        folder.mkdir(parents=True)
        (folder / f"{name}.ARW").write_bytes(b"x" * (100 * (i + 1)))
    os.utime(card / "Backup" / "older-fest", (1_600_000_000,) * 2)
    os.utime(card / "Backup" / "old-fest", (1_700_000_000,) * 2)
    monkeypatch.setattr(backup, "drives", lambda: [])
    return card


def full(monkeypatch, free_fraction):
    monkeypatch.setattr(card_import.shutil, "disk_usage", lambda _: Usage(1000, 0, int(1000 * free_fraction)))


@pytest.mark.parametrize("answer, expected", [
    ("", []), ("1 3", [1, 3]), ("1,3", [1, 3]), ("2-3", [2, 3]), ("1-2 2", [1, 2]),
    ("4", None), ("x", None), ("3-1", None),
])
def test_parse_selection(answer, expected):
    assert cli.parse_selection(answer, 3) == expected


def test_no_offer_when_enough_free(card, monkeypatch):
    full(monkeypatch, 0.6)
    cli.offer_pruning(card, "current", log=lambda *_: None, ask=lambda _: pytest.fail("asked"))


def test_unsafe_folders_are_listed_but_only_deleted_with_force(card, monkeypatch):
    full(monkeypatch, 0.2)
    lines = []
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "1")
    listed = [line for line in lines if line.startswith("  ")]
    assert "older-fest" in listed[0] and "old-fest" in listed[1] and len(listed) == 2  # not "current"
    assert "not yet in a secondary backup; primary drive not connected" in listed[0]
    assert (card / "Backup" / "older-fest").exists()
    assert any("not yet safe (--force-prune" in line for line in lines)
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "1", force=True)
    assert not (card / "Backup" / "older-fest").exists()
    assert (card / "Backup" / "old-fest").exists() and (card / "Backup" / "current").exists()


def test_on_the_backup_drive_alone_is_not_safe(card, monkeypatch, tmp_path):
    full(monkeypatch, 0.2)
    ssd = tmp_path / "ssd"
    (ssd / config.BACKUP_MARKER).parent.mkdir(parents=True)
    (ssd / config.BACKUP_MARKER).write_text("")
    waiting = ssd / config.BACKUP_DIR / "party" / "old-fest"
    waiting.mkdir(parents=True)
    (waiting / "old-fest.ARW").write_bytes(b"x" * 100)
    monkeypatch.setattr(backup, "drives", lambda: [ssd])
    lines = []
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "1 2")
    listed = [line for line in lines if line.startswith("  ")]
    assert "1 of 1 files not yet in a secondary backup, 1 not even on the primary drive" in listed[0]
    assert listed[1].endswith("(1 of 1 files not yet in a secondary backup)")  # old-fest
    assert (card / "Backup" / "older-fest").exists() and (card / "Backup" / "old-fest").exists()


@pytest.mark.parametrize("where", ["copied", "secondary", "cloud"])
def test_safe_once_on_the_secondary_or_in_the_cloud(card, monkeypatch, tmp_path, where):
    full(monkeypatch, 0.2)
    ssd, secondary, cloud = tmp_path / "ssd", tmp_path / "secondary", tmp_path / "cloud"
    for drive, marker in ((ssd, config.BACKUP_MARKER), (secondary, config.SECONDARY_BACKUP_MARKER)):
        drive.mkdir()
        (drive / marker).write_text("")
    monkeypatch.setattr(backup, "drives", lambda: [ssd, secondary])
    monkeypatch.setattr(config, "CLOUD_PICTURES", cloud)
    folder = {"copied": ssd / config.COPIED_DIR, "secondary": secondary / config.SECONDARY_DIR,
              "cloud": cloud}[where] / "party" / "old-fest"
    folder.mkdir(parents=True)
    (folder / "old-fest.ARW").write_bytes(b"x" * 100)
    lines = []
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "2")
    listed = [line for line in lines if line.startswith("  ")]
    assert listed[1].endswith("(safe: in a secondary backup)")
    assert not (card / "Backup" / "old-fest").exists()

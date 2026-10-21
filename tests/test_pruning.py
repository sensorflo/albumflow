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


def test_offers_other_folders_oldest_first_and_deletes(card, monkeypatch):
    full(monkeypatch, 0.2)
    lines = []
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "1")
    listed = [line for line in lines if line.startswith("  ")]
    assert "older-fest" in listed[0] and "old-fest" in listed[1] and len(listed) == 2  # not "current"
    assert "backup drive not connected" in listed[0]
    assert not (card / "Backup" / "older-fest").exists()
    assert (card / "Backup" / "old-fest").exists() and (card / "Backup" / "current").exists()


def test_shows_what_is_missing_on_backup_drive(card, monkeypatch, tmp_path):
    full(monkeypatch, 0.2)
    ssd = tmp_path / "ssd"
    (ssd / config.BACKUP_DIR / "old-fest").mkdir(parents=True)
    (ssd / config.BACKUP_DIR / "old-fest" / "old-fest.ARW").write_bytes(b"x" * 100)
    monkeypatch.setattr(backup, "find_backup_drive", lambda: ssd)
    lines = []
    cli.offer_pruning(card, "current", log=lines.append, ask=lambda _: "")
    listed = [line for line in lines if line.startswith("  ")]
    assert "1 of 1 files NOT on backup drive" in listed[0]  # older-fest
    assert "all on backup drive" in listed[1]  # old-fest
    assert (card / "Backup" / "older-fest").exists()  # Enter: nothing deleted

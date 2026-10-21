import os
import time

import pytest

from albumflow import annex, cli, config
from albumflow.cli import ProgressDisplay

DAY = 86400


def make(folder, size, age_days, now):
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "a.ARW"
    f.write_bytes(b"x" * size)
    os.utime(f, (now - age_days * DAY,) * 2)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    drive = tmp_path / "annex"
    drive.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setattr(config, "PICTURES_NEW", home / "new")
    monkeypatch.setattr(config, "ANNEX_SUGGEST_ROOTS", ["new", "Rejected"])
    monkeypatch.setattr(config, "LIGHTROOM_CATALOGS", [])
    monkeypatch.setenv("ALBUMFLOW_ANNEX", str(drive))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    return home, drive, time.time()


def test_candidates_are_folders_with_files(setup):
    home, drive, now = setup
    make(home / "new" / "party" / "fest", 10, 1, now)
    make(home / "new" / "party" / "fest" / "sub", 10, 1, now)  # part of fest, not its own candidate
    (home / "new" / "party" / "desktop.ini").write_text("x")  # doesn't make "party" a candidate
    make(home / "new" / "party" / "old (lightroom)", 10, 1, now)  # excluded by pattern
    make(home / "new" / "party" / "half.part", 10, 1, now)  # an interrupted move
    make(home / "Rejected", 10, 1, now)  # the root itself holds the files
    found = sorted(p.relative_to(home).as_posix() for p in annex.candidate_folders(home / "new")
                   + annex.candidate_folders(home / "Rejected"))
    assert found == ["Rejected", "new/party/fest"]


def test_size_counts_subfolders_and_newest_file_counts_for_age(setup):
    home, drive, now = setup
    make(home / "new" / "party" / "fest", 100, 400, now)
    make(home / "new" / "party" / "fest" / "sub", 50, 2, now)  # recently worked on
    [c] = annex.suggestions([home / "new"], now=now)
    assert c.size == 150
    assert abs((now - c.newest) / DAY - 2) < 0.01


def test_old_and_large_first(setup):
    home, drive, now = setup
    make(home / "new" / "t" / "big-recent", 6_000, 30, now)
    make(home / "new" / "t" / "big-old", 4_000, 730, now)
    make(home / "new" / "t" / "small-old", 500, 730, now)
    order = [c.folder.name for c in annex.suggestions([home / "new"], now=now)]
    assert order == ["big-old", "big-recent", "small-old"]


def test_annexed_folders_are_not_suggested(setup):
    home, drive, now = setup
    make(home / "new" / "t" / "a", 10, 100, now)
    annex.to_annex(home / "new" / "t" / "a", log=lambda *_: None)
    assert annex.suggestions([home / "new"], now=now) == []


def test_choose_and_move(setup, capsys):
    home, drive, now = setup
    make(home / "new" / "t" / "one", 300, 700, now)
    make(home / "new" / "t" / "two", 200, 700, now)
    make(home / "new" / "t" / "three", 100, 700, now)
    assert cli.suggest_for_annex(ProgressDisplay(), dry_run=False, ask=lambda _: "1 3") == 0
    assert annex.is_annex_link(home / "new" / "t" / "one (annex)")
    assert (home / "new" / "t" / "two").is_dir()
    assert annex.is_annex_link(home / "new" / "t" / "three (annex)")
    out = capsys.readouterr().out
    assert "one" in out and "new\\t" in out and "Moving all 3 would free" in out


def test_enter_moves_nothing(setup):
    home, drive, now = setup
    make(home / "new" / "t" / "one", 300, 700, now)
    assert cli.suggest_for_annex(ProgressDisplay(), dry_run=False, ask=lambda _: "") == 0
    assert (home / "new" / "t" / "one").is_dir()


def test_a_refused_folder_is_skipped_others_moved(setup, monkeypatch):
    home, drive, now = setup
    make(home / "new" / "t" / "one", 300, 700, now)
    make(home / "new" / "t" / "two", 200, 700, now)
    (annex.require_annex() / "new" / "t" / "one").mkdir(parents=True)  # makes "one" refuse
    assert cli.suggest_for_annex(ProgressDisplay(), dry_run=False, ask=lambda _: "1-2") == 2
    assert (home / "new" / "t" / "one").is_dir()
    assert annex.is_annex_link(home / "new" / "t" / "two (annex)")


@pytest.mark.parametrize("days, expected", [
    (0, "today"), (1, "yesterday"), (5, "5 days"), (13, "13 days"), (14, "2 weeks"),
    (59, "8 weeks"), (60, "2 months"), (400, "13 months"), (730, "2 years"), (1200, "3 years"),
])
def test_ago(days, expected):
    import datetime
    today = datetime.date(2026, 10, 19)
    stamp = time.mktime((today - datetime.timedelta(days=days)).timetuple()) + 12 * 3600
    assert cli.ago(stamp, today) == expected

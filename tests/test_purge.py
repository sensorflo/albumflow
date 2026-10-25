import os

import pytest

from albumflow import annex, backup, card_import, cli, config, purge
from albumflow.files import AlbumflowError


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The home folder with Originals and Rejected, the annex, the primary and secondary drive, no card."""
    home, annex_drive = tmp_path / "home", tmp_path / "annex"
    primary, secondary = tmp_path / "P", tmp_path / "S"
    for drive, marker in ((primary, config.BACKUP_MARKER), (secondary, config.SECONDARY_BACKUP_MARKER)):
        drive.mkdir()
        (drive / marker).write_text("")
    annex_drive.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setattr(config, "ORIGINALS", home / "Large" / "Pictures" / "Originals")
    monkeypatch.setenv("ALBUMFLOW_ANNEX", str(annex_drive))
    monkeypatch.setattr(backup, "drives", lambda: [primary, secondary])
    monkeypatch.setattr(card_import, "find_cards", lambda: [])
    recycled = []

    def fake_recycle(path):  # never the real Recycle Bin
        recycled.append(sorted(p.name for p in path.rglob("*") if p.is_file()))
        annex.delete_tree(path, home)

    monkeypatch.setattr(purge, "recycle", fake_recycle)
    return {"home": home, "annex": annex_drive, "primary": primary, "secondary": secondary, "recycled": recycled}


def album(root, name, files=("1.ARW",)):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for file in files:
        (folder / file).write_bytes(b"x")
    return folder


def finish(name):
    (config.ORIGINALS / name).mkdir(parents=True)


def answers(*replies):
    replies = list(replies)
    return lambda prompt: replies.pop(0)


def run(argv, *replies):
    args = cli.build_parser().parse_args(["purge", *argv])
    return cli.cmd_purge(args, ask=answers(*replies))


def candidates(place):
    return {c.name: c.blocked for p, _, cs in purge.places() if p == place for c in cs}


def test_primary_always_secondary_only_when_finished(world):
    album(world["primary"] / config.COPIED_DIR, r"party\open")
    album(world["secondary"] / config.SECONDARY_DIR, r"party\open")
    album(world["secondary"] / config.SECONDARY_DIR, r"party\done")
    finish(r"party\done")
    assert candidates("primary") == {r"party\open": None}
    assert candidates("secondary") == {r"party\done": None, r"party\open": "not finished yet (not in Originals)"}


def test_card_once_in_a_secondary_backup_or_finished(world, tmp_path, monkeypatch):
    card = tmp_path / "card"
    (card / "DCIM").mkdir(parents=True)
    for name in ("on-secondary", "finished", "open"):
        album(card / config.CARD_BACKUP_DIR, name, ["1.ARW", "2.ARW"])
    album(world["primary"] / config.COPIED_DIR, r"party\on-secondary", ["1.ARW", "2.ARW"])
    album(world["primary"] / config.COPIED_DIR, r"party\open", ["1.ARW"])
    finish(r"party\finished")
    monkeypatch.setattr(card_import, "find_cards", lambda: [card])
    assert candidates("card") == {"on-secondary": None, "finished": None,
                                  "open": "1 of 2 photos not yet in a secondary backup"}


def test_rejected_covers_the_c_folder_and_its_annex_part(world):
    rejected = world["home"] / config.REJECTED_TREE
    here = album(rejected, r"party\fest", ["1.ARW"])
    there = album(world["annex"] / config.REJECTED_TREE, r"party\fest", ["2.ARW", "3.ARW"])
    annex.create_link(annex.link_of(here), there)
    album(world["secondary"] / config.SECONDARY_DIR, r"party\fest", ["1.ARW", "2.ARW"])
    [c] = [c for p, _, cs in purge.places() if p == "rejected" for c in cs]
    assert (c.name, sorted(c.files), c.blocked) == (r"party\fest", ["1.ARW", "2.ARW", "3.ARW"],
                                                     "1 of 3 photos not yet in a secondary backup")


def test_purge_rejected_to_the_recycle_bin_moves_the_annex_part_back_first(world):
    rejected = world["home"] / config.REJECTED_TREE
    here = album(rejected, r"party\fest", ["1.ARW"])
    there = album(world["annex"] / config.REJECTED_TREE, r"party\fest", ["2.ARW"])
    annex.create_link(annex.link_of(here), there)
    finish(r"party\fest")
    assert run(["rejected"], "1", "delete") == 0
    assert world["recycled"] == [["1.ARW", "2.ARW"]]  # both parts, from C:
    assert not rejected.joinpath("party").exists()  # the folder, its link, the topic folder
    assert not (world["annex"] / config.REJECTED_TREE / "party").exists()


def test_purge_rejected_permanently_deletes_in_place(world):
    rejected = world["home"] / config.REJECTED_TREE
    here = album(rejected, r"party\fest", ["1.ARW"])
    there = album(world["annex"] / config.REJECTED_TREE, r"party\fest", ["2.ARW"])
    annex.create_link(annex.link_of(here), there)
    finish(r"party\fest")
    assert run(["rejected", "--permanent"], "1", "delete") == 0
    assert world["recycled"] == []
    assert not os.path.lexists(annex.link_of(here)) and not here.exists() and not there.exists()


def test_blocked_albums_are_skipped_unless_forced(world):
    open_album = album(world["secondary"] / config.SECONDARY_DIR, r"party\open")
    assert run(["secondary"], "1") == 0  # skipped: no confirmation asked
    assert open_album.is_dir()
    assert run(["secondary", "--force"], "1", "delete") == 0
    assert not open_album.exists() and not open_album.parent.exists()


def test_needs_the_word_delete(world):
    copied = album(world["primary"] / config.COPIED_DIR, r"party\done")
    for confirmation in ("y", "", "yes", "Delete"):
        assert run(["primary"], "1", confirmation) == 0
        assert copied.is_dir()
    assert run(["primary"], "") == 0  # Enter: none, no second question
    assert copied.is_dir()


def test_without_a_place_all_available_ones(world, capsys):
    album(world["primary"] / config.COPIED_DIR, r"party\done")
    assert run([], "", "", "") == 0  # rejected, primary, secondary; no card
    out = capsys.readouterr().out
    assert out.index("\nrejected:") < out.index("\nprimary:") < out.index("\nsecondary:")
    assert "\ncard:" not in out


def test_a_place_that_isnt_available(world):
    with pytest.raises(AlbumflowError, match="card: not available"):
        run(["card"])


def test_update_secondary_scans_the_secondary_drive_into_the_index(world):
    from albumflow import index
    album(world["primary"] / config.BACKUP_DIR, r"party\fest", ["1.ARW"])
    album(world["secondary"] / config.SECONDARY_DIR, r"trip\old", ["9.ARW"])  # there before, outside albumflow
    backup.update_secondary(log=lambda *_: None)
    assert index.load(world["primary"])["secondary"]["albums"] == {r"party\fest": {"1.ARW": 1},
                                                                   r"trip\old": {"9.ARW": 1}}


def test_without_the_secondary_drive_the_index_tells_what_is_on_it(world, monkeypatch, capsys):
    """An unfinished album purged from copied-to-secondary isn't copied to the primary drive again."""
    from albumflow import index
    new = world["home"] / "new"
    monkeypatch.setattr(config, "PICTURES_NEW", new)
    album(new, r"party\fest", ["1.ARW", "2.ARW"])
    album(world["primary"] / config.BACKUP_DIR, r"party\fest", ["1.ARW"])
    backup.update_secondary(log=lambda *_: None)  # 1.ARW to the secondary drive and copied-to-secondary
    monkeypatch.setattr(backup, "drives", lambda: [world["primary"]])  # the secondary drive is in the office
    assert run(["primary"], "1", "delete") == 0  # purged from copied-to-secondary, though unfinished
    assert backup.on_secondary_drive(world["primary"], "party", "fest") == {"1.ARW": 1}
    assert cli.main(["backup-new", "--all", "--no-eject"]) == 0
    assert sorted(p.name for p in (world["primary"] / config.BACKUP_DIR / "party" / "fest").iterdir()) == ["2.ARW"]
    assert index.on_secondary(world["primary"], None, "fest") == {"1.ARW": 1}


def test_primary_blocked_while_clock_corrections_are_pending(world, monkeypatch):
    album(world["primary"] / config.COPIED_DIR, r"party\fest")
    monkeypatch.setattr(purge, "corrections_pending", lambda topic, album: (topic, album) == ("party", "fest"))
    assert candidates("primary") == {r"party\fest": "clock corrections pending, which need the originals here"}

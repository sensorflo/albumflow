import pytest

from albumflow import annex, cli, config
from albumflow.files import AlbumflowError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    drive = tmp_path / "annex"
    drive.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setenv("ALBUMFLOW_ANNEX", str(drive))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    monkeypatch.setattr(cli, "check_lightroom", lambda folders: None)
    monkeypatch.setattr(cli, "lightroom_hint", lambda report, log: None)
    return home, drive


def files(folder):
    """The files below folder, not looking through the '(annex)' junctions."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*")
            if p.is_file() and not any(part.endswith(config.ANNEX_SUFFIX) for part in p.relative_to(folder).parts)}


def rejected(home, name, content):
    folder = home / config.REJECTED_TREE / name
    folder.mkdir(parents=True, exist_ok=True)
    for file, data in content.items():
        (folder / file).write_bytes(data)
    return folder


def test_rejected_folder_moves_its_files_and_stays(setup):
    home, drive = setup
    folder = rejected(home, r"party\fest", {"1.ARW": b"one", "1.xmp": b"x"})
    target = annex.to_annex(folder, log=lambda *_: None)
    assert target == drive / config.REJECTED_TREE / "party" / "fest"
    assert files(target) == {"1.ARW": b"one", "1.xmp": b"x"}
    assert folder.is_dir() and not any(folder.iterdir())  # stays, empty: the drop target
    assert annex.is_annex_link(annex.link_of(folder))
    rejected(home, r"party\fest", {"2.ARW": b"two"})  # the next culling session
    annex.to_annex(folder, log=lambda *_: None)
    assert files(target) == {"1.ARW": b"one", "1.xmp": b"x", "2.ARW": b"two"}
    assert folder.is_dir() and not any(folder.iterdir())
    assert [e.kind for e in annex.status()] == ["ok"]


def test_merge_drops_identical_files_and_stops_at_different_ones(setup):
    home, drive = setup
    folder = rejected(home, r"party\fest", {"1.ARW": b"one"})
    annex.to_annex(folder, log=lambda *_: None)
    rejected(home, r"party\fest", {"1.ARW": b"one", "2.ARW": b"two"})  # e.g. an interrupted merge
    annex.to_annex(folder, log=lambda *_: None)
    assert not any(folder.iterdir())
    rejected(home, r"party\fest", {"2.ARW": b"TWO", "3.ARW": b"three"})
    with pytest.raises(AlbumflowError, match="other content"):
        annex.to_annex(folder, log=lambda *_: None)
    assert files(folder) == {"2.ARW": b"TWO", "3.ARW": b"three"}  # checked before moving anything


def test_elsewhere_a_folder_merges_into_its_annex_folder_and_goes(setup):
    home, drive = setup
    album = home / "Large" / "Pictures" / "Originals" / "party" / "fest"
    album.mkdir(parents=True)
    (album / "1.ARW").write_bytes(b"one")
    annex.to_annex(album, log=lambda *_: None)  # a whole move, as before
    assert not album.exists() and annex.is_annex_link(annex.link_of(album))
    album.mkdir()
    (album / "2.ARW").write_bytes(b"two")
    assert [e.kind for e in annex.status()] == ["merge pending (to-annex)"]
    annex.to_annex(album, log=lambda *_: None)
    assert not album.exists()
    assert files(drive / album.relative_to(home)) == {"1.ARW": b"one", "2.ARW": b"two"}


def test_to_annex_rejected_drains_every_rejected_folder(setup, capsys):
    home, drive = setup
    rejected(home, r"party\fest", {"1.ARW": b"one"})
    rejected(home, r"hollidays\trip", {"2.ARW": b"two"})
    rejected(home, r"party\empty", {})
    assert cli.main(["to-annex", "--rejected"]) == 0
    assert files(drive / config.REJECTED_TREE) == {r"party\fest\1.ARW": b"one", r"hollidays\trip\2.ARW": b"two"}
    assert files(home / config.REJECTED_TREE) == {}
    assert cli.main(["to-annex", "--rejected"]) == 0
    assert "Nothing to move" in capsys.readouterr().err


def test_in_rejected_tree(setup):
    home, _ = setup
    assert annex.in_rejected_tree(home / config.REJECTED_TREE / "party" / "fest")
    assert annex.in_rejected_tree(home / "large" / "pictures" / "rejected" / "Unsorted")  # case doesn't matter
    assert not annex.in_rejected_tree(home / config.REJECTED_TREE)
    assert not annex.in_rejected_tree(home / "Large" / "Pictures" / "Originals" / "party")

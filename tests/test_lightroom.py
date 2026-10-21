import sqlite3

import pytest

from albumflow import annex, cli, config, lightroom
from albumflow.files import AlbumflowError


def make_catalog(path, root, photos):
    """photos: (folder below root, e.g. 'party/fest/', sidecarStatus, is virtual copy)."""
    con = sqlite3.connect(path)
    con.executescript("""
        create table AgLibraryRootFolder (id_local integer primary key, absolutePath text);
        create table AgLibraryFolder (id_local integer primary key, pathFromRoot text, rootFolder integer);
        create table AgLibraryFile (id_local integer primary key, folder integer);
        create table Adobe_images (id_local integer primary key, rootFile integer, sidecarStatus real,
                                   masterImage integer);
    """)
    con.execute("insert into AgLibraryRootFolder values (1, ?)", (str(root).replace("\\", "/") + "/",))
    folders = {}
    for i, (folder, status, virtual) in enumerate(photos, 1):
        if folder not in folders:
            folders[folder] = len(folders) + 1
            con.execute("insert into AgLibraryFolder values (?, ?, 1)", (folders[folder], folder))
        con.execute("insert into AgLibraryFile values (?, ?)", (i, folders[folder]))
        con.execute("insert into Adobe_images values (?, ?, ?, ?)", (i, i, status, 1 if virtual else None))
    con.commit()
    con.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    new = home / "Large" / "Pictures" / "Pictures" / "new"
    drive = tmp_path / "annex"
    drive.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setattr(config, "PICTURES_NEW", new)
    monkeypatch.setenv("ALBUMFLOW_ANNEX", str(drive))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    for name in ("fest", "clean", "virtual"):
        (new / "party" / name / "sub").mkdir(parents=True)
        (new / "party" / name / "a.ARW").write_text("a")
    catalog = tmp_path / "test.lrcat"
    make_catalog(catalog, new, [
        ("party/fest/", 1, False), ("party/fest/", 0, False), ("party/fest/sub/", 1, False),
        ("party/clean/", 0, False), ("party/clean/", 0, False),
        ("party/virtual/", 0, False), ("party/virtual/", 0, True),
    ])
    monkeypatch.setattr(config, "LIGHTROOM_CATALOGS", [catalog])
    return home, new, drive


def test_check_counts_photos_unsaved_and_virtual(setup):
    home, new, drive = setup
    report = lightroom.check([new / "party" / "fest"])
    assert report.photos == 3
    assert sorted((p.relative_to(new).as_posix(), n) for p, n in report.unsaved.items()) == [
        ("party/fest", 1), ("party/fest/sub", 1)]
    assert lightroom.check([new / "party" / "virtual"]).virtual
    assert lightroom.check([home / "Music"]).photos == 0


def test_folder_name_prefix_is_not_a_match(setup):
    home, new, drive = setup
    (new / "party" / "fe").mkdir()
    assert lightroom.check([new / "party" / "fe"]).photos == 0  # "fe" must not match "fest"


def test_refuse_names_folders_relative_to_home(setup):
    home, new, drive = setup
    with pytest.raises(AlbumflowError) as e:
        lightroom.refuse_if_unsafe(lightroom.check([new / "party" / "fest"]), home)
    message = str(e.value)
    assert "Large\\Pictures\\Pictures\\new\\party\\fest (1 photos)" in message
    assert "Large\\Pictures\\Pictures\\new\\party\\fest\\sub (1 photos)" in message
    assert "Save Metadata to File" in message


def test_to_annex_refuses_unsaved_metadata_and_moves_nothing(setup):
    home, new, drive = setup
    assert cli.main(["to-annex", str(new / "party" / "fest")]) == 2
    assert (new / "party" / "fest" / "a.ARW").exists()
    assert not list(drive.rglob("*"))


def test_to_annex_refuses_virtual_copies(setup, capsys):
    home, new, drive = setup
    assert cli.main(["to-annex", str(new / "party" / "virtual")]) == 2
    assert "virtual copies" in capsys.readouterr().err
    assert (new / "party" / "virtual").is_dir()


def test_to_annex_clean_folder_moves_and_hints(setup, capsys):
    home, new, drive = setup
    assert cli.main(["to-annex", str(new / "party" / "clean")]) == 0
    assert annex.is_annex_link(new / "party" / "clean (annex)")
    assert "Lightroom has 2 photos of this folder" in capsys.readouterr().err


def test_no_hint_for_folders_lightroom_doesnt_know(setup, capsys):
    home, new, drive = setup
    music = home / "Large" / "Music" / "Goa"
    music.mkdir(parents=True)
    (music / "t.mp3").write_text("x")
    assert cli.main(["to-annex", str(music)]) == 0
    assert "Lightroom" not in capsys.readouterr().err


def test_missing_catalog_is_an_error(setup, monkeypatch, tmp_path):
    home, new, drive = setup
    monkeypatch.setattr(config, "LIGHTROOM_CATALOGS", [tmp_path / "nope.lrcat"])
    assert cli.main(["to-annex", str(new / "party" / "clean")]) == 2
    assert (new / "party" / "clean").is_dir()

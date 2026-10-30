import os

import pytest

from albumflow import album_choice, annex, cli, config
from albumflow.files import AlbumflowError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    drive = tmp_path / "annex"
    drive.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setattr(config, "PICTURES_NEW", home / "Large" / "Pictures" / "Pictures" / "new")
    monkeypatch.setenv("ALBUMFLOW_ANNEX", str(drive))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    album = config.PICTURES_NEW / "party" / "fest"
    (album / "sub").mkdir(parents=True)
    (album / "a.ARW").write_bytes(b"a" * 100)
    (album / "b.xmp").write_text("x")
    (album / "sub" / "c.jpg").write_bytes(b"c" * 10)
    (album / "empty").mkdir()
    return home, drive, album


def in_annex(drive, album):
    return drive / album.relative_to(config.HOME_FOLDER)


def test_to_annex_and_back(setup):
    home, drive, album = setup
    target = annex.to_annex(album, log=lambda *_: None)
    link = album.with_name("fest (annex)")
    assert target == in_annex(drive, album)
    assert not album.exists() and annex.is_annex_link(link)
    assert (link / "sub" / "c.jpg").read_bytes() == b"c" * 10  # readable through the link
    assert (target / "empty").is_dir()
    assert not (target / annex.MOVE_NOTE).exists()

    annex.from_annex(album, log=lambda *_: None)
    assert (album / "a.ARW").read_bytes() == b"a" * 100 and (album / "empty").is_dir()
    assert not link.exists() and not target.exists()
    assert not (album / annex.MOVE_NOTE).exists()


def test_dry_run_changes_nothing(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None, dry_run=True)
    assert album.is_dir() and not in_annex(drive, album).exists()


def test_resume_after_partial_copy(setup):
    home, drive, album = setup
    part = in_annex(drive, album).with_name("fest.part")
    annex.write_note(part, "to-annex", album)
    (part / "a.ARW").write_bytes(b"a" * 100)  # copied already
    (part / "sub").mkdir()
    (part / "sub" / "c.jpg").write_bytes(b"c" * 3)  # interrupted mid-file
    annex.to_annex(album, log=lambda *_: None)
    assert (in_annex(drive, album) / "sub" / "c.jpg").read_bytes() == b"c" * 10
    assert not album.exists() and annex.is_annex_link(album.with_name("fest (annex)"))


def test_resume_after_rename_before_delete(setup):
    home, drive, album = setup
    target = in_annex(drive, album)
    annex.copy_tree(album, target)
    annex.write_note(target, "to-annex", album)
    annex.to_annex(album, log=lambda *_: None)
    assert not album.exists() and annex.is_annex_link(album.with_name("fest (annex)"))


def test_resume_from_annex_after_link_removed(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)
    target = in_annex(drive, album)
    # interrupted after renaming .part to the album, before deleting the annex copy
    annex.copy_tree(target, album)
    annex.write_note(album, "from-annex", target)
    os.rmdir(album.with_name("fest (annex)"))
    annex.from_annex(album, log=lambda *_: None)
    assert album.is_dir() and not target.exists() and not (album / annex.MOVE_NOTE).exists()


def test_refuses_existing_in_annex(setup):
    home, drive, album = setup
    in_annex(drive, album).mkdir(parents=True)
    with pytest.raises(AlbumflowError, match="already exists in the annex"):
        annex.to_annex(album, log=lambda *_: None)
    assert (album / "a.ARW").exists()


def test_refuses_already_in_annex(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)
    with pytest.raises(AlbumflowError, match="already in the annex"):
        annex.to_annex(album, log=lambda *_: None)


def test_refuses_while_editors_run(setup, monkeypatch):
    home, drive, album = setup
    monkeypatch.setattr(annex, "running_editors", lambda: ["Lightroom.exe"])
    with pytest.raises(AlbumflowError, match="Lightroom.exe"):
        annex.to_annex(album, log=lambda *_: None)


def test_refuses_outside_home(setup, tmp_path):
    with pytest.raises(AlbumflowError, match="not inside the home folder"):
        annex.to_annex(tmp_path / "elsewhere", log=lambda *_: None)


def test_refuses_folder_containing_a_link(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)
    with pytest.raises(AlbumflowError, match="contains a junction"):
        annex.to_annex(album.parent, log=lambda *_: None)


def test_delete_never_goes_through_a_junction(tmp_path):
    target = tmp_path / "target"
    (target / "inner").mkdir(parents=True)
    (target / "inner" / "f").write_text("keep")
    annex.create_link(tmp_path / "link", target)
    with pytest.raises(AlbumflowError, match="is a junction"):
        annex.delete_tree(tmp_path / "link" / "inner", tmp_path)
    holder = tmp_path / "holder"
    holder.mkdir()
    annex.create_link(holder / "link", target)
    with pytest.raises(AlbumflowError, match="contains the junction"):
        annex.delete_tree(holder, tmp_path)
    os.rmdir(holder / "link")
    assert (target / "inner" / "f").read_text() == "keep"


def test_status(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)  # ok
    lost = config.PICTURES_NEW / "party" / "lost"
    in_annex(drive, lost).mkdir(parents=True)
    (in_annex(drive, lost) / "x.ARW").write_text("x")  # missing link
    both = config.PICTURES_NEW / "party" / "both"
    both.mkdir()
    (both / "y.ARW").write_text("y")
    in_annex(drive, both).mkdir()
    (in_annex(drive, both) / "y.ARW").write_text("y")  # in both places
    broken = config.PICTURES_NEW / "party" / "gone (annex)"
    gone_target = drive / "elsewhere"
    gone_target.mkdir()
    annex.create_link(broken, gone_target)
    gone_target.rmdir()  # broken link
    (drive / "old-layout" / "topic").mkdir(parents=True)  # no folder on C:

    kinds = {(e.kind, e.home_path.name) for e in annex.status()}
    assert ("ok", "fest") in kinds
    assert ("missing link", "lost") in kinds
    assert ("in both places", "both") in kinds
    assert ("broken link", "gone") in kinds
    assert ("no folder on C:", "old-layout") in kinds

    [missing] = [e for e in annex.status() if e.kind == "missing link"]
    annex.fix_link(missing)
    assert annex.is_annex_link(lost.with_name("lost (annex)"))


def test_annexed_albums_are_not_offered_for_import(setup):
    home, drive, album = setup
    assert "party/fest" in [str(a) for a in album_choice.albums()]
    annex.to_annex(album, log=lambda *_: None)
    assert "party/fest" not in [str(a) for a in album_choice.albums()]
    assert "party/fest (annex)" not in [str(a) for a in album_choice.albums()]


@pytest.mark.parametrize("text, expected", [
    (".", True), ("./foo/bar", True), ("~/Music", True), ("C:\\x", True), ("/c/x", True),
    ("party/fest", False), ("fest", False), ("", False),
])
def test_looks_like_path(text, expected):
    assert cli.looks_like_path(text) == expected


def test_from_annex_folder_from_link_path_or_query(setup, monkeypatch):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)
    assert cli.from_annex_folder(str(album.with_name("fest (annex)"))) == album
    assert cli.from_annex_folder(str(in_annex(drive, album))) == album
    assert cli.from_annex_folder("party/fest") == album  # exact query, no question


def test_to_annex_command_with_current_folder(setup, monkeypatch):
    home, drive, album = setup
    monkeypatch.chdir(album)
    assert cli.to_annex_folder(".") == album


def make_missing_links(drive, names):
    for name in names:
        folder = in_annex(drive, config.PICTURES_NEW / "party" / name)
        folder.mkdir(parents=True)
        (folder / "x.ARW").write_text("x")


@pytest.mark.parametrize("answers, linked", [
    (["y", "n", "y"], ["a1", "a3"]),
    (["n", "a"], ["a2", "a3"]),  # a: this one and all remaining
    (["y", "q"], ["a1"]),
    (["?", "y", "n", "n"], ["a1"]),  # an unknown answer is asked again
])
def test_fix_asks_per_link(setup, monkeypatch, capsys, answers, linked):
    home, drive, album = setup
    make_missing_links(drive, ["a1", "a2", "a3"])
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    cli.main(["annex-status", "--fix"])
    party = config.PICTURES_NEW / "party"
    assert sorted(p.name.removesuffix(" (annex)") for p in party.iterdir() if annex.is_annex_link(p)) == linked


def test_folder_with_only_subfolders(setup):
    home, drive, album = setup
    music = home / "Large" / "Music"
    music.mkdir(parents=True)
    (drive / "Large" / "Music" / "Goa" / "Album 1").mkdir(parents=True)
    (drive / "Large" / "Music" / "Goa" / "Album 1" / "t.mp3").write_text("x")
    (drive / "toplevel" / "sub").mkdir(parents=True)  # directly in the home folder: never linked
    kinds = {(e.kind, e.home_path.name) for e in annex.status()}
    assert ("missing link", "Goa") in kinds
    assert ("no folder on C:", "toplevel") in kinds


def test_a_git_worktree_on_both_sides_is_looked_through(setup):
    home, drive, album = setup
    annex.to_annex(album, log=lambda *_: None)
    pictures = home / "Large" / "Pictures"
    (pictures / ".gitignore").write_text("x")
    (pictures / "notes.txt").write_text("x")
    (drive / "Large" / "Pictures" / ".git").write_text("gitdir: C:/somewhere")
    (drive / "Large" / "Pictures" / ".gitignore").write_text("x")
    (drive / "Large" / "Pictures" / "Desktop.ini").write_text("x")
    kinds = {(e.kind, e.home_path.name) for e in annex.status()}
    assert kinds == {("ok", "fest")}

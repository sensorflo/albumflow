import argparse
import os
from pathlib import Path

import pytest

from albumflow import annex, cli, config, emdb, movies
from albumflow.files import AlbumflowError

SEP = emdb.SEPARATOR


def record(title, location):
    year = ["2019", "0", "118", "#US", "10106", "12", "1920x1080", emdb.encode(location)] + ["0"] * 9
    return ["    }, {", f'{emdb.TITLE_PREFIX}{title}{SEP}{SEP}{SEP}-1",', f"{emdb.YEAR_PREFIX}{SEP.join(year)}\",",
            '        "plot": "x"']


def write_db(path, entries):
    lines = ["{", '    "version": "80",', '    "movies": [{'] + [l for t, loc in entries for l in record(t, loc)] + ["}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(emdb.BOM + "\r\n".join(lines).encode("utf-16-le"))


def backup_db():
    folder = config.EMDB_BACKUPS / "EMDB_BACKUP_20261027_1700"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "emdb.dat").write_bytes(config.EMDB_DATABASE.read_bytes())


def locations():
    return {m.title: m.location for m in emdb.movies(emdb.read_lines(config.EMDB_DATABASE))}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home, hdd = tmp_path / "home", tmp_path / "hdd"
    hdd.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setenv("ALBUMFLOW_MOVIE_ANNEX", str(hdd))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    folder = home / "Large" / "Videos" / "Eraserhead (1977)"
    folder.mkdir(parents=True)
    (folder / "Eraserhead.mkv").write_bytes(b"e" * 100)
    (folder / "Featurettes").mkdir()
    (folder / "Featurettes" / "a.mkv").write_bytes(b"a" * 10)
    return home, hdd, folder


def quiet(*_):
    pass


def test_to_annex_and_back(setup):
    home, hdd, folder = setup
    target = movies.to_hdd(folder.name, log=quiet)
    assert target == hdd / "Large" / "Videos" / folder.name
    assert annex.is_link(folder)  # the link has the movie's own name
    assert (folder / "Featurettes" / "a.mkv").read_bytes() == b"a" * 10  # readable through it
    assert not (target / annex.MOVE_NOTE).exists()
    assert [e.kind for e in movies.status(hdd)] == ["on the movie annex"]

    movies.from_hdd(folder.name, log=quiet)
    assert not annex.is_link(folder) and (folder / "Eraserhead.mkv").read_bytes() == b"e" * 100
    assert not target.exists() and not (folder / annex.MOVE_NOTE).exists()
    assert [e.kind for e in movies.status(hdd)] == ["on C:"]


def test_dry_run_changes_nothing(setup):
    home, hdd, folder = setup
    movies.to_hdd(folder.name, log=quiet, dry_run=True)
    assert folder.is_dir() and not annex.is_link(folder) and not (hdd / "Large").exists()


def test_resume_after_a_delete_stopped_halfway(setup):
    """E.g. a file open in a player: the copy is complete, only the rest is deleted, then linked."""
    home, hdd, folder = setup
    target = hdd / "Large" / "Videos" / folder.name
    annex.write_note(target, "movie-to-annex", folder)
    annex.copy_tree(folder, target)
    (folder / "Featurettes" / "a.mkv").unlink()  # deleted already
    movies.to_hdd(folder.name, log=quiet)
    assert annex.is_link(folder) and (target / "Featurettes" / "a.mkv").is_file()


def test_refuses_what_is_already_there(setup):
    home, hdd, folder = setup
    (hdd / "Large" / "Videos" / folder.name).mkdir(parents=True)
    with pytest.raises(AlbumflowError, match="already exists"):
        movies.to_hdd(folder.name, log=quiet)
    assert [e.kind for e in movies.status(hdd)] == ["in both places"]


def test_without_the_hdd(setup, monkeypatch):
    home, hdd, folder = setup
    movies.to_hdd(folder.name, log=quiet)
    monkeypatch.setenv("ALBUMFLOW_MOVIE_ANNEX", str(hdd / "gone"))
    assert [e.kind for e in movies.status(movies.find_hdd())] == ["on the movie annex (not connected)"]
    with pytest.raises(AlbumflowError, match="not connected"):
        movies.from_hdd(folder.name, log=quiet)


def test_link_to_a_volume(tmp_path, monkeypatch):
    """The real thing: a junction to \\\\?\\Volume{...}\\..., as for the HDD (here C:'s volume)."""
    monkeypatch.delenv("ALBUMFLOW_MOVIE_ANNEX", raising=False)
    (tmp_path / "movie").mkdir()
    (tmp_path / "movie" / "m.mkv").write_text("m")
    target = movies.volume_path(tmp_path / "movie")
    assert target.startswith("\\\\?\\Volume{")
    movies.create_link(tmp_path / "link", target)
    assert (tmp_path / "link" / "m.mkv").read_text() == "m"
    assert movies.same_target(tmp_path / "link", target)
    os.rmdir(tmp_path / "link")
    assert (tmp_path / "movie" / "m.mkv").is_file()


def test_choose_movie(setup):
    names = ["Eraserhead (1977)", "Parasite.2019", "The.Death.of.Stalin.2017"]
    assert cli.choose_movie("parasite", names) == "Parasite.2019"
    assert cli.choose_movie("death stalin", names) == "The.Death.of.Stalin.2017"
    assert cli.choose_movie("20", names, ask=lambda _: "2") == "The.Death.of.Stalin.2017"  # two contain 20
    with pytest.raises(AlbumflowError):
        cli.choose_movie("nothing", names)


# --- EMDB ---

def test_emdb_round_trip_and_location_change(tmp_path):
    path = tmp_path / "emdb.dat"
    write_db(path, [("1917", R"[FloBig]D:\Videos\1917 [BluRay].mkv"), ("No file", "")])
    raw = path.read_bytes()
    lines = emdb.read_lines(path)
    emdb.write_lines(path, lines)
    assert path.read_bytes() == raw  # unchanged when nothing changes
    found = emdb.movies(lines)
    assert [(m.title, m.location) for m in found] == [("1917", R"[FloBig]D:\Videos\1917 [BluRay].mkv"), ("No file", "")]
    assert emdb.without_label(found[0].location) == R"D:\Videos\1917 [BluRay].mkv"
    assert "<SC>BluRay<SCB>" in raw.decode("utf-16")
    emdb.write_lines(path, emdb.with_locations(lines, {found[0].line: R"C:\x\1917 [BluRay].mkv"}))
    assert [m.location for m in emdb.movies(emdb.read_lines(path))] == [R"C:\x\1917 [BluRay].mkv", ""]


def test_emdb_unknown_format(tmp_path):
    path = tmp_path / "emdb.dat"
    path.write_bytes(emdb.BOM + f'{emdb.TITLE_PREFIX}x",\r\n{emdb.YEAR_PREFIX}2019{SEP}0",'.encode("utf-16-le"))
    with pytest.raises(AlbumflowError, match="format"):
        emdb.movies(emdb.read_lines(path))


def test_emdb_written_only_when_closed_and_backed_up(tmp_path):
    write_db(config.EMDB_DATABASE, [("1917", R"D:\Videos\1917.mkv")])
    with pytest.raises(AlbumflowError, match="backup"):
        emdb.check_safe_to_write(config.EMDB_DATABASE, [])
    backup_db()
    with pytest.raises(AlbumflowError, match="running"):
        emdb.check_safe_to_write(config.EMDB_DATABASE, ["emdb.exe"])
    emdb.check_safe_to_write(config.EMDB_DATABASE, [])
    write_db(config.EMDB_DATABASE, [("1917", R"D:\Videos\1917 (changed).mkv")])
    with pytest.raises(AlbumflowError, match="backup"):  # the backup is older than the database
        emdb.check_safe_to_write(config.EMDB_DATABASE, [])


def args(**kw):
    return argparse.Namespace(**{"fix": False, "verbose": False, **kw})


def test_migration(setup, capsys):
    """The HDD's old layout, a loose movie file, links, and EMDB's Locations, fixed in one go."""
    home, hdd, folder = setup
    old = hdd / "Videos"
    (old / "Parasite.2019").mkdir(parents=True)
    (old / "Parasite.2019" / "Parasite.mkv").write_bytes(b"p")
    (old / "1917.2019.mkv").write_bytes(b"1")
    (old / "1917.2019.en.srt").write_text("s")
    (old / "Fifth Element" / "BDMV").mkdir(parents=True)
    (old / "Fifth Element" / "BDMV" / "00001.m2ts").write_bytes(b"f")
    videos = home / "Large" / "Videos"
    write_db(config.EMDB_DATABASE, [
        ("Parasite", R"[FloBig]D:\Videos\Parasite.2019\Parasite.mkv"),
        ("1917", R"D:\Videos\1917.2019.mkv"),
        ("The Fifth Element", R"S:\media\sensorflo\7e0fb0f5-7af1-4290-8fe5-e6a35b6c12b8\Videos\Fifth Element\BDMV"
                              R"\00001.m2ts"),
        ("Eraserhead", str(folder / "Eraserhead.mkv")),
        ("Gone", R"D:\Videos\Gone.mkv"),
        ("Not a movie", R"C:\Users\senso\Documents\x.mkv"),
    ])
    backup_db()

    assert cli.cmd_movie_status(args()) == 1
    assert "old layout (1)" in capsys.readouterr().out
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: "y") == 1  # Gone stays a problem
    out = capsys.readouterr().out
    assert "1 problem(s) left" in out

    assert not old.exists()
    assert annex.is_link(videos / "Parasite.2019") and annex.is_link(videos / "1917.2019")
    assert (videos / "1917.2019" / "1917.2019.en.srt").is_file()  # with its movie
    assert locations() == {
        "Parasite": str(videos / "Parasite.2019" / "Parasite.mkv"),
        "1917": str(videos / "1917.2019" / "1917.2019.mkv"),
        "The Fifth Element": str(videos / "Fifth Element" / "BDMV" / "00001.m2ts"),
        "Eraserhead": str(folder / "Eraserhead.mkv"),
        "Gone": R"D:\Videos\Gone.mkv",
        "Not a movie": R"C:\Users\senso\Documents\x.mkv",
    }
    entries, changes, _ = cli.movie_problems(hdd)
    assert not changes and [e.name for e in entries if e.problem] == ["Gone"]


def test_emdb_untouched_while_running(setup, monkeypatch):
    home, hdd, folder = setup
    (hdd / "Large" / "Videos" / "Parasite").mkdir(parents=True)
    (hdd / "Large" / "Videos" / "Parasite" / "p.mkv").write_bytes(b"p")
    write_db(config.EMDB_DATABASE, [("Parasite", R"D:\Videos\Parasite\p.mkv")])
    backup_db()
    before = config.EMDB_DATABASE.read_bytes()
    monkeypatch.setattr(annex, "running_editors", lambda: ["emdb.exe"])
    with pytest.raises(AlbumflowError, match="running"):
        cli.cmd_movie_status(args(fix=True), ask=lambda _: "y")
    assert config.EMDB_DATABASE.read_bytes() == before
    assert annex.is_link(home / "Large" / "Videos" / "Parasite")  # the link was created before


def test_movies_not_in_emdb(setup, capsys):
    home, hdd, folder = setup
    (home / "Large" / "Videos" / "Captures").mkdir()  # not a movie
    write_db(config.EMDB_DATABASE, [])
    assert cli.cmd_movie_status(args()) == 1
    out = capsys.readouterr().out
    assert "not in EMDB (1)" in out and "Eraserhead" in out and "Captures" not in out

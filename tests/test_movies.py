import argparse
import os
from pathlib import Path

import pytest

from albumflow import annex, cli, config, emdb, mediainfo, movienames, movies
from albumflow.files import AlbumflowError

SEP = emdb.SEPARATOR


# --- helpers ---

def record(title, location, original="", year="2019"):
    fields = [year, "0", "118", "#US", "10106", "12", "1920x1080", emdb.encode(location)] + ["0"] * 9
    return ["    }, {", f'{emdb.TITLE_PREFIX}{title}{SEP}{"#O" + original if original else ""}{SEP}{SEP}-1",',
            f"{emdb.YEAR_PREFIX}{SEP.join(fields)}\",", '        "plot": "x"']


def write_db(path, entries):
    """entries: (title, location) or (title, location, original, year)"""
    lines = ["{", '    "version": "80",', '    "movies": [{'] + [l for e in entries for l in record(*e)] + ["}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(emdb.BOM + "\r\n".join(lines).encode("utf-16-le"))


def backup_db():
    folder = config.EMDB_BACKUPS / "EMDB_BACKUP_20261027_1700"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "emdb.dat").write_bytes(config.EMDB_DATABASE.read_bytes())


def locations():
    return {m.title: m.location for m in emdb.movies(emdb.read_lines(config.EMDB_DATABASE))}


def video(width=1920, height=1080, fmt="AVC", rate=29e6, hdr=""):
    return {"@type": "Video", "Format": fmt, "Width": str(width), "Height": str(height), "BitRate": str(int(rate)),
            "HDR_Format": hdr}


def audio(lang, fmt="FLAC", ch=2, rate=None, commercial="", features="", title="", default="No"):
    track = {"@type": "Audio", "Format": fmt, "Channels": str(ch), "Language": lang, "Title": title,
             "Format_Commercial_IfAny": commercial, "Format_AdditionalFeatures": features, "Default": default}
    if rate:
        track["BitRate"] = str(rate)
    return track


def sub(lang, forced="No", title=""):
    return {"@type": "Text", "Format": "UTF-8", "Language": lang, "Forced": forced, "Title": title}


ERASERHEAD_TRACKS = [{"@type": "General"}, video(1920, 1040, rate=8.8e6), audio("en"), sub("en")]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home, hdd = tmp_path / "home", tmp_path / "hdd"
    hdd.mkdir()
    monkeypatch.setattr(config, "HOME_FOLDER", home)
    monkeypatch.setenv("ALBUMFLOW_MOVIE_ANNEX", str(hdd))
    monkeypatch.setattr(annex, "running_editors", lambda: [])
    tracks = {}  # file name -> MediaInfo's tracks; others: Eraserhead's
    runs = []

    def fake_run(path):
        runs.append(path.name)
        return tracks.get(path.name, ERASERHEAD_TRACKS)
    monkeypatch.setattr(mediainfo, "run", fake_run)
    release = movie(home, "Eraserhead (1977) REMASTERED 1080p H264 FLAC", "Eraserhead.mkv", 100)
    (release.parent / "Featurettes").mkdir()
    (release.parent / "Featurettes" / "a.mkv").write_bytes(b"a" * 10)
    return argparse.Namespace(home=home, hdd=hdd, release=release.parent, tracks=tracks, runs=runs,
                              view=home / "Large" / "Videos" / "Videos")


def movie(root, release, file="m.mkv", size=10, store=None):
    path = (store or root / config.MOVIE_STORE) / release / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"m" * size)
    return path


def quiet(*_):
    pass


def args(**kw):
    return argparse.Namespace(**{"fix": False, "verbose": False, **kw})


def kinds(entries):
    return sorted((e.kind, e.name) for e in entries)


def link_all(hdd):
    found, entries = movies.status(hdd)
    movies.fix_links(found, entries, hdd, log=quiet)


# --- store and view ---

def test_links_and_moves(setup):
    s = setup
    link_all(s.hdd)
    link = s.view / s.release.name
    assert annex.is_link(link) and (link / "Eraserhead.mkv").read_bytes() == b"m" * 100
    assert kinds(movies.status(s.hdd)[1]) == [("on C:", s.release.name)]

    target = movies.to_hdd(s.release.name, log=quiet)
    assert target == s.hdd / config.MOVIE_STORE / s.release.name and not s.release.exists()
    assert (link / "Featurettes" / "a.mkv").read_bytes() == b"a" * 10  # the link points there now
    assert not (target / annex.MOVE_NOTE).exists()
    assert kinds(movies.status(s.hdd)[1]) == [("on the movie annex", s.release.name)]

    movies.from_hdd(s.release.name, log=quiet)
    assert s.release.is_dir() and not target.exists() and not (s.release / annex.MOVE_NOTE).exists()
    assert movies.same_target(link, str(s.release))


def test_dry_run_changes_nothing(setup):
    movies.to_hdd(setup.release.name, log=quiet, dry_run=True)
    assert setup.release.is_dir() and not (setup.hdd / "Large").exists()


def test_resume_after_a_delete_stopped_halfway(setup):
    """E.g. a file open in a player: the copy is complete, only the rest is deleted."""
    s = setup
    target = s.hdd / config.MOVIE_STORE / s.release.name
    annex.write_note(target, "movie-to-annex", s.release)
    annex.copy_tree(s.release, target)
    (s.release / "Featurettes" / "a.mkv").unlink()  # deleted already
    movies.to_hdd(s.release.name, log=quiet)
    assert not s.release.exists() and (target / "Featurettes" / "a.mkv").is_file()


def test_moved_by_qbittorrent(setup):
    """qBittorrent's Set location moves the folder; the link is then pointed there."""
    s = setup
    link_all(s.hdd)
    (s.hdd / config.MOVIE_STORE).mkdir(parents=True)
    os.rename(s.release, s.hdd / config.MOVIE_STORE / s.release.name)
    found, entries = movies.status(s.hdd)
    assert kinds(entries) == [("link to fix", s.release.name)]
    movies.fix_links(found, entries, s.hdd, log=quiet)
    assert kinds(movies.status(s.hdd)[1]) == [("on the movie annex", s.release.name)]


def test_without_the_hdd(setup, monkeypatch):
    s = setup
    link_all(s.hdd)
    movies.to_hdd(s.release.name, log=quiet)
    monkeypatch.setenv("ALBUMFLOW_MOVIE_ANNEX", str(s.hdd / "gone"))
    assert kinds(movies.status(None)[1]) == [("not on C: (movie annex not connected)", s.release.name)]
    with pytest.raises(AlbumflowError, match="not connected"):
        movies.from_hdd(s.release.name, log=quiet)


def test_gone_and_foreign(setup):
    s = setup
    link_all(s.hdd)
    annex.delete_tree(s.release, s.home)
    (s.view / "a folder").mkdir()
    found, entries = movies.status(s.hdd)
    assert kinds(entries) == [("movie gone", s.release.name), ("not a link", "a folder")]
    movies.fix_links(found, entries, s.hdd, log=quiet)
    assert not os.path.lexists(s.view / s.release.name)


def test_link_to_a_volume(tmp_path, monkeypatch):
    """The real thing: a junction to \\\\?\\Volume{...}\\..., as for the HDD (here C:'s volume)."""
    monkeypatch.delenv("ALBUMFLOW_MOVIE_ANNEX", raising=False)
    (tmp_path / "movie").mkdir()
    (tmp_path / "movie" / "m.mkv").write_text("m")
    target = movies.volume_path(tmp_path / "movie")
    assert target.startswith("\\\\?\\Volume{")
    movies.create_link(tmp_path / "link", target)
    assert (tmp_path / "link" / "m.mkv").read_text() == "m"
    assert movies.same_target(tmp_path / "link", target) and movies.release_of(os.readlink(tmp_path / "link")) == "movie"
    os.rmdir(tmp_path / "link")
    assert (tmp_path / "movie" / "m.mkv").is_file()


def test_choose_movie():
    options = {"Eraserhead (1977) [x]": "Eraserhead.1977", "Eraserhead.1977": "Eraserhead.1977",
               "Parasite.2019": "Parasite.2019", "The.Death.of.Stalin.2017": "The.Death.of.Stalin.2017"}
    assert cli.choose_movie("parasite", options) == "Parasite.2019"
    assert cli.choose_movie("eraserhead", options) == "Eraserhead.1977"  # by both names: one movie
    assert cli.choose_movie("death stalin", options) == "The.Death.of.Stalin.2017"
    assert cli.choose_movie("20", options, ask=lambda _: "2") == "The.Death.of.Stalin.2017"
    with pytest.raises(AlbumflowError):
        cli.choose_movie("nothing", options)


# --- names ---

@pytest.mark.parametrize("release, edition", [
    ("Donnie.Darko.2001.REMASTERED.Directors.Cut.1080p.BluRay-FGT", "Remastered, Director's Cut"),
    ("Apocalypse.Now.1979.Final.Cut.2160p.UHD.BluRay", "Final Cut"),
    ("Strange.Days.1995.20th.Anniv.GER.Bluray.1080p", "20th Anniversary"),
    ("Any Given Sunday 1999 DC 1080p BluRay DTS x264-CtrlHD", "Director's Cut"),
    ("The.Discreet.Charm.of.the.Bourgeoisie.1972.RM4K.1080p", "Remastered"),
    ("The.Final.Cut.2004.1080p.BluRay", ""),  # a title, not an edition
    ("Parasite.2019.KORAEN.1080p.BluRay.REMUX", ""),
])
def test_edition(release, edition):
    assert movienames.edition(release) == edition


def test_canonical_and_safe():
    assert movienames.canonical("Parasite", "기생충", "2019", "Parasite.2019.1080p", "1080p AVC 29Mbps") \
        == "Parasite - 기생충 (2019) [1080p AVC 29Mbps]"
    assert movienames.canonical("2001: A Space Odyssey", "", "1968", "2001.A.Space.Odyssey.1968.REMASTERED", None) \
        == "2001 - A Space Odyssey (1968) {Remastered}"
    assert movienames.canonical("Holy Motors", "Holy Motors", "2012", "x", None) == "Holy Motors (2012)"
    assert movienames.safe('Face/Off: What? "Now" *|<>.') == "Face-Off - What Now"


def test_describe():
    parasite = [{"@type": "General"}, video(),
                audio("ko", "MLP FBA", 8, commercial="Dolby TrueHD with Dolby Atmos", features="16-ch"),
                audio("ko", "DTS", 8, commercial="DTS-HD Master Audio", features="XLL"),
                audio("en", "AC-3", 6, rate=640000, title="English"), audio("de", "AC-3", 6, rate=640000),
                audio("es-419", "AC-3", 2, rate=192000), audio("en", "AC-3", 2, rate=192000, title="Commentary"),
                sub("en"), sub("fr"), sub("de", forced="Yes"), sub("it", title="Italian Forced")]
    assert mediainfo.describe(parasite) == "1080p AVC 29Mbps, ko TrueHD Atmos 7.1, en de AC3 5.1 640kbps, +1, sub en +1"
    discreet = [{"@type": "General", "OverallBitRate": "5200000"}, video(1792, 1080, "HEVC", 0)]
    discreet += [audio(lang, ch=1) for lang in ("fr", "en", "de")] + [sub("en"), sub("de"), sub("fr")]
    assert mediainfo.describe(discreet) == "1080p HEVC 5Mbps, fr en de FLAC 1.0, sub fr en de"
    assert mediainfo.describe([video(3840, 1600, "HEVC", 18e6, "Dolby Vision, HDR10")]) == "2160p DV HDR10 HEVC 18Mbps"
    assert mediainfo.describe([{"@type": "General"}]) is None  # e.g. not downloaded completely


def test_doubtful_languages():
    found = mediainfo.doubtful_languages([audio("en", title="German DD 5.1"), audio("und"), audio("de", title="DD")])
    assert found == ["audio track tagged en, titled 'German DD 5.1'", "audio track without a language ('')"]


# --- EMDB ---

def test_emdb_round_trip_and_location_change(tmp_path):
    path = tmp_path / "emdb.dat"
    write_db(path, [("1917", R"[FloBig]D:\Videos\1917 [BluRay].mkv"), ("Parasite", "", "기생충")])
    raw = path.read_bytes()
    lines = emdb.read_lines(path)
    emdb.write_lines(path, lines)
    assert path.read_bytes() == raw  # unchanged when nothing changes
    found = emdb.movies(lines)
    assert [(m.title, m.original, m.location) for m in found] == [
        ("1917", "", R"[FloBig]D:\Videos\1917 [BluRay].mkv"), ("Parasite", "기생충", "")]
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


def test_migration(setup, capsys):
    """The HDD's old layout, a loose movie file, links, names and EMDB's Locations, fixed in one go."""
    s = setup
    old = s.hdd / "Videos"
    movie(s.hdd, "Parasite.2019.KORAEN.1080p", "Parasite.mkv", store=old)
    (old / "1917.2019.mkv").write_bytes(b"1")
    (old / "1917.2019.en.srt").write_text("s")
    movie(s.hdd, "Fifth Element 1997", R"BDMV\STREAM\00001.m2ts", store=old)
    s.tracks["Parasite.mkv"] = [video(), audio("ko", "DTS", 6, commercial="DTS-HD Master Audio"), sub("en")]
    write_db(config.EMDB_DATABASE, [
        ("Parasite", R"[FloBig]D:\Videos\Parasite.2019.KORAEN.1080p\Parasite.mkv", "기생충", "2019"),
        ("1917", R"D:\Videos\1917.2019.mkv"),
        ("The Fifth Element", R"S:\media\sensorflo\7e0fb0f5-7af1-4290-8fe5-e6a35b6c12b8\Videos\Fifth Element 1997"
                              R"\BDMV\STREAM\00001.m2ts", "", "1997"),
        ("Eraserhead", R"C:\Users\senso\Large\Videos" + "\\" + s.release.name + R"\Eraserhead.mkv", "", "1977"),
        ("Gone", R"D:\Videos\Gone.mkv"),
        ("Not a movie", R"C:\Users\senso\Documents\x.mkv"),
    ])
    backup_db()

    assert cli.cmd_movie_status(args()) == 1
    assert "old layout (1)" in capsys.readouterr().out
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: "y") == 0
    assert "No problems left" in capsys.readouterr().out

    assert not old.exists() and (s.hdd / config.MOVIE_STORE / "1917.2019" / "1917.2019.en.srt").is_file()
    eraserhead = "Eraserhead (1977) {Remastered} [1080p AVC 9Mbps, en FLAC 2.0, sub en]"
    names = {"Parasite": "Parasite - 기생충 (2019) [1080p AVC 29Mbps, ko DTS-HD MA 5.1, sub en]",
             "1917": "1917 (2019) [1080p AVC 9Mbps, en FLAC 2.0, sub en]",
             "The Fifth Element": "The Fifth Element (1997) [1080p AVC 9Mbps, en FLAC 2.0, sub en]"}
    assert sorted(p.name for p in s.view.iterdir()) == sorted([eraserhead, *names.values()])
    assert all(annex.is_link(p) for p in s.view.iterdir())
    assert locations() == {
        "Parasite": str(s.view / names["Parasite"] / "Parasite.mkv"),
        "1917": str(s.view / names["1917"] / "1917.2019.mkv"),
        "The Fifth Element": str(s.view / names["The Fifth Element"] / "BDMV" / "STREAM" / "00001.m2ts"),
        "Eraserhead": str(s.view / eraserhead / "Eraserhead.mkv"),
        "Gone": "",  # cleared, the entry stays
        "Not a movie": R"C:\Users\senso\Documents\x.mkv",  # not a movie's: none of albumflow's business
    }
    assert (s.view / names["Parasite"] / "Parasite.mkv").is_file()  # through the link, on the HDD


def test_filesystem_is_the_master(setup):
    """Movies added (found by title and year, or chosen), renamed (found by file name) and removed."""
    s = setup
    parasite = movie(s.home, "Parasite.2019.KORAEN.1080p.BluRay.REMUX-FGT", "Parasite.mkv")
    motors = movie(s.home, "Корпорация «Святые моторы» (Holy Motors) 2012, BDRemux", R"BDMV\STREAM\00001.m2ts", 50)
    (motors.parent / "00002.m2ts").write_bytes(b"x")  # smaller: not the movie
    renamed = movie(s.home, "Stalin (renamed)", "Death.of.Stalin.mkv")
    link_all(s.hdd)
    write_db(config.EMDB_DATABASE, [
        ("Parasite", ""), ("Parasite", ""),  # two entries could be it: no guess
        ("Holy Motors", "", "", "2012"),
        ("The Death of Stalin", str(s.view / "The Death of Stalin (2017) [x]" / "Death.of.Stalin.mkv")),
        ("Removed", str(s.view / "Removed (2019)" / "r.mkv")),
        ("Eraserhead", str(s.release / "Eraserhead.mkv"), "", "1977"),  # directly into the store
    ])
    backup_db()

    sync = cli.movie_problems(s.hdd, mediainfo.Reader())[2]
    found = {(e.kind, e.name) for e in sync.entries}
    assert {("Location to set", "Holy Motors"), ("Location outdated", "The Death of Stalin"),
            ("Location to clear", "Removed"), ("not in EMDB", parasite.parent.name),
            ("Location outdated", "Eraserhead")} <= found

    answers = iter(["1", "y"])  # the first Parasite, then yes
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: next(answers)) == 0
    tech = "[1080p AVC 9Mbps, en FLAC 2.0, sub en]"
    assert [(m.title, m.location) for m in emdb.movies(emdb.read_lines(config.EMDB_DATABASE))] == [
        ("Parasite", str(s.view / f"Parasite (2019) {tech}" / "Parasite.mkv")), ("Parasite", ""),
        ("Holy Motors", str(s.view / f"Holy Motors (2012) {tech}" / "BDMV" / "STREAM" / "00001.m2ts")),
        ("The Death of Stalin", str(s.view / f"The Death of Stalin (2019) {tech}" / "Death.of.Stalin.mkv")),
        ("Removed", ""),
        ("Eraserhead", str(s.view / f"Eraserhead (1977) {{Remastered}} {tech}" / "Eraserhead.mkv"))]
    assert all(annex.is_link(p) for p in s.view.iterdir()) and len(list(s.view.iterdir())) == 4
    assert renamed.is_file()  # the store is never renamed


def test_names_kept_while_the_hdd_is_disconnected(setup, monkeypatch):
    """MediaInfo's findings are kept, so names are made without the movie annex; Locations to it stay."""
    s = setup
    link_all(s.hdd)
    write_db(config.EMDB_DATABASE, [("Eraserhead", "", "", "1977"), ("Old", R"D:\Videos\Old\o.mkv")])
    backup_db()
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: "y") == 0
    movies.to_hdd(s.release.name, log=quiet)
    monkeypatch.setenv("ALBUMFLOW_MOVIE_ANNEX", str(s.hdd / "gone"))
    runs = len(s.runs)
    entries, _, sync, _ = cli.movie_problems(None, mediainfo.Reader())
    assert not sync.changes and not sync.renames and not [e for e in entries if e.problem]
    assert len(s.runs) == runs  # from the kept findings


def test_nothing_renamed_while_emdb_runs(setup, monkeypatch):
    s = setup
    link_all(s.hdd)
    write_db(config.EMDB_DATABASE, [("Eraserhead", "", "", "1977")])
    backup_db()
    before = config.EMDB_DATABASE.read_bytes()
    monkeypatch.setattr(annex, "running_editors", lambda: ["emdb.exe"])
    with pytest.raises(AlbumflowError, match="running"):
        cli.cmd_movie_status(args(fix=True), ask=lambda _: "y")
    assert config.EMDB_DATABASE.read_bytes() == before
    assert [p.name for p in s.view.iterdir()] == [s.release.name]


def test_not_in_emdb_without_a_choice(setup, capsys):
    write_db(config.EMDB_DATABASE, [])
    backup_db()
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: "y") == 1
    assert "add the movie in EMDB, then run --fix again" in capsys.readouterr().out


def test_long_path_is_a_hint(setup, monkeypatch, capsys):
    s = setup
    monkeypatch.setattr(config, "EMDB_MAX_PATH", 50)
    link_all(s.hdd)
    write_db(config.EMDB_DATABASE, [("Eraserhead", "", "", "1977")])
    backup_db()
    assert cli.cmd_movie_status(args(fix=True), ask=lambda _: "y") == 0  # a hint, not a problem
    assert cli.cmd_movie_status(args()) == 0
    assert "long path (1)" in capsys.readouterr().out


@pytest.mark.parametrize("name, title, year", [
    ("1917.2019.BDREMUX.1080p.seleZen", "1917", "2019"),
    ("Blade.Runner.2049.2017.BluRay.1080p", "Blade Runner 2049", "2017"),
    ("2001.A.Space.Odyssey.1968.REMASTERED", "2001: A Space Odyssey", "1968"),
    ("The.Death.of.Stalin.2017.1080p", "The Death of Stalin", "2018"),  # release years may differ by one
    ("Eraserhead (1977) REMASTERED 1080p", "Eraserhead", "1977"),
])
def test_match_title(name, title, year):
    found = movies.match_title(name, [emdb.Movie("Other", "", year, "", 2), emdb.Movie(title, "", year, "", 1)])
    assert found.title == title


def test_match_title_not():
    assert movies.match_title("Upgrade.2018", [emdb.Movie("Up", "", "2018", "", 1)]) is None  # whole words only
    assert movies.match_title("Dune.2021", [emdb.Movie("Dune", "", "1984", "", 1)]) is None

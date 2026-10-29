from pathlib import Path

import pytest

from albumflow import backup, card_import, cli, config, files
from albumflow.files import AlbumflowError, NoEject

CARD, PRIMARY, SECONDARY = Path("E:\\"), Path("F:\\Backup\\pictures-new"), Path("G:\\")


@pytest.fixture
def labels(monkeypatch):
    names = {"E": "SONY", "F": "FloBig", "G": "Office"}
    monkeypatch.setattr(files, "volume_label", lambda drive: names[drive.drive[0]])
    monkeypatch.setattr(cli, "volume_label", lambda drive: names[drive.drive[0]])
    monkeypatch.setattr(cli, "drives", lambda: [Path(f"{letter}:\\") for letter in names])


def ejected(keep):
    return [role for drive, role in ((CARD, "card"), (PRIMARY, "primary"), (SECONDARY, "secondary"))
            if keep.ejects(drive, role)]


@pytest.mark.parametrize("names, expected", [
    ([], ["card", "primary", "secondary"]),
    (["e"], ["primary", "secondary"]),
    (["F:"], ["card", "secondary"]),
    (["sdcards"], ["primary", "secondary"]),
    (["drive"], ["card", "secondary"]),
    (["Backup-Drive", "secondary"], ["card"]),
    (["primary"], ["card", "secondary"]),
    (["flobig"], ["card", "secondary"]),
    (["Office", "SONY"], ["primary"]),
    (["nothing"], ["card", "primary", "secondary"]),
])
def test_ejects_all_but_the_drives_named(labels, names, expected):
    assert ejected(NoEject(names)) == expected


def test_keep_all():
    assert ejected(NoEject(keep_all=True)) == []


def parse(argv):
    return cli.build_parser().parse_args(cli.bare_no_eject(argv, cli.build_parser()))


@pytest.mark.parametrize("argv, value", [
    (["import", "trip/day1"], None),
    (["import", "--no-eject", "trip/day1"], ""),  # the album isn't taken as a drive
    (["import", "trip/day1", "--no-eject"], ""),
    (["import", "--no-ej", "trip/day1"], ""),
    (["import", "--no-eject=drive,e:", "trip/day1"], "drive,e:"),
])
def test_no_eject_takes_drives_only_after_an_equals_sign(argv, value):
    args = parse(argv)
    assert (args.no_eject, args.album_query) == (value, "trip/day1")


@pytest.mark.parametrize("following", ["e:", "drive", "sdcards", "FloBig,e"])
def test_drives_without_an_equals_sign_are_refused(following, capsys):
    with pytest.raises(SystemExit):
        parse(["import", "--no-eject", following, "trip/day1"])
    assert f"--no-eject={following}" in capsys.readouterr().err


def test_a_role_the_command_doesnt_eject_is_an_error(labels):
    with pytest.raises(AlbumflowError, match="--no-eject=sdcards: this command doesn't eject that"):
        cli.no_eject(parse(["backup-new", "--no-eject=sdcards", "trip/day1"]))
    assert cli.no_eject(parse(["update-secondary", "--no-eject=secondary"])).names == ["secondary"]


def test_an_unknown_name_is_a_warning(labels):
    lines = []
    keep = cli.no_eject(parse(["import", "--no-eject=FloBig, Typo,x", "trip/day1"]), lines.append)
    assert keep.names == ["FloBig", "Typo", "x"]
    assert lines == ["Warning: --no-eject=Typo: no drive with that name is connected (yet)"]


@pytest.mark.parametrize("options, expected", [
    ([], ["card", "primary"]),
    (["--no-eject"], []),
    (["--no-eject=drive"], ["card"]),
    (["--no-eject=e"], ["primary"]),
])
def test_import_ejects_what_isnt_kept(labels, tmp_path, monkeypatch, options, expected):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    monkeypatch.setattr(backup, "find_backup_drive", lambda drive=None: PRIMARY)
    calls = []
    monkeypatch.setattr(card_import, "import_and_backup", lambda *args, **kwargs: calls.append(kwargs))
    args = parse(["import", "trip/day1", *options])
    args.topic, args.album = "trip", "day1"
    assert cli.cmd_import(args) == 0
    eject = calls[0]["eject"]
    assert [role for drive, role in ((CARD, "card"), (PRIMARY, "primary")) if eject(drive, role)] == expected

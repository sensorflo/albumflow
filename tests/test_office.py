"""albumflow on the office laptop: the drives only, none of the private laptop's folders, no cloud."""
import pytest

from albumflow import backup, card_import, cli, config, index


@pytest.fixture
def office(tmp_path, monkeypatch):
    nowhere = tmp_path / "not-on-this-computer"
    for name in ("HOME_FOLDER", "PICTURES_NEW", "PICTURES_NEW_OLD", "ORIGINALS", "CLOUD_PICTURES", "CLOUD_MY_DRIVE"):
        monkeypatch.setattr(config, name, nowhere / name)
    monkeypatch.delenv("ALBUMFLOW_ANNEX", raising=False)
    primary, secondary = tmp_path / "P", tmp_path / "S"
    for drive, marker in ((primary, config.BACKUP_MARKER), (secondary, config.SECONDARY_BACKUP_MARKER)):
        drive.mkdir()
        (drive / marker).write_text("")
    monkeypatch.setattr(backup, "drives", lambda: [primary, secondary])
    monkeypatch.setattr(card_import, "find_cards", lambda: [])
    yield primary, secondary
    assert not nowhere.exists(), "nothing written to the private laptop's folders"


def test_update_secondary_in_the_office(office):
    primary, secondary = office
    waiting = primary / config.BACKUP_DIR / "party" / "fest"
    waiting.mkdir(parents=True)
    (waiting / "1.ARW").write_bytes(b"one")
    assert cli.main(["update-secondary", "--no-eject"]) == 0
    assert (secondary / config.SECONDARY_DIR / "party" / "fest" / "1.ARW").is_file()
    assert index.on_secondary(primary, "party", "fest") == {"1.ARW": 3}


def test_purge_in_the_office(office, capsys):
    primary, _ = office
    (primary / config.COPIED_DIR / "party" / "fest").mkdir(parents=True)
    args = cli.build_parser().parse_args(["purge"])
    assert cli.cmd_purge(args, ask=lambda prompt: "") == 0  # lists, chooses none
    out = capsys.readouterr().out
    assert "primary:" in out and "secondary:" in out

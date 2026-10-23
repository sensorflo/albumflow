import pytest

from albumflow import cli, config


@pytest.fixture
def new(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path)
    for album in ("party/Pan Tau Okt 2026", "OnlyForOthers/Pan Tau Maerz 2026"):
        (tmp_path / album).mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("command", ["import", "import-only", "backup-raw", "sync-clocks"])
def test_album_commands_refuse_only_for_others(new, command, capsys):
    assert cli.main([command, "OnlyForOthers/Pan Tau Maerz 2026", "--dry-run"]) == 2
    assert "leftovers of finished albums" in capsys.readouterr().err


def test_only_for_others_is_not_offered(new):
    assert [(a.topic, a.name) for a in cli.album_candidates("pan tau")] == [("party", "Pan Tau Okt 2026")]
    assert cli.choose_album("pan tau maerz", create=False, candidates=cli.album_candidates("pan tau maerz"),
                            ask=lambda prompt: "1", out=lambda *_: None) == ("party", "Pan Tau Okt 2026")

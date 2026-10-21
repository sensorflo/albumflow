import os

import pytest

from albumflow import album_choice, config
from albumflow.cli import choose_album
from albumflow.files import AlbumflowError


@pytest.fixture
def new(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path)
    for i, (topic, album) in enumerate([
        ("party", "castle-of-dreams-2025"),
        ("party", "Secret Castle 2026"),  # most recent
        ("hollidays", "norway-2026"),
    ]):
        path = tmp_path / topic / album
        path.mkdir(parents=True)
        os.utime(path, (1_700_000_000 + i * 1000,) * 2)
    return tmp_path


def names(albums):
    return [str(a) for a in albums]


@pytest.mark.parametrize("query, expected", [
    ("party/secret", ("party", "secret")),
    ("party\\secret", ("party", "secret")),
    ("secret", (None, "secret")),
    ("", (None, "")),
])
def test_split_query(query, expected):
    assert album_choice.split_query(query) == expected


def test_excluded_albums_are_no_candidates(new):
    (new / "party" / "Secret Castle 2026 (lightroom)").mkdir()
    (new / "party" / "Secret Castle 2026 (Rejected)").mkdir()
    assert "party/Secret Castle 2026 (lightroom)" not in names(album_choice.albums())
    assert "party/Secret Castle 2026 (Rejected)" not in names(album_choice.albums())
    assert "party/Secret Castle 2026" in names(album_choice.albums())


def test_exact_ignores_case_and_separators(new):
    hits = album_choice.exact("party/secret-castle-2026", album_choice.albums())
    assert names(hits) == ["party/Secret Castle 2026"]


def test_words_match_and_recent_first(new):
    ranked = album_choice.rank("castle", album_choice.albums())
    assert names(ranked) == ["party/Secret Castle 2026", "party/castle-of-dreams-2025"]


def test_typo_still_matches(new):
    assert names(album_choice.rank("norwya", album_choice.albums()))[0] == "hollidays/norway-2026"


def test_empty_query_lists_recent(new):
    assert names(album_choice.rank("", album_choice.albums())) == [
        "hollidays/norway-2026", "party/Secret Castle 2026", "party/castle-of-dreams-2025"]


def test_choose_exact_needs_no_question(new):
    def ask(prompt):
        raise AssertionError("should not ask")
    assert choose_album("party/secret castle 2026", create=False, ask=ask, out=lambda *_: None) == \
        ("party", "Secret Castle 2026")


def test_choose_enter_takes_first(new):
    assert choose_album("castle", create=False, ask=lambda _: "", out=lambda *_: None) == \
        ("party", "Secret Castle 2026")


def test_choose_by_number(new):
    assert choose_album("castle", create=False, ask=lambda _: "2", out=lambda *_: None) == \
        ("party", "castle-of-dreams-2025")


def test_create_offers_similar_but_defaults_to_new(new):
    lines = []
    chosen = choose_album("party/castle-2027", create=True, ask=lambda _: "", out=lines.append)
    assert chosen == ("party", "castle-2027")
    assert any("create new album party/castle-2027" in line for line in lines)


def test_create_without_anything_similar_asks_nothing(new):
    def ask(prompt):
        raise AssertionError("should not ask")
    assert choose_album("party/zzzz-qqqq", create=True, ask=ask, out=lambda *_: None) == ("party", "zzzz-qqqq")


def test_no_match_without_create(new):
    with pytest.raises(AlbumflowError, match="add -c"):
        choose_album("zzzz-qqqq", create=False, ask=lambda _: "", out=lambda *_: None)


def test_invalid_answer(new):
    with pytest.raises(AlbumflowError, match="No album chosen"):
        choose_album("castle", create=False, ask=lambda _: "9", out=lambda *_: None)


def test_choice_is_repeated_back(new):
    lines = []
    choose_album("castle", create=False, ask=lambda _: "2", out=lines.append)
    assert lines[-1] == "Album: party/castle-of-dreams-2025"


def test_q_quits_the_choice(new):
    with pytest.raises(KeyboardInterrupt):
        choose_album("castle", create=False, ask=lambda _: "q", out=lambda *_: None)


def test_ctrl_c_ends_without_traceback(new, monkeypatch, capsys):
    from albumflow import cli
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    assert cli.main(["import-only", "castle"]) == 130
    assert capsys.readouterr().err.strip().endswith("Aborted.")


def test_no_arguments_shows_help(capsys):
    from albumflow import cli
    assert cli.main([]) == 0
    assert capsys.readouterr().out.startswith("usage: albumflow")


def test_topic_given_compares_only_names_within_it(new):
    # "party/zz-unknown 2026" must not match every party album just because of "party"
    assert names(album_choice.rank("party/zz-unknown 2026", album_choice.albums())) == []
    assert names(album_choice.rank("party/castle", album_choice.albums())) == [
        "party/Secret Castle 2026", "party/castle-of-dreams-2025"]
    assert names(album_choice.rank("hollidays/castle", album_choice.albums())) == []


def test_typo_in_topic_still_finds_the_album(new):
    assert names(album_choice.rank("prty/secret castle", album_choice.albums()))[0] == "party/Secret Castle 2026"


def test_create_new_album_in_topic_asks_nothing(new):
    def ask(prompt):
        raise AssertionError("should not ask")
    assert choose_album("hollidays/Madeira 2026", create=True, ask=ask, out=lambda *_: None) == \
        ("hollidays", "Madeira 2026")

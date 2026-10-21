import datetime
import random
import subprocess

import pytest
from PIL import Image

from albumflow import backup, card_import, clock, config
from albumflow.clock import Shot, plan, split_name
from albumflow.exiftool import ExifTool

BASE = datetime.datetime(2026, 8, 28, 12, 0, 0)


def shot(seconds: int, number: int, offset: int = 0, camera: str = "B") -> Shot:
    """A photo taken at BASE + seconds on the camera's own clock."""
    stem = f"{clock.format_time(BASE + datetime.timedelta(seconds=seconds))}-_DSC{number:04d}"
    return Shot(stem, camera=camera, offset=offset)


def planned_order_is_master_order(shots):
    ordered = sorted(shots, key=lambda s: s.key(planned=True))
    return all(a.master_time <= b.master_time for a, b in zip(ordered, ordered[1:]))


@pytest.mark.parametrize("name, expected", [
    ("20260828T120736-_DSC8045.ARW", (None, "20260828T120736-_DSC8045.ARW")),
    ("20260828T120948-20260828T120736-_DSC8045.ARW", ("20260828T120948", "20260828T120736-_DSC8045.ARW")),
])
def test_split_name(name, expected):
    assert split_name(name) == expected


def test_far_off_but_not_mixed_stays_untouched():
    shots = [shot(0, 1, camera="A"), shot(60, 2, camera="A"),
             shot(7200, 3, offset=3600)]  # an hour off, but taken after all of A's photos
    assert plan(shots) == []


def test_mixed_photo_is_corrected():
    a1, a2 = shot(0, 1, camera="A"), shot(100, 2, camera="A")
    b = shot(150, 3, offset=180)  # really taken at -30, i.e. before a1
    assert plan([a1, a2, b]) == [b]
    assert b.key(planned=True).startswith(clock.format_time(BASE - datetime.timedelta(seconds=30)))


def test_master_is_never_corrected():
    a = shot(100, 1, camera="A")
    b = shot(0, 2, offset=-300)  # really taken at +300, after a
    assert plan([a, b]) == [b]


def test_same_camera_photos_stay_in_order():
    # b1 is not mixed with a; correcting only b2 would put b2 before b1 -> b1 must follow.
    a = shot(130, 1, camera="A")
    b1, b2 = shot(120, 2, offset=180), shot(170, 3, offset=180)  # really at -60 and -10
    corrected = plan([a, b1, b2])
    assert set(corrected) == {b1, b2}
    assert planned_order_is_master_order([a, b1, b2])


def test_ties_within_a_second_count_as_in_order():
    a = shot(10, 1, camera="A")
    b = shot(12, 2, offset=2)  # same master time as a
    assert plan([a, b]) == []


def test_already_corrected_stays_corrected_with_current_measurement():
    b = shot(0, 1, offset=100)
    b.corrected = clock.format_time(BASE - datetime.timedelta(seconds=90))  # from an older measurement
    assert plan([b]) == [b]
    assert b.key(planned=True).startswith(clock.format_time(BASE - datetime.timedelta(seconds=100)))


@pytest.mark.parametrize("seed", range(30))
def test_random_albums_end_up_in_master_order(seed):
    rnd = random.Random(seed)
    offsets = {"A": 0, "B": rnd.randint(-900, 900), "C": rnd.randint(-900, 900)}
    shots = []
    for number in range(rnd.randint(5, 80)):
        camera = rnd.choice("ABC")
        shots.append(shot(rnd.randint(0, 3600), number, offset=offsets[camera], camera=camera))
    corrected = plan(shots)
    assert planned_order_is_master_order(shots)
    assert all(s.camera != "A" for s in corrected)


# --- with real files and exiftool ---

def make_photo(path, capture_time, model):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(path)
    subprocess.run([config.exiftool(), "-q", "-overwrite_original", f"-DateTimeOriginal={capture_time}",
                    f"-CreateDate={capture_time}", f"-Model={model}", str(path)], check=True)


@pytest.fixture
def album(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    monkeypatch.setattr(config, "MASTER_CAMERA", "MASTER")
    album = tmp_path / "new" / "party" / "fest"
    ssd = tmp_path / "ssd" / config.BACKUP_DIR / "fest"
    for name, t, model in [
        ("20260828T120000-_DSC0001.JPG", "2026:08:28 12:00:00", "MASTER"),  # the master's clock photo
        ("20260828T120100-_DSC0002.JPG", "2026:08:28 12:01:00", "MASTER"),
        ("20260828T120305-_DSC0101.JPG", "2026:08:28 12:03:05", "OTHER"),  # other's clock photo
        ("20260828T120200-_DSC0102.JPG", "2026:08:28 12:02:00", "OTHER"),  # really 11:59:55: mixed
    ]:
        make_photo(album / name, t, model)
        ssd.mkdir(parents=True, exist_ok=True)
        (ssd / name).write_bytes((album / name).read_bytes())
    (album / "20260828T120200-_DSC0102.xmp").write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    return album, ssd


def test_measure_and_apply(album):
    album, ssd = album
    with ExifTool() as exif:
        clock.measure(album, album / "20260828T120000-_DSC0001.JPG", datetime.time(12, 0, 0), exif)
        camera, offset = clock.measure(album, album / "20260828T120305-_DSC0101.JPG", datetime.time(12, 1, 0), exif)
    assert (camera, offset) == ("OTHER", 125)  # 2:05 ahead of the clock, and of the master
    assert clock.apply(album, ssd, log=lambda *_: None) == 1

    corrected = album / "20260828T115955-20260828T120200-_DSC0102.JPG"
    assert corrected.exists() and not (album / "20260828T120200-_DSC0102.JPG").exists()
    assert (album / "20260828T115955-20260828T120200-_DSC0102.xmp").exists()  # sidecar follows
    with ExifTool() as exif:
        assert exif.capture_time(corrected) == "20260828T115955"
    assert sorted(p.name for p in clock.photo_files(album)) == [  # in the order they were taken
        "20260828T115955-20260828T120200-_DSC0102.JPG", "20260828T120000-_DSC0001.JPG",
        "20260828T120100-_DSC0002.JPG", "20260828T120305-_DSC0101.JPG"]
    assert (ssd / "20260828T120200-_DSC0102.JPG").exists()  # the original on the backup is untouched

    # repeating changes nothing; the backup sees the corrected photo as backed up
    assert clock.apply(album, ssd, log=lambda *_: None) == 0
    assert backup.missing_on_backup(album, ssd) == []


def test_waits_until_original_is_on_backup(album):
    album, ssd = album
    (ssd / "20260828T120200-_DSC0102.JPG").unlink()
    with ExifTool() as exif:
        clock.measure(album, album / "20260828T120000-_DSC0001.JPG", datetime.time(12, 0, 0), exif)
        clock.measure(album, album / "20260828T120305-_DSC0101.JPG", datetime.time(12, 1, 0), exif)
    lines = []
    assert clock.apply(album, ssd, log=lines.append) == 0
    assert any("1 photos wait" in line for line in lines)
    assert (album / "20260828T120200-_DSC0102.JPG").exists()


def test_nothing_without_master_measurement(album):
    album, ssd = album
    with ExifTool() as exif:
        clock.measure(album, album / "20260828T120305-_DSC0101.JPG", datetime.time(12, 1, 0), exif)
    lines = []
    assert clock.apply(album, ssd, log=lines.append) == 0
    assert any(line.startswith("Clock correction paused") and "MASTER (2 photos)" in line for line in lines)


def test_reimport_skips_photo_corrected_since(album, tmp_path):
    album, ssd = album
    with ExifTool() as exif:
        clock.measure(album, album / "20260828T120000-_DSC0001.JPG", datetime.time(12, 0, 0), exif)
        clock.measure(album, album / "20260828T120305-_DSC0101.JPG", datetime.time(12, 1, 0), exif)
    clock.apply(album, ssd, log=lambda *_: None)
    card = tmp_path / "card"
    make_photo(card / "DCIM" / "100MSDCF" / "_DSC0102.JPG", "2026:08:28 12:02:00", "OTHER")
    card_import.import_only("party", "fest", card=card, log=lambda *_: None)
    assert not (album / "20260828T120200-_DSC0102.JPG").exists()  # not imported a second time


def readings(album, exif, *pairs):
    for name, t in pairs:
        clock.measure(album, album / name, datetime.time.fromisoformat(t), exif)


def test_new_camera_without_reading_pauses_corrections(album):
    album, ssd = album
    make_photo(album / "20260828T120230-_DSC0201.JPG", "2026:08:28 12:02:30", "THIRD")
    (ssd / "20260828T120230-_DSC0201.JPG").write_bytes((album / "20260828T120230-_DSC0201.JPG").read_bytes())
    with ExifTool() as exif:
        readings(album, exif, ("20260828T120000-_DSC0001.JPG", "12:00:00"), ("20260828T120305-_DSC0101.JPG", "12:01:00"))
    lines = []
    assert clock.apply(album, ssd, log=lines.append) == 0
    assert any("paused" in line and "THIRD (1 photos)" in line for line in lines)
    assert any('sync-clocks -r' in line and '"party/fest"' in line for line in lines)

    state = clock.AlbumState(album)
    state.assumed.append("THIRD")
    state.save()
    # _DSC0102 (master time 11:59:55), and now also the other camera's clock photo
    # _DSC0101 (camera 12:03:05, master 12:01:00), which came after THIRD's 12:02:30
    assert clock.apply(album, ssd, log=lambda *_: None) == 2


def test_import_records_cameras(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PICTURES_NEW", tmp_path / "new")
    (tmp_path / "new" / "party").mkdir(parents=True)
    card = tmp_path / "card"
    make_photo(card / "DCIM" / "100MSDCF" / "_DSC0001.JPG", "2026:08:28 12:00:00", "MASTER")
    make_photo(card / "DCIM" / "100MSDCF" / "_DSC0101.JPG", "2026:08:28 12:00:01", "OTHER")
    card_import.import_only("party", "fest", card=card, create_album=True, log=lambda *_: None)
    state = clock.AlbumState(tmp_path / "new" / "party" / "fest")
    assert state.cameras == {"20260828T120000-_DSC0001": "MASTER", "20260828T120001-_DSC0101": "OTHER"}


def test_sync_clocks_command(album, monkeypatch, capsys):
    from albumflow import cli
    album, ssd = album
    monkeypatch.setattr(backup, "find_backup_drive", lambda drive=None: ssd.parent.parent)
    monkeypatch.setattr("builtins.input", lambda: "y")
    assert cli.main(["sc", "-r", "_DSC0001=12:00:00", "-r", "_DSC0101=12:01:00", "party/fest"]) == 0
    err = capsys.readouterr().err
    assert "OTHER: +2:05 relative to the master, +2:05 relative to true time" in err
    assert err.index("  MASTER (master): clock photo") < err.index("  OTHER: clock photo")  # master listed first
    assert "corrected 1 photos (1 of OTHER)" in err
    assert (album / "20260828T115955-20260828T120200-_DSC0102.JPG").exists()


def test_sync_clocks_asks_for_readings(album, monkeypatch, capsys):
    from albumflow import cli
    album, ssd = album
    monkeypatch.setattr(backup, "find_backup_drive", lambda drive=None: None)
    # Enter takes the default (the first photo); the hour is pre-typed, so minutes and seconds suffice
    answers = iter(["", "00 00", "_DSC0101", "01:00", "-"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    assert cli.main(["sc", "party/fest"]) == 0
    assert set(clock.AlbumState(album).clock) == {"MASTER", "OTHER"}
    assert "The backup drive is not connected" in capsys.readouterr().err


@pytest.mark.parametrize("text, expected", [
    ("09:28:54", datetime.time(9, 28, 54)), ("09 28 54", datetime.time(9, 28, 54)),
    ("9.28.54", datetime.time(9, 28, 54)), ("09 28", datetime.time(9, 28)), ("092854", datetime.time(9, 28, 54)),
    ("09  28 : 54", datetime.time(9, 28, 54)),
])
def test_parse_clock_time(text, expected):
    from albumflow import cli
    assert cli.parse_clock_time(text) == expected


@pytest.mark.parametrize("text", ["25 00 00", "09", "abc", "09 28 54 1"])
def test_parse_clock_time_rejects(text):
    from albumflow import cli
    from albumflow.files import AlbumflowError
    with pytest.raises(AlbumflowError):
        cli.parse_clock_time(text)


def test_default_photo_moves_on_after_each_reading(album, monkeypatch):
    from albumflow import cli
    album, ssd = album
    monkeypatch.setattr(backup, "find_backup_drive", lambda drive=None: None)
    prompts = []

    def fake_input():
        prompts.append(len(prompts))
        return ["", "00 00", "", "01 00", "-"][len(prompts) - 1]  # takes both defaults

    monkeypatch.setattr("builtins.input", fake_input)
    cli.main(["sc", "party/fest"])
    # both defaults are the master's photos: the second reading (the next photo) replaced the first
    assert clock.AlbumState(album).clock["MASTER"]["photo"] == "20260828T120100-_DSC0002.JPG"


def test_readings_from_another_album(album, monkeypatch, capsys):
    from albumflow import cli
    album, ssd = album
    other = album.parent / "fest OnlyForMe"
    other.mkdir()
    with ExifTool() as exif:
        readings(album, exif, ("20260828T120000-_DSC0001.JPG", "12:00:00"))
    state = clock.AlbumState(other)
    state.clock["OTHER"] = {"offset": 7, "photo": "own.JPG", "clock": "10:00:00"}
    state.save()

    taken, kept = clock.copy_readings(album, other, "party/fest")
    assert taken == ["MASTER"] and kept == []
    state = clock.AlbumState(other)
    assert state.clock["MASTER"]["from"] == "party/fest"
    assert state.clock["OTHER"]["offset"] == 7  # its own reading stays
    assert any("(from party/fest)" in line for line in clock.describe(state))


def test_readings_from_option_with_loose_album_name(album, monkeypatch, capsys):
    from albumflow import cli
    album, ssd = album
    other = album.parent / "fest OnlyForMe"
    other.mkdir()
    with ExifTool() as exif:
        readings(album, exif, ("20260828T120000-_DSC0001.JPG", "12:00:00"))
    monkeypatch.setattr(backup, "find_backup_drive", lambda drive=None: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")  # "fest" is ambiguous: pick from the list
    assert cli.main(["sc", "party/fest onlyforme", "--readings-from", "fest"]) == 0
    assert clock.AlbumState(other).clock["MASTER"]["from"] == "party/fest"
    assert "taken over from party/fest" in capsys.readouterr().err


def test_readings_from_album_without_readings(album):
    album, ssd = album
    other = album.parent / "empty"
    other.mkdir()
    from albumflow.files import AlbumflowError
    with pytest.raises(AlbumflowError, match="has no clock readings"):
        clock.copy_readings(other, album, "party/empty")

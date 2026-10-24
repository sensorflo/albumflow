from albumflow import backup, config, gdrive


class FakeDrive:
    """My Drive with one folder Backup/pictures-new/trip/day1, holding the given files."""

    def __init__(self, files):
        self.files_in_day1 = dict(files)  # name -> size
        self.deleted = []

    def folder(self, parts):
        return "day1-id" if parts == ("Backup", "pictures-new", "trip", "day1") else None

    def files(self, folder):
        assert folder == "day1-id"
        return {name: (f"id-{name}", size) for name, size in self.files_in_day1.items()}

    def delete(self, file_id):
        self.deleted.append(file_id)


def cloud_album(tmp_path, monkeypatch, files):
    my_drive = tmp_path / "My Drive"
    monkeypatch.setattr(config, "CLOUD_MY_DRIVE", my_drive)
    monkeypatch.setattr(config, "CLOUD_PICTURES", my_drive / "Backup" / "pictures-new")
    folder = config.CLOUD_PICTURES / "trip" / "day1"
    folder.mkdir(parents=True)
    for name, content in files.items():
        (folder / name).write_bytes(content)
    return folder


def test_deletes_permanently_what_drive_has_with_the_same_size(tmp_path, monkeypatch):
    folder = cloud_album(tmp_path, monkeypatch, {"1.ARW": b"one", "2.ARW": b"two", "3.ARW": b"three"})
    drive = FakeDrive({"1.ARW": 3, "2.ARW": 99})  # 2.ARW: another size; 3.ARW: not uploaded yet
    monkeypatch.setattr(gdrive, "drive", lambda: drive)
    lines = []
    backup.delete_from_cloud(sorted(folder.iterdir()), "rejected", False, lines.append)
    assert drive.deleted == ["id-1.ARW"]
    assert not folder.exists()  # all deleted locally too, the empty folder removed
    assert "2 of them went to Drive's trash: Drive didn't have them (yet)." in lines


def test_without_login_deletes_locally_and_says_so(tmp_path, monkeypatch):
    folder = cloud_album(tmp_path, monkeypatch, {"1.ARW": b"one"})
    lines = []
    backup.delete_from_cloud([folder / "1.ARW"], "rejected", False, lines.append)
    assert not (folder / "1.ARW").exists()
    assert any("Not logged in to Google Drive" in line for line in lines)


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    folder = cloud_album(tmp_path, monkeypatch, {"1.ARW": b"one"})
    drive = FakeDrive({"1.ARW": 3})
    monkeypatch.setattr(gdrive, "drive", lambda: drive)
    backup.delete_from_cloud([folder / "1.ARW"], "rejected", True, lambda *_: None)
    assert drive.deleted == [] and (folder / "1.ARW").exists()


def test_quote():
    assert gdrive._quote("Mike's 'best'") == r"'Mike\'s \'best\''"

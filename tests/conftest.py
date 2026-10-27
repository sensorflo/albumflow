import pytest

from albumflow import config


@pytest.fixture(autouse=True)
def no_real_cloud(tmp_path, monkeypatch):
    """No test looks at or writes to the real cloud folder (G: is mounted on the laptop)."""
    monkeypatch.setattr(config, "CLOUD_PICTURES", tmp_path / "no-cloud" / "pictures-new")


@pytest.fixture(autouse=True)
def no_google_drive(tmp_path, monkeypatch):
    """No test uses the real Google Drive: as if not logged in (tests may set a fake client)."""
    from albumflow import gdrive
    monkeypatch.setattr(config, "CLOUD_MY_DRIVE", tmp_path / "no-cloud")
    monkeypatch.setattr(gdrive, "drive", lambda: None)


@pytest.fixture(autouse=True)
def no_self_copy(monkeypatch):
    """No test writes albumflow.pyz to a drive (test_selfcopy calls selfcopy.write directly)."""
    from albumflow import selfcopy
    monkeypatch.setattr(selfcopy, "write_to_connected", lambda: None)


@pytest.fixture(autouse=True)
def no_real_emdb(tmp_path, monkeypatch):
    """No test reads or writes the real EMDB database or its backups."""
    monkeypatch.setattr(config, "EMDB_DATABASE", tmp_path / "no-emdb" / "emdb.dat")
    monkeypatch.setattr(config, "EMDB_BACKUPS", tmp_path / "no-emdb" / "Backups")
    monkeypatch.setattr(config, "MEDIAINFO_CACHE", tmp_path / "no-emdb" / "mediainfo.json")

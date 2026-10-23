import pytest

from albumflow import config


@pytest.fixture(autouse=True)
def no_real_cloud(tmp_path, monkeypatch):
    """No test looks at or writes to the real cloud folder (G: is mounted on the laptop)."""
    monkeypatch.setattr(config, "CLOUD_PICTURES", tmp_path / "no-cloud" / "pictures-new")

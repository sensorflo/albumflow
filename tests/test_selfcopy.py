import stat
import subprocess
import sys
import zipfile

from albumflow import config, selfcopy


def test_writes_a_read_only_zipapp_only_when_changed(tmp_path):
    assert selfcopy.write(tmp_path)
    target = tmp_path / config.SELF_COPY
    assert not target.stat().st_mode & stat.S_IWRITE
    names = zipfile.ZipFile(target).namelist()
    assert "__main__.py" in names and "albumflow/cli.py" in names and "albumflow/selfcopy.py" in names
    assert not selfcopy.write(tmp_path)  # unchanged: not written again


def test_runs_with_python_alone(tmp_path):
    """As on the office laptop: no installed packages (-S: no site-packages), no Google libraries."""
    selfcopy.write(tmp_path)
    result = subprocess.run([sys.executable, "-S", str(tmp_path / config.SELF_COPY), "-h"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "update-secondary" in result.stdout

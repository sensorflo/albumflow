"""Locations outside the repo: credentials, tokens and state."""

import os
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("ALBUMFLOW_HOME") or Path(os.environ["APPDATA"]) / "albumflow"
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir(target: str) -> Path:
    path = config_dir() / "state" / target
    path.mkdir(parents=True, exist_ok=True)
    return path

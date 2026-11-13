"""Personal, local-first bilingual literature reader."""

from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
SHARED_BUILD = bool(getattr(sys, "frozen", False) or os.environ.get("READER_SHARED_BUILD") == "1")


def default_data_dir():
    if not SHARED_BUILD:
        return ROOT / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PaperReader" / "data"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "PaperReader" / "data"


DATA = Path(os.environ.get("READER_DATA_DIR") or default_data_dir()).resolve()
VERSION = "0.6.15"

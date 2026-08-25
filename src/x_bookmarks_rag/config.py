"""Paths, URLs, and environment configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The session file holds a live X login. It stays outside the repository so a
# stray `git add -f` can never commit it.
CONFIG_DIR = Path(
    os.environ.get("XBM_CONFIG_DIR", Path.home() / ".config" / "x-bookmarks")
)
STATE_PATH = CONFIG_DIR / "state.json"

DATA_DIR = Path(os.environ.get("XBM_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "bookmarks.db"

BOOKMARKS_URL = "https://x.com/i/bookmarks"
LOGIN_URL = "https://x.com/login"

# Set in .env. Phase 2 uses it; phase 1 only reports whether it is present.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Pacing for the capture scroll loop, in milliseconds.
SCROLL_DELAY_MIN_MS = 800
SCROLL_DELAY_MAX_MS = 2000
RESPONSE_TIMEOUT_MS = 20_000


def ensure_dirs() -> None:
    """Create the data and config directories with safe permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    for directory in (DATA_DIR, RAW_DIR, MEDIA_DIR):
        directory.mkdir(parents=True, exist_ok=True)

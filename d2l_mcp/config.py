"""all the knobs in one place — everything can be overridden w/ env vars"""
import os
from pathlib import Path

# western's owl = brightspace hosted at this url. diff school? set D2L_BASE_URL
BASE_URL = os.environ.get("D2L_BASE_URL", "https://westernu.brightspace.com").rstrip("/")

# root folder for everything this tool saves (cookies, browser profile, synced course files)
DATA_DIR = Path(os.environ.get("D2L_DATA_DIR", Path.home() / ".d2l-mcp")).expanduser()

COOKIE_FILE = DATA_DIR / "cookies.json"        # session cookies grabbed after u log in
BROWSER_PROFILE = DATA_DIR / "browser-profile" # playwright keeps the browser profile here so sso remembers u
SYNC_DIR = DATA_DIR / "courses"                # downloaded slides/pdfs + extracted .txt live here

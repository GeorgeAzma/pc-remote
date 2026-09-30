"""Where things live.

From source: everything is in the source folder, as before. Installed (a
PyInstaller build): the program is next to the .exe (read-only under
Program Files), its bundled files (web/, version.txt) are in the bundle,
and what it writes (certificates, the log) goes to %LOCALAPPDATA%\\PC Remote."""
import os
import sys

FROZEN = getattr(sys, "frozen", False)
APP = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", APP)
DATA = os.environ.get("PC_REMOTE_DATA") or (  # (the override is for testing)
    os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PC Remote") if FROZEN else APP)
os.makedirs(DATA, exist_ok=True)


def version() -> str | None:
    """The release this build is (written by packaging/build.py), if any."""
    try:
        with open(os.path.join(RES, "version.txt"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None

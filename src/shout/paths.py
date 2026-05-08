"""Filesystem locations Shout reads/writes."""

from __future__ import annotations

import os
from pathlib import Path


# Per Apple convention, app data goes under ~/Library/Application Support.
# We keep the socket here too (rather than /tmp) so a single user's daemon
# is reachable across reboots and not exposed to other users.
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Shout"
SOCKET_PATH = APP_SUPPORT / "daemon.sock"
LOG_PATH = APP_SUPPORT / "daemon.log"


def ensure_app_support() -> Path:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    return APP_SUPPORT


def hammerspoon_config_dir() -> Path:
    return Path(os.path.expanduser("~/.hammerspoon"))


def karabiner_complex_mods_dir() -> Path:
    return Path(
        os.path.expanduser("~/.config/karabiner/assets/complex_modifications")
    )


def launch_agent_path() -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents/com.greg.shout.plist"))

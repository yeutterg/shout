"""Tiny JSON-backed user config under Application Support.

We keep a single file at ~/Library/Application Support/Shout/config.json
with a flat dict of preferences. Read at session start, written on
change. Reads return {} on missing/corrupt files so a deleted config
just falls back to defaults rather than crashing the daemon.

Currently used keys:
  - input_device: str | null   sounddevice device name; null = system default
"""

from __future__ import annotations

import json
import logging

from . import paths

log = logging.getLogger("shout.config")


def _path():
    return paths.APP_SUPPORT / "config.json"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("config file unreadable; falling back to defaults")
        return {}


def save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


def get_input_device() -> str | None:
    val = load().get("input_device")
    return val if isinstance(val, str) and val else None


def set_input_device(name: str | None) -> None:
    data = load()
    if name is None:
        data.pop("input_device", None)
    else:
        data["input_device"] = name
    save(data)


def get_model() -> str | None:
    """Override the default Parakeet model via config.json {"model": "..."}.

    Common values:
      - mlx-community/parakeet-tdt-0.6b-v2 — English-only (default)
      - mlx-community/parakeet-tdt-0.6b-v3 — 25 European languages, auto-detect
    """
    val = load().get("model")
    return val if isinstance(val, str) and val else None

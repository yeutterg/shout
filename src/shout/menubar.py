"""Menu bar icon with a Microphone picker and a Quit item.

Lives next to the clock. Click reveals:
  Microphone (header)
  System default (NAME)
  ---
  <list of input devices>
  ---
  Quit Shout

Microphone selection writes to ~/Library/Application Support/Shout/
config.json; the next PTT session reads it. Quit calls back into the
daemon to shut it down (os._exit(0)), so brew services treats it as a
clean exit and does NOT restart.

Design choice: this is in the menu bar rather than on the overlay
because the overlay is a non-activating NSPanel — it intentionally
cannot accept keyboard focus (so CGEvent typing passes through to the
user's app), and it is only visible during a session. A menu bar item
is the standard macOS pattern for an always-reachable settings affordance,
and it's the same approach Wispr Flow takes.

Threading: must be created on the main thread (AppKit run loop owner).
The daemon's Daemon.run() does this once at startup.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import objc
import sounddevice as sd
from AppKit import (
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject

from . import config

log = logging.getLogger("shout.menubar")


class _MenuController(NSObject):
    """ObjC delegate for menu actions (mic select + quit)."""

    def initWithMenuBar_(self, menubar):
        self = objc.super(_MenuController, self).init()
        if self is None:
            return None
        self._menubar = menubar
        return self

    def selectMic_(self, sender):
        name = sender.representedObject()
        if name == "__default__":
            config.set_input_device(None)
            log.info("input device → system default")
        else:
            config.set_input_device(name)
            log.info("input device → %s", name)
        self._menubar.rebuild()

    def quitShout_(self, sender):
        log.info("quit requested from menu bar")
        cb = self._menubar.on_quit
        if cb is not None:
            cb()

    # Expose under the @selector(selectMic:) / @selector(quitShout:) names.
    selectMic_ = objc.selector(selectMic_, signature=b"v@:@")
    quitShout_ = objc.selector(quitShout_, signature=b"v@:@")


class MenuBar:
    """Holds the NSStatusItem and refreshes its menu on demand."""

    def __init__(self, on_quit: Optional[Callable[[], None]] = None) -> None:
        self.on_quit = on_quit

        bar = NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        # An emoji is the cheapest icon path for v0; an SF Symbol via
        # NSImage.imageWithSystemSymbolName is a v1 polish swap.
        self._item.button().setTitle_("●")
        self._item.button().setToolTip_("Shout")

        self._controller = _MenuController.alloc().initWithMenuBar_(self)
        self.rebuild()

    def rebuild(self) -> None:
        menu = NSMenu.alloc().init()

        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Microphone", None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)

        current = config.get_input_device()
        default_name = _default_input_name()
        default_title = (
            f"System default ({default_name})"
            if default_name else "System default"
        )

        default_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            default_title, "selectMic:", ""
        )
        default_item.setTarget_(self._controller)
        default_item.setRepresentedObject_("__default__")
        default_item.setState_(
            NSControlStateValueOn if current is None else NSControlStateValueOff
        )
        menu.addItem_(default_item)

        try:
            devices = sd.query_devices()
        except Exception:
            log.exception("sounddevice.query_devices() failed")
            devices = []

        seen: set[str] = set()
        for d in devices:
            if int(d.get("max_input_channels", 0)) < 1:
                continue
            name = d.get("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                name, "selectMic:", ""
            )
            item.setTarget_(self._controller)
            item.setRepresentedObject_(name)
            item.setState_(
                NSControlStateValueOn if current == name
                else NSControlStateValueOff
            )
            menu.addItem_(item)

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Shout", "quitShout:", "q"
        )
        quit_item.setTarget_(self._controller)
        menu.addItem_(quit_item)

        self._item.setMenu_(menu)


def _default_input_name() -> str | None:
    """Resolve sounddevice's current default input device to a human name.

    Falls back to None on failure so the menu still renders (with a
    plain 'System default' label) if the host audio API is unhappy."""
    try:
        info = sd.query_devices(kind="input")
    except Exception:
        return None
    if isinstance(info, dict):
        name = info.get("name")
        return name if isinstance(name, str) and name else None
    return None

"""Menu bar icon with a Microphone picker.

Lives next to the clock. Click reveals a submenu listing every input
device sounddevice sees on this Mac, plus a "System default" entry.
Selecting a device writes ~/Library/Application Support/Shout/config.json
and the next PTT session uses that device.

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


class _MicMenuController(NSObject):
    """ObjC delegate for the Microphone submenu actions."""

    def initWithMenuBar_(self, menubar):
        self = objc.super(_MicMenuController, self).init()
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

    # Force the bridge to expose this as the @selector(selectMic:) name.
    selectMic_ = objc.selector(selectMic_, signature=b"v@:@")


class MenuBar:
    """Holds the NSStatusItem and refreshes its menu on demand."""

    def __init__(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        # An emoji is the cheapest icon path for v0; an SF Symbol via
        # NSImage.imageWithSystemSymbolName is a v1 polish swap.
        self._item.button().setTitle_("●")
        self._item.button().setToolTip_("Shout")

        self._controller = _MicMenuController.alloc().initWithMenuBar_(self)
        self.rebuild()

    def rebuild(self) -> None:
        menu = NSMenu.alloc().init()

        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Microphone", None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)

        current = config.get_input_device()

        default_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "System default", "selectMic:", ""
        )
        default_item.setTarget_(self._controller)
        default_item.setRepresentedObject_("__default__")
        default_item.setState_(
            NSControlStateValueOn if current is None else NSControlStateValueOff
        )
        menu.addItem_(default_item)

        menu.addItem_(NSMenuItem.separatorItem())

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

        self._item.setMenu_(menu)

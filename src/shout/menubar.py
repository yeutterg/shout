"""Menu bar icon: microphone + model + language pickers + quit.

Lives next to the clock. Click reveals:
  Microphone
    System default (NAME)
    <list of input devices>
  Model
    <known Parakeet variants, ✓ on the active one>
  Language
    English
    Multilingual (auto-detect)            ← grayed when current model is English-only
  ─────
  Quit Shout                               ⌘Q

Microphone selection writes to ~/Library/Application Support/Shout/
config.json; the next PTT session reads it.

Model and Language selections also write to config.json, then trigger
a daemon restart (exit code 1, so brew services relaunches us). We
have to restart because the model is loaded once at startup and held
in MLX memory; hot-swapping is doable but not worth the complexity for
a v0 feature that fires maybe once per power-user-onboarding session.

Quit calls back into the daemon to shut it down (exit 0 = clean), so
brew services treats it as a clean exit and does NOT auto-restart.

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


# Known Parakeet variants the menu can switch between. (id, display, is_multilingual)
# Add more entries here as new models become wrappable by parakeet-mlx.
KNOWN_MODELS: list[tuple[str, str, bool]] = [
    (
        "mlx-community/parakeet-tdt-0.6b-v2",
        "Parakeet TDT 0.6B v2 (English)",
        False,
    ),
    (
        "mlx-community/parakeet-tdt-0.6b-v3",
        "Parakeet TDT 0.6B v3 (Multilingual)",
        True,
    ),
]

# The default the daemon falls back to when config.get_model() returns None.
# Must agree with daemon.DEFAULT_MODEL.
_DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"


class _MenuController(NSObject):
    """ObjC delegate for menu actions (mic select + model select +
    language select + quit)."""

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

    def selectModel_(self, sender):
        model_id = sender.representedObject()
        config.set_model(model_id)
        log.info("model → %s (will restart daemon)", model_id)
        self._menubar.rebuild()
        self._menubar.request_restart()

    def selectLanguage_(self, sender):
        # Language menu maps to a model: "english" → first English-only
        # known model; "multilingual" → first multilingual known model.
        kind = sender.representedObject()
        target = next(
            (m for m in KNOWN_MODELS if m[2] == (kind == "multilingual")),
            None,
        )
        if target is None:
            log.warning("no known model for language=%s", kind)
            return
        config.set_model(target[0])
        log.info("language → %s (model %s, restarting)", kind, target[0])
        self._menubar.rebuild()
        self._menubar.request_restart()

    def quitShout_(self, sender):
        log.info("quit requested from menu bar")
        cb = self._menubar.on_quit
        if cb is not None:
            cb()

    selectMic_ = objc.selector(selectMic_, signature=b"v@:@")
    selectModel_ = objc.selector(selectModel_, signature=b"v@:@")
    selectLanguage_ = objc.selector(selectLanguage_, signature=b"v@:@")
    quitShout_ = objc.selector(quitShout_, signature=b"v@:@")


class MenuBar:
    """Holds the NSStatusItem and refreshes its menu on demand."""

    def __init__(
        self,
        on_quit: Optional[Callable[[], None]] = None,
        on_restart: Optional[Callable[[], None]] = None,
    ) -> None:
        self.on_quit = on_quit
        self.on_restart = on_restart

        bar = NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self._item.button().setTitle_("●")
        self._item.button().setToolTip_("Shout")

        self._controller = _MenuController.alloc().initWithMenuBar_(self)
        self.rebuild()

    def request_restart(self) -> None:
        if self.on_restart is not None:
            self.on_restart()

    def rebuild(self) -> None:
        menu = NSMenu.alloc().init()
        self._build_microphone_menu(menu)
        self._build_model_menu(menu)
        self._build_language_menu(menu)
        menu.addItem_(NSMenuItem.separatorItem())
        self._build_quit_item(menu)
        self._item.setMenu_(menu)

    # ---- submenu builders ----

    def _build_microphone_menu(self, menu) -> None:
        mic_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Microphone", None, ""
        )
        sub = NSMenu.alloc().init()
        mic_root.setSubmenu_(sub)
        menu.addItem_(mic_root)

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
        sub.addItem_(default_item)

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
            sub.addItem_(item)

    def _build_model_menu(self, menu) -> None:
        root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Model", None, ""
        )
        sub = NSMenu.alloc().init()
        root.setSubmenu_(sub)
        menu.addItem_(root)

        current = config.get_model() or _DEFAULT_MODEL
        for model_id, display, _ in KNOWN_MODELS:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                display, "selectModel:", ""
            )
            item.setTarget_(self._controller)
            item.setRepresentedObject_(model_id)
            item.setState_(
                NSControlStateValueOn if model_id == current
                else NSControlStateValueOff
            )
            sub.addItem_(item)

    def _build_language_menu(self, menu) -> None:
        root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Language", None, ""
        )
        sub = NSMenu.alloc().init()
        root.setSubmenu_(sub)
        menu.addItem_(root)

        current_id = config.get_model() or _DEFAULT_MODEL
        current_is_multi = next(
            (m[2] for m in KNOWN_MODELS if m[0] == current_id), False
        )
        any_multi_available = any(m[2] for m in KNOWN_MODELS)

        en = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "English", "selectLanguage:", ""
        )
        en.setTarget_(self._controller)
        en.setRepresentedObject_("english")
        en.setState_(
            NSControlStateValueOn if not current_is_multi
            else NSControlStateValueOff
        )
        sub.addItem_(en)

        # Per user request: gray out Multilingual when the *current*
        # selected model is single-language. Switching to multilingual
        # is then a two-step flow: pick a multilingual model first,
        # then re-open this menu.
        multi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Multilingual (auto-detect)", "selectLanguage:", ""
        )
        multi.setTarget_(self._controller)
        multi.setRepresentedObject_("multilingual")
        multi.setState_(
            NSControlStateValueOn if current_is_multi
            else NSControlStateValueOff
        )
        # Enable iff we have any multilingual model available. Per the
        # docstring at the top of this module, the visual ✓ tracks the
        # current selection; clicking switches the model. Earlier code
        # had `any_multi_available AND current_is_multi`, which had the
        # bug that Multilingual was permanently grayed when the active
        # model was English-only — defeating the whole point of the
        # menu entry.
        multi.setEnabled_(any_multi_available)
        sub.addItem_(multi)

    def _build_quit_item(self, menu) -> None:
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Shout", "quitShout:", "q"
        )
        quit_item.setTarget_(self._controller)
        menu.addItem_(quit_item)


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

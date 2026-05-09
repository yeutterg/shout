"""Floating tentative-token overlay.

Uses an AppKit `NSPanel` (via pyobjc) configured as a non-activating,
borderless, always-on-top floating panel — the same class Spotlight,
Alfred, and Wispr Flow use. Critically, this class of NSWindow never
becomes the *key* window when shown, so the user's previously-focused
text field stays focused and CGEvent text injection lands there.

We tried Tk's `::tk::unsupported::MacWindowStyle` to get the same
behavior on a Toplevel, but on macOS 12+ Tk's borderless windows still
take focus on `deiconify()`. NSPanel is the documented, stable path.

The strip shows a rolling window of the most-recent finalized text
plus the current draft, in two colors.

Threading: every public method is safe to call from any thread. State
mutations and AppKit calls dispatch to the main thread via
`NSOperationQueue.mainQueue()`.
"""

from __future__ import annotations

import logging

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSPanel,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSOperationQueue


_BG_R, _BG_G, _BG_B, _BG_A = 0.10, 0.10, 0.10, 0.92
_FG_HISTORY = (0.87, 0.87, 0.87, 1.00)
_FG_DRAFT = (0.62, 0.65, 0.69, 1.00)
_FONT_SIZE = 14.0
_HEIGHT = 112.0
_WIDTH = 760.0
_BOTTOM_MARGIN = 80.0
_PADDING = 18.0
_HISTORY_CHARS = 240

log = logging.getLogger("shout.overlay")


def _on_main(fn):
    """Schedule fn() on the main thread; safe from any thread."""
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


class Overlay:
    """Non-activating floating NSPanel showing recent transcript text."""

    def __init__(self) -> None:
        # NSApplication must be initialised before we create any panel.
        # Daemon.run() does this once at startup; we re-do it here as a
        # no-op safeguard.
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - _WIDTH) / 2.0
        y = _BOTTOM_MARGIN
        frame = NSMakeRect(x, y, _WIDTH, _HEIGHT)

        style_mask = (
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        )
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style_mask, NSBackingStoreBuffered, False
        )
        panel.setOpaque_(False)
        panel.setHasShadow_(False)
        panel.setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(_BG_R, _BG_G, _BG_B, _BG_A)
        )
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
        )

        # Two stacked text fields: brighter "history", dimmer "draft".
        # We keep them as siblings inside the content view, both with
        # frame-based layout (auto-resizing turned off so explicit
        # frame updates take effect on every render).
        content = panel.contentView()
        inner_w = _WIDTH - 2.0 * _PADDING
        inner_h = _HEIGHT - 2.0 * _PADDING

        history_field = _make_label(
            NSMakeRect(_PADDING, _PADDING, inner_w, inner_h),
            _FG_HISTORY,
        )
        content.addSubview_(history_field)

        draft_field = _make_label(
            NSMakeRect(_PADDING, _PADDING, inner_w, inner_h),
            _FG_DRAFT,
        )
        content.addSubview_(draft_field)

        self._panel = panel
        self._history_field = history_field
        self._draft_field = draft_field
        self._history = ""
        self._draft = ""

    # ---- public API (safe from any thread) ----

    def show(self) -> None:
        # orderFrontRegardless does NOT make the panel key, because
        # the NSWindowStyleMaskNonactivatingPanel mask short-circuits
        # the activation. The user's app stays focused.
        _on_main(lambda: self._panel.orderFrontRegardless())

    def hide(self) -> None:
        def go():
            self._panel.orderOut_(None)
            self._history = ""
            self._draft = ""
            self._history_field.setStringValue_("")
            self._draft_field.setStringValue_("")
        _on_main(go)

    def append_finalized(self, text: str) -> None:
        def go():
            self._history = (self._history + text)[-_HISTORY_CHARS:]
            self._render_locked()
        _on_main(go)

    def set_draft(self, draft: str) -> None:
        def go():
            self._draft = draft
            self._render_locked()
        _on_main(go)

    # ---- main-thread internals ----

    def _render_locked(self) -> None:
        history = self._history.lstrip() if self._history else ""
        draft = self._draft if history else self._draft.lstrip()

        # Render history left-aligned, then position the draft field
        # immediately after. Manual layout because NSStackView would be
        # overkill for two labels.
        self._history_field.setStringValue_(history)
        self._history_field.sizeToFit()
        history_size = self._history_field.frame().size

        self._draft_field.setStringValue_(draft)
        self._draft_field.sizeToFit()
        draft_size = self._draft_field.frame().size

        # Keep both fields inside the panel; if combined width exceeds
        # the inner content width, draft wraps to the next visual line
        # automatically (sizeToFit respects NSTextField wrapping).
        history_origin_y = (_HEIGHT - history_size.height) / 2.0
        draft_origin_y = (_HEIGHT - draft_size.height) / 2.0
        self._history_field.setFrameOrigin_(
            (_PADDING, history_origin_y)
        )
        self._draft_field.setFrameOrigin_(
            (_PADDING + history_size.width, draft_origin_y)
        )


def _make_label(frame, fg) -> NSTextField:
    """A non-editable, transparent-background NSTextField."""
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setBezeled_(False)
    f.setBordered_(False)
    f.setDrawsBackground_(False)
    f.setFont_(NSFont.systemFontOfSize_(_FONT_SIZE))
    f.setTextColor_(
        NSColor.colorWithRed_green_blue_alpha_(*fg)
    )
    f.cell().setUsesSingleLineMode_(False)
    f.cell().setWraps_(True)
    f.cell().setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    f.setStringValue_("")
    return f

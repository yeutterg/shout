"""Floating overlay: streaming preview + batch result.

Two visual rows:
  Top:    streaming preview (history + draft, dim) — what the model is
          guessing live during the hold. NOT typed at the cursor.
  Bottom: batch result (bright) — appears after Caps Lock release once
          the full-context batch transcribe returns. THIS is what gets
          typed at the cursor.

Both rows render in an AppKit `NSPanel` configured as a non-activating
floating panel — same window class Spotlight uses, never becomes the
key window, so CGEvent text injection passes through to whatever app
the user was already focused on.

Threading: every public method is safe to call from any thread; AppKit
calls dispatch to the main thread via `NSOperationQueue.mainQueue()`.
"""

from __future__ import annotations

import logging

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
from Foundation import NSMakeRect, NSMakeSize, NSOperationQueue


_BG_R, _BG_G, _BG_B, _BG_A = 0.10, 0.10, 0.10, 0.92
_FG_HISTORY = (0.65, 0.66, 0.68, 1.00)   # dim grey
_FG_DRAFT = (0.45, 0.47, 0.51, 1.00)     # dimmer grey
_FG_BATCH = (0.96, 0.96, 0.96, 1.00)     # near-white

_STREAM_FONT_SIZE = 13.0
_BATCH_FONT_SIZE = 16.0

_WIDTH = 760.0
_HEIGHT = 140.0
_BOTTOM_MARGIN = 80.0
_PADDING = 14.0
_ROW_GAP = 8.0

# History tail length. Generous because the strip is only visible
# during a session; we are not paying ongoing render cost.
_HISTORY_CHARS = 320

log = logging.getLogger("shout.overlay")


def _on_main(fn):
    """Schedule fn() on the main thread; safe from any thread."""
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


class Overlay:
    """Non-activating floating NSPanel with streaming + batch rows."""

    def __init__(self) -> None:
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

        content = panel.contentView()
        inner_w = _WIDTH - 2.0 * _PADDING

        # Layout: streaming row on top (taller of the two when present),
        # batch row at the bottom. macOS coordinate origin is bottom-left.
        # Positions are computed at render time because text size varies.
        history_field = _make_label(
            NSMakeRect(_PADDING, _PADDING, inner_w, 1),
            _FG_HISTORY, _STREAM_FONT_SIZE,
        )
        content.addSubview_(history_field)

        draft_field = _make_label(
            NSMakeRect(_PADDING, _PADDING, inner_w, 1),
            _FG_DRAFT, _STREAM_FONT_SIZE,
        )
        content.addSubview_(draft_field)

        batch_field = _make_label(
            NSMakeRect(_PADDING, _PADDING, inner_w, 1),
            _FG_BATCH, _BATCH_FONT_SIZE,
        )
        content.addSubview_(batch_field)

        self._panel = panel
        self._history_field = history_field
        self._draft_field = draft_field
        self._batch_field = batch_field
        self._history = ""
        self._draft = ""
        self._batch = ""

    # ---- public API (safe from any thread) ----

    def show(self) -> None:
        def go():
            # Reset state on each new session so leftover text from a
            # previous (auto-hidden) session does not flash.
            self._history = ""
            self._draft = ""
            self._batch = ""
            self._render_locked()
            self._panel.orderFrontRegardless()
        _on_main(go)

    def hide(self) -> None:
        def go():
            self._panel.orderOut_(None)
            self._history = ""
            self._draft = ""
            self._batch = ""
            self._history_field.setStringValue_("")
            self._draft_field.setStringValue_("")
            self._batch_field.setStringValue_("")
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

    def set_batch_result(self, text: str) -> None:
        """Set the bottom-row text (the canonical, batch-transcribed
        result that gets typed at the cursor)."""
        def go():
            self._batch = text
            self._render_locked()
        _on_main(go)

    # ---- main-thread internals ----

    def _render_locked(self) -> None:
        history = self._history.lstrip() if self._history else ""
        draft = self._draft if history else self._draft.lstrip()
        batch = self._batch.strip()

        inner_w = _WIDTH - 2.0 * _PADDING

        # Streaming row: history (left) + draft (right). sizeToFit on
        # an NSTextField with wrapping enabled grows height to fit the
        # current width.
        self._history_field.setPreferredMaxLayoutWidth_(inner_w)
        self._history_field.setStringValue_(history)
        self._history_field.sizeToFit()
        history_size = self._history_field.frame().size

        # Position the draft to the right of the history on the same
        # line. If history is wide, draft may overflow; we clip rather
        # than break the layout.
        self._draft_field.setPreferredMaxLayoutWidth_(
            max(60.0, inner_w - history_size.width)
        )
        self._draft_field.setStringValue_(draft)
        self._draft_field.sizeToFit()

        # Batch row.
        self._batch_field.setPreferredMaxLayoutWidth_(inner_w)
        self._batch_field.setStringValue_(batch)
        self._batch_field.sizeToFit()
        batch_size = self._batch_field.frame().size

        # Position rows. Origin (0, 0) is bottom-left.
        # Bottom row (batch) sits at PADDING.
        # Top row (streaming) sits above with a gap. If batch is empty,
        # the streaming row centers vertically instead.
        if batch:
            batch_y = _PADDING
            stream_y = _PADDING + batch_size.height + _ROW_GAP
        else:
            batch_y = _PADDING  # invisible (empty string)
            stream_y = (_HEIGHT - history_size.height) / 2.0

        self._history_field.setFrameOrigin_(
            NSMakeSize(_PADDING, stream_y)
        )
        self._draft_field.setFrameOrigin_(
            NSMakeSize(_PADDING + history_size.width, stream_y)
        )
        self._batch_field.setFrameOrigin_(
            NSMakeSize(_PADDING, batch_y)
        )


def _make_label(frame, fg, font_size: float) -> NSTextField:
    """A non-editable, transparent-background NSTextField that wraps."""
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setBezeled_(False)
    f.setBordered_(False)
    f.setDrawsBackground_(False)
    f.setFont_(NSFont.systemFontOfSize_(font_size))
    f.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(*fg))
    f.cell().setUsesSingleLineMode_(False)
    f.cell().setWraps_(True)
    f.cell().setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    f.setStringValue_("")
    return f

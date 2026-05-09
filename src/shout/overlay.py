"""Floating tentative-token overlay (tkinter-based).

Borderless, always-on-top, semi-transparent strip pinned to bottom-center.
v0 polish target: legible and unobtrusive. Swift NSPanel is a v1 swap.

The strip shows a rolling window of the most-recent finalized text plus
the current (still-being-revised) draft. Showing only the draft is
information-poor — the draft is just whatever falls inside the right-
context window of parakeet-mlx, typically 1-3 words. With finalized
history added, the user sees a sentence-or-so of context, which makes
edits and intent visible without scrolling the cursor.

Critical macOS detail: Tk Toplevels become the *key* window when shown
(especially after `lift()`), which steals keyboard focus from whatever
app the user was typing into. CGEvent text injection then lands in the
overlay (a tk.Label, which silently drops keystrokes) instead of the
target app. We fix this by switching the window to the macOS native
"floating, non-activating" style — a class of NSPanel that floats
above other windows without ever becoming key.

Threading rule: all tkinter calls must come from the thread that owns
the Tk root (the daemon's main thread). Workers schedule updates via
the daemon's UI queue, drained by `Daemon._drain_ui_queue`.
"""

from __future__ import annotations

import logging
import tkinter as tk


_BG = "#1a1a1a"
_FG_FINAL = "#dddddd"
_FG_DRAFT = "#9aa0a8"
# Half the prior font size so we can fit ~2x the words.
_FONT = ("Helvetica Neue", 14)
# Roughly 2x the prior min-height. Actual size tracks text via geometry.
_MIN_HEIGHT = 112
_BOTTOM_MARGIN = 80
# Fixed overlay width, so growing text wraps within it instead of
# stretching the strip across the screen.
_WIDTH = 760
# Roll the displayed history up to this many characters. With the
# smaller font and wider strip, ~240 chars renders comfortably across
# 2-3 wrapped lines.
_HISTORY_CHARS = 240

log = logging.getLogger("shout.overlay")


class Overlay:
    """Wraps a Toplevel that shows a rolling transcript window."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        # Set the macOS NSPanel-style class to "floating" with the
        # `nonActivatingPanel` attribute. This is the same trick
        # Wispr Flow / Spotlight / Alfred use: the window floats above
        # other apps without ever stealing focus, so CGEvent text
        # injection continues to land in whatever app was previously
        # focused.
        try:
            self._win.tk.call(
                "::tk::unsupported::MacWindowStyle",
                "style",
                self._win._w,
                "floating",
                "nonActivating",
            )
        except tk.TclError:
            log.warning(
                "could not set MacWindowStyle (non-macOS or older Tk?); "
                "overlay may steal keyboard focus"
            )
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.92)
        self._win.configure(bg=_BG)

        # Two adjacent labels: brighter for finalized history, dimmer
        # for the draft tail. wraplength forces text to wrap at the
        # overlay width rather than letting the strip grow horizontally.
        self._frame = tk.Frame(self._win, bg=_BG)
        self._frame.pack(padx=18, pady=12, fill=tk.BOTH, expand=True)
        wrap = _WIDTH - 36  # account for padx
        self._final_label = tk.Label(
            self._frame,
            text="",
            fg=_FG_FINAL,
            bg=_BG,
            font=_FONT,
            wraplength=wrap,
            justify=tk.LEFT,
            anchor="w",
        )
        self._final_label.pack(side=tk.LEFT, anchor="nw")
        self._draft_label = tk.Label(
            self._frame,
            text="",
            fg=_FG_DRAFT,
            bg=_BG,
            font=_FONT,
            wraplength=wrap,
            justify=tk.LEFT,
            anchor="w",
        )
        self._draft_label.pack(side=tk.LEFT, anchor="nw")

        self._history = ""
        self._draft = ""

    # ---- public API ----

    def show(self) -> None:
        self._reposition()
        self._win.deiconify()
        # NB: no lift() — that promotes the window to key/active and
        # would defeat the nonActivating panel style on macOS.

    def hide(self) -> None:
        self._win.withdraw()
        self._history = ""
        self._draft = ""
        self._final_label.configure(text="")
        self._draft_label.configure(text="")

    def append_finalized(self, text: str) -> None:
        self._history = (self._history + text)[-_HISTORY_CHARS:]
        self._render()

    def set_draft(self, draft: str) -> None:
        self._draft = draft
        self._render()

    # ---- internals ----

    def _render(self) -> None:
        history = self._history.lstrip() if self._history else ""
        draft = self._draft if history else self._draft.lstrip()
        self._final_label.configure(text=history)
        self._draft_label.configure(text=draft)
        self._reposition()

    def _reposition(self) -> None:
        self._win.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        win_w = _WIDTH
        win_h = max(self._win.winfo_reqheight(), _MIN_HEIGHT)
        x = (screen_w - win_w) // 2
        y = screen_h - win_h - _BOTTOM_MARGIN
        self._win.geometry(f"{win_w}x{win_h}+{x}+{y}")

"""Floating tentative-token overlay (tkinter-based).

Borderless, always-on-top, semi-transparent strip pinned to bottom-center.
v0 polish target: legible and unobtrusive. Swift NSPanel is a v1 swap.

Threading rule: all tkinter calls must come from the thread that owns
the Tk root (the daemon's main thread). Workers schedule updates via
the daemon's UI queue, drained by `Daemon._drain_ui_queue`.
"""

from __future__ import annotations

import tkinter as tk


_BG = "#1a1a1a"
_FG = "#dddddd"
_FONT = ("Helvetica Neue", 18)
_HEIGHT = 56
_BOTTOM_MARGIN = 80


class Overlay:
    """Wraps a Toplevel that shows tentative tokens during a session.

    The overlay is created hidden and toggled via `show()` / `hide()`.
    Text is set via `set_text()` (idempotent — last writer wins).

    Construct on the main thread once at daemon startup; never destroy
    and recreate (would re-trigger window-server registration cost).
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.92)
        self._win.configure(bg=_BG)

        self._label = tk.Label(
            self._win,
            text="",
            fg=_FG,
            bg=_BG,
            font=_FONT,
            padx=18,
            pady=12,
        )
        self._label.pack()

    def show(self) -> None:
        self._reposition()
        self._win.deiconify()
        self._win.lift()

    def hide(self) -> None:
        self._win.withdraw()
        self._label.configure(text="")

    def set_text(self, draft: str) -> None:
        # Strip leading whitespace — Parakeet often emits a leading space
        # at sentence start, which looks ugly pinned to the strip's left.
        self._label.configure(text=draft.lstrip())
        # Single update + reposition. set_text fires up to 30 Hz so we
        # avoid back-to-back update_idletasks() calls.
        self._reposition()

    def _reposition(self) -> None:
        self._win.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        win_w = max(self._win.winfo_reqwidth(), 200)
        win_h = max(self._win.winfo_reqheight(), _HEIGHT)
        x = (screen_w - win_w) // 2
        y = screen_h - win_h - _BOTTOM_MARGIN
        self._win.geometry(f"{win_w}x{win_h}+{x}+{y}")

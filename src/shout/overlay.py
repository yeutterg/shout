"""Floating tentative-token overlay (tkinter-based).

Borderless, always-on-top, semi-transparent strip pinned to bottom-center.
v0 polish target: legible and unobtrusive. Swift NSPanel is a v1 swap.

The strip shows a rolling window of the most-recent finalized text plus
the current (still-being-revised) draft. Showing only the draft is
information-poor — the draft is just whatever falls inside the right-
context window of parakeet-mlx, typically 1-3 words. With finalized
history added, the user sees a sentence-or-so of context, which makes
edits and intent visible without scrolling the cursor.

Threading rule: all tkinter calls must come from the thread that owns
the Tk root (the daemon's main thread). Workers schedule updates via
the daemon's UI queue, drained by `Daemon._drain_ui_queue`.
"""

from __future__ import annotations

import tkinter as tk


_BG = "#1a1a1a"
_FG_FINAL = "#dddddd"
_FG_DRAFT = "#9aa0a8"
_FONT = ("Helvetica Neue", 18)
_HEIGHT = 56
_BOTTOM_MARGIN = 80

# Roll the displayed history up to this many characters. The strip
# wraps to fit, so we cap text length rather than width to keep
# rendering cost bounded.
_HISTORY_CHARS = 120


class Overlay:
    """Wraps a Toplevel that shows a rolling transcript window."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.92)
        self._win.configure(bg=_BG)

        # Tk's Label only takes a single foreground; to render finalized
        # text brighter than draft we use two adjacent Labels packed
        # left-to-right inside a frame.
        self._frame = tk.Frame(self._win, bg=_BG)
        self._frame.pack(padx=18, pady=12)
        self._final_label = tk.Label(
            self._frame, text="", fg=_FG_FINAL, bg=_BG, font=_FONT
        )
        self._final_label.pack(side=tk.LEFT)
        self._draft_label = tk.Label(
            self._frame, text="", fg=_FG_DRAFT, bg=_BG, font=_FONT
        )
        self._draft_label.pack(side=tk.LEFT)

        self._history = ""  # tail of finalized text
        self._draft = ""

    # ---- public API ----

    def show(self) -> None:
        self._reposition()
        self._win.deiconify()
        self._win.lift()

    def hide(self) -> None:
        self._win.withdraw()
        self._history = ""
        self._draft = ""
        self._final_label.configure(text="")
        self._draft_label.configure(text="")

    def append_finalized(self, text: str) -> None:
        """Append text that the daemon has just typed at the cursor."""
        self._history = (self._history + text)[-_HISTORY_CHARS:]
        self._render()

    def set_draft(self, draft: str) -> None:
        """Replace the current draft (still-being-revised tail)."""
        self._draft = draft
        self._render()

    # ---- internals ----

    def _render(self) -> None:
        # Strip a leading space if history is empty (Parakeet often
        # emits a leading space at sentence start, which looks ugly
        # pinned to the strip's left edge).
        history = self._history if self._history else self._history
        draft = self._draft
        if not history:
            history_render = ""
            draft_render = draft.lstrip()
        else:
            history_render = history.lstrip()
            draft_render = draft

        self._final_label.configure(text=history_render)
        self._draft_label.configure(text=draft_render)
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

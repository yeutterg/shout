"""Type Unicode text at the cursor via Quartz CGEvent.

We avoid the paste buffer (it collides with whatever the user has copied)
and we avoid AppleScript (slow, requires Accessibility differently).
CGEventKeyboardSetUnicodeString sends arbitrary Unicode through the HID
event tap, which works in any focused text field on macOS — Cursor,
Slack, terminals, browsers, native apps.

Accessibility permission is required for the *posting* process (the
Shout daemon, not Hammerspoon). System Settings → Privacy & Security →
Accessibility → add the Python interpreter or the daemon binary.
"""

from __future__ import annotations

import Quartz


# CGEventKeyboardSetUnicodeString accepts up to 20 UTF-16 code units per
# event. Longer strings have to be chunked or the tail is silently
# dropped. We chunk by code-point to stay well clear of the surrogate-pair
# edge case at the boundary.
_MAX_CHARS_PER_EVENT = 16


def type_text(text: str) -> None:
    """Type the given text at the current keyboard focus.

    No-op on empty strings. Does not block waiting for the OS to deliver
    events — the calls are fire-and-forget at the HID layer.
    """
    if not text:
        return

    for chunk in _chunks(text, _MAX_CHARS_PER_EVENT):
        # virtualKey=0 is fine: Set...UnicodeString overrides the keycode
        # interpretation. keyDown=True post; we omit a separate keyUp
        # because a single keyDown event is enough for text injection
        # (and matches what other macOS dictation tools do).
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i : i + n]

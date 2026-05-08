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
# event. Chunking by Python str length is wrong for any non-BMP code point
# (emoji, supplementary CJK), where one Python char = two UTF-16 units —
# a 16-char chunk could yield 32 UTF-16 units and silently truncate.
# We chunk by encoded UTF-16 length and back up to a non-surrogate
# boundary so we never split a surrogate pair.
_MAX_UTF16_UNITS = 20


def type_text(text: str) -> None:
    """Type the given text at the current keyboard focus.

    No-op on empty strings. Does not block waiting for the OS to deliver
    events — the calls are fire-and-forget at the HID layer.
    """
    if not text:
        return

    for chunk in _utf16_safe_chunks(text, _MAX_UTF16_UNITS):
        # virtualKey=0 is fine: Set...UnicodeString overrides the keycode
        # interpretation. We post a single keyDown — a separate keyUp
        # would type the character a second time.
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        # The pyobjc bridge expects a Python str; libdispatch converts to
        # UTF-16 internally. The length we pass is in UTF-16 code units.
        utf16_len = len(chunk.encode("utf-16-le")) // 2
        Quartz.CGEventKeyboardSetUnicodeString(event, utf16_len, chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _utf16_safe_chunks(s: str, max_units: int):
    """Yield substrings whose UTF-16 encoded length is ≤ max_units, never
    splitting a surrogate pair. Each non-BMP code point counts as 2."""
    buf = []
    units = 0
    for ch in s:
        ch_units = 2 if ord(ch) > 0xFFFF else 1
        if units + ch_units > max_units:
            yield "".join(buf)
            buf = [ch]
            units = ch_units
        else:
            buf.append(ch)
            units += ch_units
    if buf:
        yield "".join(buf)

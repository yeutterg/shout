"""macOS Microphone & Accessibility permission probes.

The single most common reason Shout sessions produce empty transcripts
is that the daemon's Python interpreter is not in System Settings →
Privacy & Security → Microphone. macOS does NOT raise an error in this
case — `sounddevice.InputStream` opens, audio frames arrive, but every
sample is zero. The model duly transcribes "silence" to the empty
string and the user wonders what is broken.

We can detect this up front via `AVCaptureDevice.authorizationStatus
ForMediaType_` (no actual recording required). For Accessibility we
check `AXIsProcessTrustedWithOptions(None)`.
"""

from __future__ import annotations

import logging

import objc

log = logging.getLogger("shout.permissions")


# AVMediaTypeAudio is "soun" — a 4-char-code string.
_MEDIA_AUDIO = "soun"

# AVAuthorizationStatus values.
_NOT_DETERMINED = 0
_RESTRICTED = 1
_DENIED = 2
_AUTHORIZED = 3


def microphone_status() -> str:
    """One of: 'authorized' | 'denied' | 'restricted' |
    'not_determined' | 'unknown'."""
    try:
        from AVFoundation import AVCaptureDevice
    except ImportError:
        return "unknown"
    status = AVCaptureDevice.authorizationStatusForMediaType_(_MEDIA_AUDIO)
    return {
        _AUTHORIZED: "authorized",
        _DENIED: "denied",
        _RESTRICTED: "restricted",
        _NOT_DETERMINED: "not_determined",
    }.get(int(status), "unknown")


def request_microphone_async() -> None:
    """Trigger the macOS prompt asking for Microphone access for this
    binary. Must be called while the user is interactively present;
    on a launchd-spawned daemon the OS may suppress the prompt."""
    try:
        from AVFoundation import AVCaptureDevice
    except ImportError:
        return

    def _cb(granted: bool) -> None:
        log.info("microphone permission grant=%s", granted)

    AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        _MEDIA_AUDIO, _cb
    )


def accessibility_effective() -> bool:
    """Whether CGEventTap creation actually succeeds.

    AXIsProcessTrustedWithOptions(NULL) is the documented "is this
    process in the Accessibility list" check, but in pyobjc on macOS
    14+ it returns False for ad-hoc-signed Pythons even when CGEvent
    APIs work fine (the user may have granted Input Monitoring instead
    of Accessibility, or both, and AX checks one but not the other).
    The reliable signal is whether `CGEventTapCreate` returns a non-
    NULL CFMachPort. We probe with a listen-only tap that we tear down
    immediately, so there is no lasting side-effect.
    """
    import Quartz

    mask = (1 << Quartz.kCGEventKeyDown) | (1 << Quartz.kCGEventKeyUp)
    try:
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            1,  # kCGEventTapOptionListenOnly — no event modification
            mask,
            lambda proxy, t, ev, ctx: ev,
            None,
        )
    except Exception:
        return False
    if not tap:
        return False
    Quartz.CFRelease(tap)
    return True

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


def accessibility_trusted() -> bool:
    """True iff the current process is trusted to post + tap CGEvents.
    Same call AppKit/Quartz docs recommend for accessibility apps."""
    try:
        # HIServices, exposed on the ApplicationServices bundle.
        from ApplicationServices import AXIsProcessTrustedWithOptions
    except ImportError:
        return False
    try:
        return bool(AXIsProcessTrustedWithOptions(None))
    except Exception:
        return False

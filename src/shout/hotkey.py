"""F19 push-to-talk listener via Quartz CGEventTap.

Replaces what Hammerspoon was doing: bind F19 down/up, debounce a quick
tap, detect triple-tap to synthesize a real Caps Lock keystroke.

Runs on its own thread because CGEventTap requires a CFRunLoop. The
callback runs on that thread; it posts plain string commands ("start"
or "stop") onto the daemon's cmd_q. Latency from key event to push
onto the queue is sub-millisecond (the callback does only timestamp
math + a queue put).

Permission required: Accessibility for the daemon's Python interpreter.
That's the same permission already needed for `inject.type_text`, so
killing Hammerspoon does NOT add a permission grant; it removes one
(Hammerspoon's own Accessibility grant is no longer needed).
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Queue
from typing import Optional

import Quartz

# kVK_F19 (HIToolbox virtual keycode). Apple's HID Usage 0x6E maps to
# this when the keyboard hardware speaks Apple keyboard layout — which
# every Mac does, and which our hidutil remap targets.
_F19_KEYCODE = 80

# kVK_CapsLock — used when synthesizing a real Caps Lock keystroke on
# triple-tap. Synthesized events bypass hidutil's UserKeyMapping
# (hidutil sits at the IOKit-driver layer; CGEventPost injects above
# that), so this does NOT recursively remap to F19.
_CAPS_LOCK_KEYCODE = 57

_TRIPLE_TAP_WINDOW_S = 0.4
_HOLD_DEBOUNCE_S = 0.15

log = logging.getLogger("shout.hotkey")


class HotkeyListener:
    """Thread that watches F19 and translates it into start/stop commands.

    Lifecycle:
        listener = HotkeyListener(cmd_q)
        listener.start()   # spawns thread, returns immediately
        ...
        listener.stop()    # tears down event tap and joins thread
    """

    def __init__(self, cmd_q: "Queue[str]") -> None:
        self._cmd_q = cmd_q
        self._thread: Optional[threading.Thread] = None
        self._tap = None
        self._runloop = None
        self._tap_failed = False

        # Tap-history + hold-debounce state (touched only from the
        # CGEventTap callback thread).
        self._tap_times: list[float] = []
        self._press_pending = False
        self._press_at: float = 0.0
        self._session_active = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="shout-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ----------------- internals -----------------

    def _run(self) -> None:
        # Listen-and-modify: returning None from the callback suppresses
        # the F19 event so it does not surface in the keyboard input
        # stream of any other app. Any non-None return passes through.
        event_mask = (
            (1 << Quartz.kCGEventKeyDown)
            | (1 << Quartz.kCGEventKeyUp)
        )

        # Bind callback to this instance so it can mutate state.
        def cb(proxy, event_type, event, refcon):
            return self._callback(event_type, event)

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            0,  # default options (allow filtering)
            event_mask,
            cb,
            None,
        )
        if not self._tap:
            self._tap_failed = True
            log.error(
                "CGEventTapCreate returned NULL. The daemon needs "
                "Accessibility permission (System Settings → Privacy & "
                "Security → Accessibility), and a daemon restart after "
                "granting it (`brew services restart shout`)."
            )
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(
            self._runloop, source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(self._tap, True)
        log.info("hotkey listener: F19 tap armed")

        # Run a hold-debounce checker in the same run loop. Without
        # this, a quick tap (released before _HOLD_DEBOUNCE_S) would
        # never hit the worker because we only fire `start` after the
        # debounce window.
        Quartz.CFRunLoopRun()

    def _callback(self, event_type, event):
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        if keycode != _F19_KEYCODE:
            return event  # not ours, pass through

        # While the user holds the key, macOS sends a stream of
        # auto-repeat keyDown events (~30 Hz). We must ignore those:
        # otherwise our tap-counter immediately hits 3 within 400 ms
        # and fires the triple-tap → real-Caps-Lock path, which ends
        # the PTT session prematurely and yanks the overlay away.
        if event_type == Quartz.kCGEventKeyDown:
            autorepeat = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventAutorepeat
            )
            if not autorepeat:
                self._on_press()
        elif event_type == Quartz.kCGEventKeyUp:
            self._on_release()
        # Suppress the F19 event itself so no other app sees it.
        return None

    def _on_press(self) -> None:
        now = time.monotonic()

        # Update tap history (slide window).
        self._tap_times.append(now)
        cutoff = now - _TRIPLE_TAP_WINDOW_S
        self._tap_times = [t for t in self._tap_times if t >= cutoff]

        if len(self._tap_times) >= 3:
            self._tap_times = []
            if self._session_active:
                self._cmd_q.put("stop")
                self._session_active = False
            self._press_pending = False
            _synthesize_caps_lock_toggle()
            return

        # Defer the actual `start` until we know it is a hold and not
        # a tap. The release handler cancels if we got out before the
        # debounce window elapses; otherwise the wakeup timer fires.
        self._press_pending = True
        self._press_at = now
        # CFRunLoopAddTimer would be cleaner, but the timer setup from
        # Python is awkward. We instead post a one-shot via dispatch
        # after the debounce window to evaluate whether to fire.
        threading.Thread(
            target=self._maybe_start, args=(now,), daemon=True
        ).start()

    def _maybe_start(self, press_id: float) -> None:
        time.sleep(_HOLD_DEBOUNCE_S)
        # If a release arrived first or a triple-tap claimed the press,
        # _press_pending will have been cleared.
        if self._press_pending and self._press_at == press_id:
            self._press_pending = False
            self._session_active = True
            self._cmd_q.put("start")

    def _on_release(self) -> None:
        if self._press_pending:
            # A tap, not a hold. Cancel the deferred start.
            self._press_pending = False
            return
        if self._session_active:
            self._cmd_q.put("stop")
            self._session_active = False


def _synthesize_caps_lock_toggle() -> None:
    """Inject a real Caps Lock keystroke. Bypasses hidutil's mapping."""
    down = Quartz.CGEventCreateKeyboardEvent(None, _CAPS_LOCK_KEYCODE, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, _CAPS_LOCK_KEYCODE, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

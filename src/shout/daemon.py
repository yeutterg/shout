"""Long-running Shout daemon.

Lifecycle (single process, four threads):

  Main thread       AppKit run loop. Owns the NSPanel overlay. Workers
                    post UI updates as blocks on the main NSOperationQueue.

  Socket thread     Accepts Unix-socket connections, parses commands,
                    posts session-state messages onto cmd_q.

  Hotkey thread     Quartz CGEventTap for F19 (down/up + auto-repeat
                    filter + triple-tap → real-Caps-Lock fallback).
                    Posts "start" / "stop" onto cmd_q.

  Worker thread     Owns the MLX model and the active Streamer. Loads
                    weights once at startup, pre-warms the encoder
                    shaders against a chunk of silence (otherwise the
                    first real session would pay a ~2.5 s shader-compile
                    cost — see scripts/bench-cold-start.py), then runs
                    a session loop: awaits start, ticks at ~30 Hz,
                    types newly-finalized text via Quartz, calls
                    overlay.append_finalized / set_draft.

MLX has per-thread stream state — the model and all add_audio calls
must happen on the same thread that loaded the model. That is why the
worker thread (not the main thread) loads the model and runs Streamer.

The daemon survives session-level errors: a failed start or a crashed
audio backend logs and returns to idle, ready for the next start. Model
load failures are fatal and exit non-zero so launchd can restart us.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from queue import Empty, Queue
from typing import Optional

from AppKit import NSApp, NSApplication, NSApplicationActivationPolicyAccessory
from Foundation import NSOperationQueue

from . import config, inject, paths, permissions, protocol
from .hotkey import HotkeyListener
from .menubar import MenuBar
from .overlay import Overlay
from .stream import DEFAULT_CONTEXT_SIZE, Streamer

# English-only Parakeet. We previously defaulted to v3 (multilingual,
# auto-detect across ~25 European languages), but the user reported
# "the transcription is way off, it did another language!!" — short or
# noisy English clips can trip v3's language auto-detect into Spanish,
# French, etc. v2 has no auto-detect, same size and architecture.
# Override via ~/Library/Application Support/Shout/config.json:
#   {"model": "mlx-community/parakeet-tdt-0.6b-v3"}  # to opt back into multilingual
DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

# How often the worker polls the streamer. ~33 Hz is fast enough for
# tokens to feel live without burning a core.
_TICK_HZ = 30
_TICK_INTERVAL_S = 1.0 / _TICK_HZ

# Shader pre-warm: feed N seconds of silence through transcribe_stream
# before the first real session, so the Metal kernels are JIT-compiled
# and cached. ~1 s is plenty for the encoder pass to fire at least once.
_WARMUP_AUDIO_SECONDS = 1.0
_WARMUP_SAMPLE_RATE = 16_000

log = logging.getLogger("shout.daemon")


class Daemon:
    def __init__(self, model_id: str | None = None) -> None:
        # Resolution order: explicit constructor arg > config.json > default.
        self._model_id = model_id or config.get_model() or DEFAULT_MODEL

        self._cmd_q: Queue[str] = Queue()
        self._model_ready = threading.Event()
        self._session_running = threading.Event()
        self._reload_requested = threading.Event()
        self._shutdown = threading.Event()
        self._exit_code = 0

        self._overlay: Optional[Overlay] = None
        self._menubar: Optional[MenuBar] = None
        self._hotkey: Optional[HotkeyListener] = None
        # Pending hide of the overlay 2 s after a session ends. Stored
        # so the next session can cancel it before the timer fires.
        self._auto_hide_timer: Optional[threading.Timer] = None

    # ----------------- public entry point -----------------

    def run(self) -> int:
        paths.ensure_app_support()
        self._setup_logging()
        log.info("daemon starting; model=%s", self._model_id)

        # Surface microphone state at startup. The single most common
        # reason transcripts come back empty is silent-zeros from a
        # denied Microphone (sounddevice opens InputStream successfully
        # but every sample is 0; see python-sounddevice#196). Mic check
        # is read-only via AVCaptureDevice.
        mic_status = permissions.microphone_status()
        log.info("permissions: microphone=%s", mic_status)
        if mic_status not in ("authorized", "unknown"):
            log.error(
                "Microphone permission is %r. Audio capture will return "
                "zeros and every transcript will be empty. Grant it in "
                "System Settings → Privacy & Security → Microphone for "
                "%s, then `brew services restart shout`.",
                mic_status, sys.executable,
            )
        # CGEventTap rights (Accessibility OR Input Monitoring) are
        # checked when HotkeyListener.start() actually creates the tap;
        # the listener itself logs a clear error if creation fails. We
        # do not pre-probe here because Apple's
        # AXIsProcessTrustedWithOptions disagrees with the actual
        # CGEventTap behavior for ad-hoc-signed Pythons.

        # NSApplication has to be initialised on the main thread before
        # any AppKit object exists. Setting policy to Accessory keeps us
        # out of the dock and app-switcher.
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        self._overlay = Overlay()
        # Menu bar lives in the same NSApplication run loop as the
        # overlay; constructed on main thread.
        # `on_restart` is in-process model reload (the worker drops the
        # current model, loads the one config.get_model() now points at,
        # warms shaders, resumes serving) — so the menu bar item never
        # disappears. `on_quit` exits cleanly (code 0); brew services
        # treats that as "stay down" and does not relaunch.
        self._menubar = MenuBar(
            on_quit=lambda: self._shutdown_now(0),
            on_restart=lambda: self._reload_requested.set(),
        )

        threading.Thread(
            target=self._socket_loop, name="shout-socket", daemon=True
        ).start()
        threading.Thread(
            target=self._worker_loop, name="shout-worker", daemon=True
        ).start()

        # F19 listener replaces what Hammerspoon used to do. Same cmd_q
        # the socket thread uses.
        self._hotkey = HotkeyListener(self._cmd_q)
        self._hotkey.start()

        try:
            NSApp.run()  # blocks until os._exit (see _shutdown_now).
        except KeyboardInterrupt:
            self._shutdown_now(0)
        return self._exit_code

    # ----------------- worker thread -----------------

    def _worker_loop(self) -> None:
        # Imported here so MLX initializes on this thread.
        import mlx.core as mx
        import parakeet_mlx

        def load(model_id: str):
            log.info("loading model %s …", model_id)
            t0 = time.perf_counter()
            try:
                m = parakeet_mlx.from_pretrained(model_id)
            except Exception:
                log.exception("model load failed: %s", model_id)
                return None
            log.info("model loaded in %.2f s", time.perf_counter() - t0)
            return m

        def warm(m) -> None:
            # Pre-warm shader compilation. Without this, the first
            # add_audio() of the first session pays ~2.5 s of Metal
            # kernel JIT (see bench-cold-start.py phase `first_chunk_s`
            # first run vs. warm runs).
            try:
                t0 = time.perf_counter()
                silence = mx.zeros(
                    int(_WARMUP_AUDIO_SECONDS * _WARMUP_SAMPLE_RATE)
                )
                with m.transcribe_stream(
                    context_size=DEFAULT_CONTEXT_SIZE
                ) as s:
                    s.add_audio(silence)
                log.info("shaders warm in %.2f s", time.perf_counter() - t0)
            except Exception:
                log.warning("shader warm-up failed", exc_info=True)

        model = load(self._model_id)
        if model is None:
            self._shutdown_now(1)
            return
        warm(model)
        self._model_ready.set()

        while not self._shutdown.is_set():
            # Service a model-reload request (from the menu bar's
            # Model / Language selection) before pulling the next
            # session command.
            if self._reload_requested.is_set():
                self._reload_requested.clear()
                new_id = config.get_model() or DEFAULT_MODEL
                if new_id != self._model_id:
                    log.info("reload: %s → %s", self._model_id, new_id)
                    self._model_ready.clear()
                    new_model = load(new_id)
                    if new_model is None:
                        # Reload failed — keep the old model and
                        # revert config so the menu bar reflects truth.
                        log.error("reload failed; reverting config")
                        config.set_model(
                            None if self._model_id == DEFAULT_MODEL
                            else self._model_id
                        )
                        self._model_ready.set()
                    else:
                        model = new_model
                        self._model_id = new_id
                        mx.clear_cache()
                        warm(model)
                        self._model_ready.set()

            try:
                cmd = self._cmd_q.get(timeout=0.1)
            except Empty:
                continue
            if cmd == protocol.CMD_START:
                self._run_session(model)
            # Stray STOP outside a session is a no-op.

    def _run_session(self, model) -> None:
        if self._session_running.is_set():
            log.warning("start ignored: session already running")
            return
        self._session_running.set()
        log.info("session: start")

        # Cancel any pending auto-hide from the previous session so it
        # does not fire mid-way through this one.
        if self._auto_hide_timer is not None:
            self._auto_hide_timer.cancel()
            self._auto_hide_timer = None

        self._overlay.show()

        streamer = Streamer(model, input_device=config.get_input_device())
        try:
            streamer.start()
        except Exception:
            log.exception("session: failed to start streamer")
            self._overlay.hide()
            self._session_running.clear()
            return

        # Streaming phase: live preview only — text is NOT typed at the
        # cursor during the hold. We type once at end-of-session from
        # the batch result (see below), which is materially more
        # accurate because it sees the full audio in both directions
        # rather than the streaming model's ~1.3 s right-context window.
        try:
            while True:
                try:
                    cmd = self._cmd_q.get_nowait()
                except Empty:
                    cmd = None

                if cmd == protocol.CMD_STOP or self._shutdown.is_set():
                    break

                frame = streamer.tick()
                if frame is not None:
                    if frame.finalized_delta:
                        self._overlay.append_finalized(frame.finalized_delta)
                    self._overlay.set_draft(frame.draft)

                time.sleep(_TICK_INTERVAL_S)
        except Exception:
            log.exception("session: error mid-stream")

        # Batch phase: close audio capture, run a full-context batch
        # transcribe, type the result at the cursor.
        try:
            streamer.stop()
        except Exception:
            log.exception("session: error closing streamer")

        try:
            batch_text = streamer.batch_transcribe()
        except Exception:
            log.exception("batch transcribe failed")
            batch_text = ""

        if batch_text:
            self._overlay.set_batch_result(batch_text)
            inject.type_text(batch_text)
        log.info("session: stop (batch=%r)", batch_text)

        # Clear `_session_running` BEFORE arming the auto-hide timer.
        # If we cleared after, a fast re-press could land in the gap
        # between session N's end and timer-arm and get rejected with
        # "session already running".
        self._session_running.clear()

        self._auto_hide_timer = threading.Timer(2.0, self._overlay.hide)
        self._auto_hide_timer.daemon = True
        self._auto_hide_timer.start()

    # ----------------- socket thread -----------------

    def _socket_loop(self) -> None:
        sock_path = str(paths.SOCKET_PATH)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        srv.listen(4)
        srv.settimeout(0.5)
        log.info("socket bound at %s", sock_path)

        while not self._shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_client(conn)
            finally:
                conn.close()
        srv.close()

    def _handle_client(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        try:
            data = conn.recv(4096)
            if not data:
                return
            msg = protocol.decode(data.split(b"\n", 1)[0])
        except Exception as e:
            conn.sendall(protocol.encode({"ok": False, "error": str(e)}))
            return

        cmd = msg.get("cmd")
        if cmd in (protocol.CMD_START, protocol.CMD_STOP):
            if cmd == protocol.CMD_START and not self._model_ready.is_set():
                conn.sendall(
                    protocol.encode(
                        {"ok": False, "error": "model still loading"}
                    )
                )
                return
            self._cmd_q.put(cmd)
            conn.sendall(protocol.encode({"ok": True, "cmd": cmd}))
        elif cmd == protocol.CMD_PING:
            conn.sendall(
                protocol.encode(
                    {
                        "ok": True,
                        "model_ready": self._model_ready.is_set(),
                        "session_running": self._session_running.is_set(),
                        "model": self._model_id,
                    }
                )
            )
        elif cmd == protocol.CMD_QUIT:
            conn.sendall(protocol.encode({"ok": True}))
            self._shutdown_now(0)
        else:
            conn.sendall(
                protocol.encode({"ok": False, "error": f"unknown cmd: {cmd}"})
            )

    # ----------------- shutdown -----------------

    def _shutdown_now(self, code: int) -> None:
        """Tear down everything and exit the process with `code`.

        Called from any thread. We use os._exit (rather than asking
        NSApp.run() to return) because breaking out of the AppKit
        run loop from a background thread is finicky: NSApp.stop_()
        only takes effect after the next event, so we'd need to post
        a fake event from the main queue. Doing the cleanup here on
        the calling thread and then os._exit is simpler and works
        the same in launchd's eyes."""
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self._exit_code = code
        try:
            if self._hotkey is not None:
                self._hotkey.stop()
        except Exception:
            pass
        try:
            os.unlink(paths.SOCKET_PATH)
        except FileNotFoundError:
            pass
        log.info("daemon exiting with code %d", code)
        os._exit(code)

    # ----------------- helpers -----------------

    def _setup_logging(self) -> None:
        log_path = paths.LOG_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Avoid double-configuring if the daemon is restarted in-process.
        root_log = logging.getLogger()
        if root_log.handlers:
            return
        # Pick up SHOUT_LOG_LEVEL=DEBUG from the environment for noisy
        # diagnostics; default INFO is fine for steady-state.
        level_name = os.environ.get("SHOUT_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.FileHandler(log_path),
                logging.StreamHandler(),
            ],
        )

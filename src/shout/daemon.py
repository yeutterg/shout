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
import threading
import time
from queue import Empty, Queue
from typing import Optional

from AppKit import NSApp, NSApplication, NSApplicationActivationPolicyAccessory
from Foundation import NSOperationQueue

from . import config, inject, paths, protocol
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
        self._shutdown = threading.Event()
        self._exit_code = 0

        self._overlay: Optional[Overlay] = None
        self._menubar: Optional[MenuBar] = None
        self._hotkey: Optional[HotkeyListener] = None

    # ----------------- public entry point -----------------

    def run(self) -> int:
        paths.ensure_app_support()
        self._setup_logging()
        log.info("daemon starting; model=%s", self._model_id)

        # NSApplication has to be initialised on the main thread before
        # any AppKit object exists. Setting policy to Accessory keeps us
        # out of the dock and app-switcher.
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        self._overlay = Overlay()
        # Menu bar lives in the same NSApplication run loop as the
        # overlay; constructed on main thread. Quit (clean exit 0) and
        # Restart (exit 1, which brew services interprets as a crash
        # and relaunches automatically) are both wired through
        # _shutdown_now.
        self._menubar = MenuBar(
            on_quit=lambda: self._shutdown_now(0),
            on_restart=lambda: self._shutdown_now(1),
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

        log.info("loading model …")
        t0 = time.perf_counter()
        try:
            model = parakeet_mlx.from_pretrained(self._model_id)
        except Exception:
            log.exception("model load failed")
            self._shutdown_now(1)
            return
        log.info("model loaded in %.2f s", time.perf_counter() - t0)

        # Pre-warm shader compilation. Without this, the very first
        # add_audio() call of the first session takes ~2.5 s while Metal
        # kernels are JIT-compiled (see bench-cold-start.py phase
        # `first_chunk_s` first run vs. warm runs).
        try:
            t0 = time.perf_counter()
            silence = mx.zeros(
                int(_WARMUP_AUDIO_SECONDS * _WARMUP_SAMPLE_RATE)
            )
            with model.transcribe_stream(context_size=DEFAULT_CONTEXT_SIZE) as s:
                s.add_audio(silence)
            log.info("shaders warm in %.2f s", time.perf_counter() - t0)
        except Exception:
            log.warning("shader warm-up failed", exc_info=True)

        self._model_ready.set()

        while not self._shutdown.is_set():
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
        self._overlay.show()

        streamer = Streamer(model, input_device=config.get_input_device())
        try:
            streamer.start()
        except Exception:
            log.exception("session: failed to start streamer")
            self._overlay.hide()
            self._session_running.clear()
            return

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
                        inject.type_text(frame.finalized_delta)
                        self._overlay.append_finalized(frame.finalized_delta)
                    self._overlay.set_draft(frame.draft)

                time.sleep(_TICK_INTERVAL_S)
        except Exception:
            log.exception("session: error mid-stream")

        try:
            final = streamer.stop()
            if final.finalized_delta:
                inject.type_text(final.finalized_delta)
                self._overlay.append_finalized(final.finalized_delta)
        except Exception:
            log.exception("session: error during stop")

        self._overlay.hide()
        self._session_running.clear()
        log.info("session: stop")

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

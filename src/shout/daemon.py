"""Long-running Shout daemon.

Lifecycle (single process, three threads):

  Main thread       Tk mainloop. Owns the overlay window. Polls a queue
                    every ~33 ms for UI updates posted by the worker.

  Socket thread     Accepts Unix-socket connections, parses commands,
                    posts session-state messages onto cmd_q.

  Worker thread     Owns the MLX model and the active Streamer. Loads
                    weights once at startup, then runs a session loop:
                    awaits start, ticks at ~30 Hz pushing audio through
                    parakeet-mlx, types newly-finalized text via Quartz,
                    posts overlay updates onto ui_q.

MLX has per-thread stream state — the model and all add_audio calls
must happen on the same thread that loaded the model. That is why the
worker thread (not the main thread) loads the model and runs Streamer.

The daemon survives session-level errors: a failed start or a crashed
audio backend logs and returns to idle, ready for the next start.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Optional

from . import inject, paths, protocol
from .overlay import Overlay
from .stream import Streamer

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# How often the worker polls the streamer. ~33 Hz is fast enough for
# tokens to feel live without burning a core.
_TICK_HZ = 30
_TICK_INTERVAL_S = 1.0 / _TICK_HZ

# How often the main thread drains the UI queue.
_UI_POLL_MS = 33

log = logging.getLogger("shout.daemon")


@dataclass
class _UIMsg:
    """Cross-thread message from worker → main(tk) thread."""

    kind: str  # "show" | "hide" | "draft"
    text: str = ""


class Daemon:
    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        self._model_id = model_id

        self._ui_q: Queue[_UIMsg] = Queue()
        self._cmd_q: Queue[str] = Queue()
        self._model_ready = threading.Event()
        self._session_running = threading.Event()
        self._shutdown = threading.Event()

        self._root: Optional[tk.Tk] = None
        self._overlay: Optional[Overlay] = None

    # ----------------- public entry point -----------------

    def run(self) -> int:
        paths.ensure_app_support()
        self._setup_logging()
        log.info("daemon starting; model=%s", self._model_id)

        self._root = tk.Tk()
        self._root.withdraw()
        self._overlay = Overlay(self._root)

        threading.Thread(
            target=self._socket_loop, name="shout-socket", daemon=True
        ).start()
        threading.Thread(
            target=self._worker_loop, name="shout-worker", daemon=True
        ).start()

        self._root.after(_UI_POLL_MS, self._drain_ui_queue)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown.set()
            try:
                os.unlink(paths.SOCKET_PATH)
            except FileNotFoundError:
                pass
        return 0

    # ----------------- main thread (tk) -----------------

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                msg = self._ui_q.get_nowait()
                if msg.kind == "show":
                    self._overlay.show()
                elif msg.kind == "hide":
                    self._overlay.hide()
                elif msg.kind == "draft":
                    self._overlay.set_text(msg.text)
        except Empty:
            pass
        if not self._shutdown.is_set():
            self._root.after(_UI_POLL_MS, self._drain_ui_queue)
        else:
            self._root.quit()

    # ----------------- worker thread -----------------

    def _worker_loop(self) -> None:
        # Imported here so MLX initializes on this thread.
        import parakeet_mlx

        log.info("loading model …")
        t0 = time.perf_counter()
        try:
            model = parakeet_mlx.from_pretrained(self._model_id)
        except Exception:
            log.exception("model load failed")
            self._shutdown.set()
            return
        log.info("model loaded in %.2f s", time.perf_counter() - t0)
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
        self._ui_q.put(_UIMsg(kind="show"))

        streamer = Streamer(model)
        try:
            streamer.start()
        except Exception:
            log.exception("session: failed to start streamer")
            self._ui_q.put(_UIMsg(kind="hide"))
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
                    self._ui_q.put(_UIMsg(kind="draft", text=frame.draft))

                time.sleep(_TICK_INTERVAL_S)
        except Exception:
            log.exception("session: error mid-stream")

        try:
            final = streamer.stop()
            if final.finalized_delta:
                inject.type_text(final.finalized_delta)
        except Exception:
            log.exception("session: error during stop")

        self._ui_q.put(_UIMsg(kind="hide"))
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
            self._shutdown.set()
        else:
            conn.sendall(
                protocol.encode({"ok": False, "error": f"unknown cmd: {cmd}"})
            )

    # ----------------- helpers -----------------

    def _setup_logging(self) -> None:
        log_path = paths.LOG_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Avoid double-configuring if the daemon is restarted in-process.
        root_log = logging.getLogger()
        if root_log.handlers:
            return
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.FileHandler(log_path),
                logging.StreamHandler(),
            ],
        )

"""Tiny line-oriented protocol for daemon ↔ client IPC over a Unix socket.

Each request and response is a single line of JSON terminated with \\n.
We use sockets (not signals or shared memory) for two reasons:
  - Bidirectional: the client can read status (`ping`, `status`) and
    error messages back from the daemon.
  - Decoupled: Hammerspoon, the CLI, or any future glue (a Stream Deck
    plugin, a Shortcut, …) can drive Shout the same way.
"""

from __future__ import annotations

import json
import socket
from typing import Any


# Commands the daemon understands. Keeping these as simple strings so
# Hammerspoon can send them without depending on a JSON library.
CMD_START = "start"
CMD_STOP = "stop"
CMD_PING = "ping"
CMD_QUIT = "quit"


def encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line.decode("utf-8"))


def send_command(socket_path: str, command: str, timeout: float = 2.0) -> dict[str, Any]:
    """Connect, send one command, read one response, close. Raises on
    connection failure (caller should treat as 'daemon not running')."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_path)
        s.sendall(encode({"cmd": command}))
        # Read until newline.
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
        return decode(buf) if buf else {"ok": False, "error": "empty response"}
    finally:
        s.close()

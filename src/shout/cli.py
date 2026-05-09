"""Shout CLI: subcommands for daemon, ptt control, install assist, diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import paths, protocol


# The Caps Lock → F19 hidutil mapping. HID Usage Page 7 (Keyboard) +
# usage code: 0x39 = Caps Lock, 0x6E = F19. Apple's hidutil represents
# the (page, code) pair as one 64-bit integer with the page in the high
# half. These constants must agree with the values baked into
# launchd/com.greg.shout.capslock-remap.plist.
_HIDUTIL_CAPS_TO_F19 = (
    '{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0x700000039,'
    '"HIDKeyboardModifierMappingDst":0x70000006E}]}'
)
_HID_CAPS_LOCK_DEC = 30064771129  # 0x700000039
_HID_F19_DEC = 30064771182  # 0x70000006E


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="Run the long-lived transcription daemon (foreground)")

    sub.add_parser("start", help="Tell the daemon to begin a PTT session")
    sub.add_parser("stop", help="Tell the daemon to end the current PTT session")
    sub.add_parser("ping", help="Check whether the daemon is reachable")
    sub.add_parser("quit", help="Ask the daemon to shut down")

    p_setup = sub.add_parser(
        "setup",
        help="Remap Caps Lock → F19 (hidutil), install Hammerspoon Lua, "
        "and persist the remap as a login-time LaunchAgent",
    )
    p_setup.add_argument(
        "--launchagent",
        action="store_true",
        help="Also install ~/Library/LaunchAgents/com.greg.shout.plist for "
        "the daemon (skip when using `brew services start shout`)",
    )

    sub.add_parser("doctor", help="Print a diagnostic of the install")
    sub.add_parser("bench", help="Run the cold-start benchmark")

    args = parser.parse_args(argv)

    if args.cmd == "daemon":
        from .daemon import Daemon

        return Daemon().run()
    if args.cmd in ("start", "stop", "ping", "quit"):
        return _send(args.cmd)
    if args.cmd == "setup":
        return _setup(install_launchagent=args.launchagent)
    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "bench":
        return _bench()
    return 1


# ----------------- subcommands -----------------


def _send(cmd: str) -> int:
    try:
        resp = protocol.send_command(str(paths.SOCKET_PATH), cmd)
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
        print(
            "shout daemon is not reachable. Start it with `shout daemon` "
            "(foreground) or `brew services start shout` (background).",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


def _setup(install_launchagent: bool) -> int:
    paths.ensure_app_support()
    installed: list[str] = []

    # 1. Apply the hidutil remap right now so the Caps Lock → F19 path
    #    is live in the current session — no logout required. The
    #    LaunchAgent below makes it stick across reboots.
    try:
        subprocess.run(
            ["/usr/bin/hidutil", "property", "--set", _HIDUTIL_CAPS_TO_F19],
            check=True,
            capture_output=True,
        )
        installed.append("hidutil mapping → caps_lock → f19 (active now)")
    except subprocess.CalledProcessError as e:
        print(
            f"hidutil failed: {e.stderr.decode().strip()}",
            file=sys.stderr,
        )
        return 1

    # 2. Drop a LaunchAgent that re-applies the mapping at every login.
    remap_agent_dst = paths.capslock_remap_agent_path()
    remap_agent_dst.parent.mkdir(parents=True, exist_ok=True)
    remap_agent_src = _resource_path(
        "launchd", "com.greg.shout.capslock-remap.plist"
    )
    shutil.copy(remap_agent_src, remap_agent_dst)
    # Reload it if already present so a config change takes effect.
    subprocess.run(
        ["/bin/launchctl", "unload", str(remap_agent_dst)],
        capture_output=True,
    )
    subprocess.run(
        ["/bin/launchctl", "load", str(remap_agent_dst)],
        capture_output=True,
    )
    installed.append(f"capslock LaunchAgent → {remap_agent_dst}")

    # 3. Optional daemon launch agent for non-brew installs.
    if install_launchagent:
        agent_src = _resource_path("launchd", "com.greg.shout.plist")
        agent_dst = paths.launch_agent_path()
        agent_dst.parent.mkdir(parents=True, exist_ok=True)
        text = Path(agent_src).read_text().replace(
            "{{SHOUT_BIN}}", _shout_binary_path()
        )
        agent_dst.write_text(text)
        installed.append(f"daemon LaunchAgent  → {agent_dst}")

    print("Installed:")
    for line in installed:
        print(f"  {line}")
    print()
    print("Next steps:")
    print("  1. Grant the Shout daemon permissions (System Settings → Privacy):")
    print("     • Microphone     — sounddevice mic capture")
    print("     • Accessibility  — both for typing at the cursor AND for")
    print("                        the F19 event tap")
    if install_launchagent:
        print("  2. Start the daemon:")
        print(f"     launchctl load {paths.launch_agent_path()}")
    else:
        print("  2. Start the daemon: brew services start shout")
    print("  3. Run `shout doctor` to confirm everything is wired up.")
    return 0


def _doctor() -> int:
    rows = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        rows.append((mark, label, detail))

    # AppKit (via pyobjc) drives the floating overlay. If this fails,
    # the daemon will crash at startup with no visible recovery.
    try:
        import AppKit  # noqa: F401

        ui_ok = True
        ui_detail = ""
    except ImportError as e:
        ui_ok = False
        ui_detail = f"({e})"
    check("AppKit importable", ui_ok, ui_detail)

    # Microphone permission. Without this, every sample sounddevice
    # produces is zero — see permissions.py.
    from . import permissions as _perms
    mic = _perms.microphone_status()
    check(
        "Microphone permission granted",
        mic == "authorized",
        f"({mic})",
    )

    # Accessibility powers both the F19 event tap and CGEvent text
    # injection.
    ax = _perms.accessibility_trusted()
    check("Accessibility permission granted", ax)

    # Caps Lock → F19 hidutil remap currently active?
    remap_active, remap_detail = _hidutil_caps_to_f19_active()
    check("hidutil caps_lock → f19 active", remap_active, remap_detail)

    # The LaunchAgent re-applies the remap at every login.
    agent_path = paths.capslock_remap_agent_path()
    check(
        "capslock-remap LaunchAgent installed",
        agent_path.exists(),
        str(agent_path),
    )

    socket_alive = False
    try:
        resp = protocol.send_command(str(paths.SOCKET_PATH), protocol.CMD_PING)
        socket_alive = bool(resp.get("ok"))
    except Exception:
        pass
    check("Daemon reachable on socket", socket_alive, str(paths.SOCKET_PATH))

    width = max(len(label) for _, label, _ in rows) + 2
    for mark, label, detail in rows:
        print(f"  {mark}  {label:<{width}} {detail}")
    failed = sum(1 for mark, _, _ in rows if mark == "✗")
    print()
    if failed:
        print(f"{failed} check(s) failed. Run `shout setup` to install missing pieces.")
        return 1
    print("All checks passed.")
    return 0


def _bench() -> int:
    repo_root = _repo_root()
    bench = repo_root / "scripts" / "bench-cold-start.py"
    if not bench.exists():
        print(
            "bench script not found — `shout bench` only works in a dev checkout.",
            file=sys.stderr,
        )
        return 1
    os.execvp(sys.executable, [sys.executable, str(bench)])
    return 0  # unreachable


# ----------------- helpers -----------------


def _hidutil_caps_to_f19_active() -> tuple[bool, str]:
    """Inspect the current hidutil UserKeyMapping for our caps→F19 entry.

    hidutil emits an NSDictionary description (not JSON), so we just
    string-match the decimal forms of the page+code pair."""
    try:
        result = subprocess.run(
            ["/usr/bin/hidutil", "property", "--get", "UserKeyMapping"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False, f"({e})"

    out = result.stdout
    src = str(_HID_CAPS_LOCK_DEC)
    dst = str(_HID_F19_DEC)
    if src in out and dst in out:
        return True, "(via hidutil)"
    if out.strip() in ("", "(null)"):
        return False, "(no UserKeyMapping set)"
    return False, "(UserKeyMapping has other entries but not caps→f19)"


def _resource_path(*parts: str) -> str:
    """Locate a packaged resource, falling back to the dev tree.

    When installed via brew/pip the configs are vendored into the wheel
    (see [tool.hatch.build.force-include] in pyproject.toml). When
    running from the source tree, we look in the repo root."""
    try:
        with resources.as_file(resources.files("shout").joinpath("_resources", *parts)) as p:
            if p.exists():
                return str(p)
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    repo_root = _repo_root()
    return str(repo_root / Path(*parts))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _shout_binary_path() -> str:
    found = shutil.which("shout")
    if found:
        return found
    return "/opt/homebrew/bin/shout"


if __name__ == "__main__":
    sys.exit(main())

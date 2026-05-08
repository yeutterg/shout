"""Shout CLI: subcommands for daemon, ptt control, install assist, diagnostics."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import socket
import sys
from importlib import resources
from pathlib import Path

from . import paths, protocol


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="Run the long-lived transcription daemon (foreground)")

    sub.add_parser("start", help="Tell the daemon to begin a PTT session")
    sub.add_parser("stop", help="Tell the daemon to end the current PTT session")
    sub.add_parser("ping", help="Check whether the daemon is reachable")
    sub.add_parser("quit", help="Ask the daemon to shut down")

    p_setup = sub.add_parser(
        "setup", help="Install the Hammerspoon Lua and Karabiner rule"
    )
    # Default: do NOT install our launchagent. brew services start shout
    # is the conventional path and creates its own homebrew.mxcl.shout
    # plist; if our plist is also installed, both daemons race to bind
    # the same Unix socket. Opt in only when running outside of brew.
    p_setup.add_argument(
        "--launchagent",
        action="store_true",
        help="Also install ~/Library/LaunchAgents/com.greg.shout.plist "
        "(skip when using `brew services start shout`)",
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
    warnings: list[str] = []

    karabiner_dst_dir = paths.karabiner_complex_mods_dir()
    if karabiner_dst_dir.parent.parent.exists():
        karabiner_dst_dir.mkdir(parents=True, exist_ok=True)
        karabiner_src = _resource_path("karabiner", "caps-to-f19.json")
        karabiner_dst = karabiner_dst_dir / "shout-caps-to-f19.json"
        shutil.copy(karabiner_src, karabiner_dst)
        installed.append(f"karabiner rule  → {karabiner_dst}")
        # Also patch the active Karabiner profile so the user does not
        # have to click through Karabiner UI → Complex Modifications →
        # Add rule. Karabiner watches karabiner.json with FSEvents and
        # reloads automatically.
        kb_config = paths.karabiner_complex_mods_dir().parent.parent / "karabiner.json"
        action = _enable_karabiner_rule(kb_config, Path(karabiner_src))
        if action:
            installed.append(f"karabiner config → {action}")
    else:
        warnings.append(
            "Karabiner-Elements is not installed (or has never been "
            "launched). Run `brew install --cask karabiner-elements`, "
            "open Karabiner once, then re-run `shout setup`."
        )

    hs_dir = paths.hammerspoon_config_dir()
    hs_dir.mkdir(parents=True, exist_ok=True)
    hs_src = _resource_path("hammerspoon", "shout.lua")
    hs_dst = hs_dir / "shout.lua"
    shutil.copy(hs_src, hs_dst)
    installed.append(f"hammerspoon lua → {hs_dst}")

    init_lua = hs_dir / "init.lua"
    require_line = 'require("shout")\n'
    if not init_lua.exists() or require_line.strip() not in init_lua.read_text():
        with init_lua.open("a") as f:
            f.write("\n-- Added by `shout setup`\n")
            f.write(require_line)
        installed.append(f"hammerspoon init → appended require to {init_lua}")

    if install_launchagent:
        agent_src = _resource_path("launchd", "com.greg.shout.plist")
        agent_dst = paths.launch_agent_path()
        agent_dst.parent.mkdir(parents=True, exist_ok=True)
        text = Path(agent_src).read_text().replace(
            "{{SHOUT_BIN}}", _shout_binary_path()
        )
        agent_dst.write_text(text)
        installed.append(f"launch agent    → {agent_dst}")

    print("Installed:")
    for line in installed:
        print(f"  {line}")
    if warnings:
        print()
        print("Warnings:")
        for w in warnings:
            print(f"  ! {w}")
    print()
    print("Next steps:")
    print("  1. Reload Hammerspoon (menu bar icon → Reload Config), then")
    print("     allow Accessibility when prompted.")
    print("  2. Grant the Shout daemon permissions (System Settings → Privacy):")
    print("     • Microphone     — for the daemon's Python interpreter")
    print("     • Accessibility  — for typing at the cursor")
    if install_launchagent:
        print("  3. Start the daemon:")
        print(f"     launchctl load {paths.launch_agent_path()}")
    else:
        print("  3. Start the daemon: brew services start shout")
    print("  4. Run `shout doctor` to confirm everything is wired up.")
    return 0


def _doctor() -> int:
    rows = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        rows.append((mark, label, detail))

    # tkinter is bundled with stock Python builds but Homebrew's
    # python@3.12 ships without it (python-tk@3.12 is a separate formula).
    try:
        import tkinter  # noqa: F401

        tk_ok = True
        tk_detail = ""
    except ImportError as e:
        tk_ok = False
        tk_detail = f"({e})"
    check("tkinter importable", tk_ok, tk_detail)

    check(
        "Karabiner-Elements installed",
        Path("/Applications/Karabiner-Elements.app").exists(),
    )
    check(
        "Hammerspoon installed",
        Path("/Applications/Hammerspoon.app").exists(),
    )
    karabiner_rule = (
        paths.karabiner_complex_mods_dir() / "shout-caps-to-f19.json"
    )
    check("Karabiner rule file present", karabiner_rule.exists(), str(karabiner_rule))

    # The rule file alone is just a library entry; the *active* config
    # has to also reference it for Karabiner to actually apply it.
    kb_config = paths.karabiner_complex_mods_dir().parent.parent / "karabiner.json"
    rule_active = False
    if kb_config.exists():
        try:
            data = json.loads(kb_config.read_text())
            for prof in data.get("profiles", []):
                for r in prof.get("complex_modifications", {}).get("rules", []):
                    if "Shout" in (r.get("description") or ""):
                        rule_active = True
                        break
                if rule_active:
                    break
        except json.JSONDecodeError:
            pass
    check(
        "Karabiner rule enabled in active profile",
        rule_active,
        str(kb_config) if kb_config.exists() else "(karabiner.json missing)",
    )

    hs_lua = paths.hammerspoon_config_dir() / "shout.lua"
    check("Hammerspoon shout.lua installed", hs_lua.exists(), str(hs_lua))

    init_lua = paths.hammerspoon_config_dir() / "init.lua"
    init_ok = init_lua.exists() and 'require("shout")' in init_lua.read_text()
    check("Hammerspoon init.lua requires shout", init_ok, str(init_lua))

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


def _enable_karabiner_rule(config_path: Path, rule_file: Path) -> str | None:
    """Inject our rule into the active Karabiner profile.

    Returns a one-line summary of what changed, or None if the rule was
    already enabled.

    The rule library file under ~/.config/karabiner/assets/complex_modifications
    is just an *available* rule — Karabiner doesn't apply rules from
    there unless they are also present in the active profile's
    `complex_modifications.rules` list (which is what the UI's
    "Add rule" button does behind the scenes).

    If `karabiner.json` does not yet exist (the user hasn't opened
    Karabiner-Elements Settings to create it), we write a minimal
    default config with our rule pre-enabled. If it does exist, we
    patch the selected profile in place, deduplicating by rule
    description.
    """
    rule_doc = json.loads(rule_file.read_text())
    new_rules = rule_doc.get("rules", [])
    if not new_rules:
        return None

    if config_path.exists():
        config = json.loads(config_path.read_text())
        action = "patched"
    else:
        config = _karabiner_default_config()
        action = "created"

    profiles = config.setdefault("profiles", [])
    if not profiles:
        profiles.append(_karabiner_default_profile())

    target = next((p for p in profiles if p.get("selected")), profiles[0])
    cm = target.setdefault("complex_modifications", {"rules": []})
    cm.setdefault("rules", [])

    existing_descs = {r.get("description") for r in cm["rules"]}
    added = []
    for r in new_rules:
        if r.get("description") in existing_descs:
            continue
        cm["rules"].append(copy.deepcopy(r))
        added.append(r.get("description", "(no description)"))

    if not added and action == "patched":
        return None

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=4) + "\n")
    if added:
        return f"{action} {config_path} (enabled: {', '.join(added)})"
    return f"{action} {config_path}"


def _karabiner_default_profile() -> dict:
    return {
        "name": "Default profile",
        "selected": True,
        "complex_modifications": {"rules": []},
        "simple_modifications": [],
        "fn_function_keys": [],
        "devices": [],
        "virtual_hid_keyboard": {"country_code": 0},
    }


def _karabiner_default_config() -> dict:
    return {
        "profiles": [_karabiner_default_profile()],
        "global": {
            "check_for_updates_on_startup": True,
            "show_in_menu_bar": True,
            "show_profile_name_in_menu_bar": False,
        },
    }


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

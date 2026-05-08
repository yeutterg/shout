# Shout

Local-first push-to-talk dictation for macOS, powered by [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) running on Apple Silicon via [MLX](https://github.com/ml-explore/mlx).

Hold Caps Lock, speak, see the words appear at the cursor. Release Caps Lock, the session ends. Triple-tap Caps Lock and the OS toggles real Caps Lock the way it always did.

This is the v0 of a Wispr Flow alternative. Streaming transcription, finalized tokens typed at the cursor, a tentative-token strip pinned to the bottom of the screen.

## Status

v0. Apple Silicon only. English + ~24 other European languages with auto-detect (Parakeet TDT v3 is multilingual).

## How it works

```
Caps Lock hold    →  hidutil: Caps Lock → F19 (HID-layer remap, no daemon)
                                       ↓
                                Shout daemon Quartz event tap fires;
                                worker opens mic, feeds parakeet-mlx,
                                types finalized tokens at cursor,
                                shows rolling history+draft in floating overlay
Caps Lock release →  daemon ends session, flushes any unfinalized text
Caps Lock 3× tap  →  daemon synthesizes a real Caps Lock keystroke
                       (CGEventPost bypasses the hidutil remap;
                        OS toggles state, LED lights)
```

The daemon stays loaded in memory (the Parakeet model takes ~1.3 s to load and ~2 GB of RAM). Each push-to-talk session reuses the loaded model, so there is no per-session warm-up delay — typically <1 s from speech to first finalized token.

## Install

Shout is distributed as a Homebrew formula. v0.1.0 is not yet tagged with a stable tarball, so use `--HEAD`:

```bash
brew tap yeutterg/shout https://github.com/yeutterg/shout
brew install --HEAD yeutterg/shout/shout

shout setup
brew services start shout
```

That's it for installation. **No Karabiner. No Hammerspoon.** `shout setup`:

- runs `hidutil` to remap Caps Lock → F19 in the current session,
- writes a small LaunchAgent to re-apply that remap at every login.

The Shout daemon does the rest: it watches for F19 via a Quartz event tap, opens the mic, runs streaming inference, types text at the cursor, and shows a rolling overlay with the most recent finalized text plus the current draft.

### Permissions (one-time)

The daemon's Python interpreter (`/opt/homebrew/opt/shout/libexec/venv/bin/python3.12`) needs two permissions in System Settings → Privacy & Security:

| Permission | Why |
| --- | --- |
| Microphone | sounddevice mic capture |
| Accessibility | Quartz CGEvent typing at cursor **and** the F19 event tap |

The Microphone prompt typically pops on first hold. Accessibility usually has to be added by hand:

1. System Settings → Privacy & Security → Accessibility → click `+`
2. ⌘⇧G → paste `/opt/homebrew/opt/shout/libexec/venv/bin/python3.12`
3. Add it, toggle it on
4. `brew services restart shout` so the daemon inherits the new permission

Then run `shout doctor`. Every check should be ✓.

## CLI

```
shout daemon    # foreground daemon (use `brew services start shout` for the login-agent path)
shout start     # ask the running daemon to begin a PTT session
shout stop      # ask the running daemon to end the current session
shout ping      # health check; reports whether the model is loaded
shout quit      # ask the daemon to shut down
shout setup     # apply hidutil remap + install LaunchAgent
shout doctor    # diagnose the install
shout bench     # run the cold-start benchmark (development only)
```

## Architecture decisions

**Why a long-running daemon, not spawn-per-session.** Cold-start of a fresh Python interpreter + parakeet-mlx import + model load + first MLX encoder pass measured 2.3 – 4.4 seconds (`scripts/bench-cold-start.py`). That's well over the v0 acceptance criterion of <1.5 s, so the daemon variant won. Trade-off: ~2 GB resident RAM while idle.

**Why hidutil, not Karabiner.** Original design used Karabiner-Elements to remap Caps Lock → F19. Karabiner v15+ uses Apple's `SMAppService` API for its privileged daemon registration; the user has to approve a system extension in Privacy & Security AND toggle two background-activity entries in Login Items & Extensions, and even then DriverKit activation can stall. Apple's built-in `hidutil` does the same HID-layer remap with one shell command and zero permissions. We re-apply it at login via a tiny LaunchAgent.

**Why no Hammerspoon either.** Original design used Hammerspoon to bind F19 hold/release and to detect triple-tap for the real-CapsLock fallback. The Shout daemon already needs Accessibility permission for Quartz CGEvent typing — that same permission lets us tap F19 directly via `Quartz.CGEventTapCreate`. One process, one permission grant, no Lua, no `init.lua` append.

**Why `(256, 8)` for `context_size`, not `(256, 0)`.** The original PRD asked for zero right-context (lowest possible latency). `parakeet-mlx ≥ 0.5` rejects a zero right-context. With 8 encoder frames (~640 ms) of right context the streamer still finalizes well inside the 1.5 s budget while leaving enough lookahead to keep finalization stable.

**Why MLX threading runs the model on a worker thread.** MLX has per-thread default-stream state. The model has to live on the same thread that calls `add_audio`. Audio capture stays on PortAudio's internal thread (just `numpy.copy()` into a queue), and the worker thread does the inference; the main thread is reserved for the Tk event loop and the overlay window.

**Why CGEvent unicode injection, not paste-buffer.** Pasting collides with whatever the user has copied. CGEvent unicode keystrokes work in any focused text field — Cursor, Slack, terminals, browsers, native apps — and don't touch the clipboard.

**Why no backspace-on-revision.** Finalized-only insert sidesteps the cross-app whack-a-mole (Slack autocomplete, Cursor inline completion, terminal scrollback) you'd hit if Shout backspaced over revised tokens. Cost: ~0.6-1.5 s lag waiting for finalization. Acceptable for v0.

## Layout

```
shout/
├── Formula/shout.rb              Homebrew formula (personal tap)
├── pyproject.toml                Hatchling-built Python package
├── scripts/bench-cold-start.py   Latency benchmark
├── src/shout/
│   ├── cli.py                    `shout` argv dispatch + setup/doctor
│   ├── daemon.py                 Long-running daemon (Tk + worker + socket + hotkey threads)
│   ├── hotkey.py                 Quartz CGEventTap for F19 + triple-tap → real Caps Lock
│   ├── stream.py                 parakeet-mlx + sounddevice loop
│   ├── inject.py                 Quartz CGEvent unicode keystrokes
│   ├── overlay.py                Tkinter floating overlay (rolling history + draft)
│   ├── protocol.py               Tiny line-JSON protocol over Unix socket
│   └── paths.py                  Filesystem locations
└── launchd/
    ├── com.greg.shout.plist               Login-agent template (substituted by `shout setup --launchagent`)
    └── com.greg.shout.capslock-remap.plist Login-agent that runs `hidutil` to apply Caps Lock → F19
```

## Development

```bash
git clone https://github.com/yeutterg/shout && cd shout
brew install python-tk@3.12 uv
uv sync --group dev

# Run the daemon in the foreground:
uv run shout daemon

# In another terminal, drive a session:
uv run shout ping
uv run shout start
uv run shout stop
```

The cold-start benchmark prints first/median/min/max for each phase and a verdict against the 1.5 s acceptance criterion:

```bash
uv run python scripts/bench-cold-start.py --runs 5 --output bench-results/cold-start.json
```

## Roadmap

This README covers v0 (Track 1 of the broader voice stack). The full plan — including Anarlog plugins for meeting transcription — lives in the PRD ([Voice Stack: Parakeet + Anarlog Plugins](https://github.com/yeutterg/shout/blob/master/docs/PRD.md), or whatever path the user resolves to). v1 polish layer: Swift menubar wrapper around the same Python inference subprocess, with NSPanel replacing Tk and a native menubar app replacing this Python event-tap.

## License

MIT.

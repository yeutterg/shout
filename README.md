# Shout

Local-first push-to-talk dictation for macOS, powered by [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) running on Apple Silicon via [MLX](https://github.com/ml-explore/mlx).

Hold Caps Lock, speak, see the words appear at the cursor. Release Caps Lock, the session ends. Triple-tap Caps Lock and the OS toggles real Caps Lock the way it always did.

This is the v0 of a Wispr Flow alternative. Streaming transcription, finalized tokens typed at the cursor, a tentative-token strip pinned to the bottom of the screen.

## Status

v0. Apple Silicon only. English + ~24 other European languages with auto-detect (Parakeet TDT v3 is multilingual).

## How it works

```
Caps Lock hold    →  Karabiner: Caps Lock → F19  →  Hammerspoon F19 down
                                                          ↓
                                                    `shout start` over Unix socket
                                                          ↓
                                                    Shout daemon: open mic,
                                                    feed parakeet-mlx,
                                                    type finalized tokens at cursor,
                                                    show drafts in floating overlay
Caps Lock release →  Hammerspoon F19 up  →  `shout stop`
Caps Lock 3× tap  →  Hammerspoon synthesizes a real Caps Lock keystroke
                       (passes above Karabiner's HID layer; OS toggles state, LED lights)
```

The daemon stays loaded in memory (the Parakeet model takes ~1.3 s to load and ~2 GB of RAM). Each push-to-talk session reuses the loaded model, so there is no per-session warm-up delay — typically <1 s from speech to first finalized token.

## Install

Shout is distributed as a Homebrew formula. Once the repo is pushed to GitHub:

```bash
brew tap yeutterg/shout https://github.com/yeutterg/shout
brew install --HEAD yeutterg/shout/shout

# Until v0.1.0 is tagged with a real tarball SHA, --HEAD is the install path.

# GUI prerequisites — Homebrew formulas can't depend on casks, so install these manually:
brew install --cask karabiner-elements hammerspoon

# Wire up the configs (Karabiner rule, Hammerspoon Lua, launchd agent):
shout setup

# Start the daemon as a login agent:
brew services start shout
```

After that, finish the one-time permission grants:

| Permission | Where | Why |
| --- | --- | --- |
| Microphone | System Settings → Privacy & Security → Microphone | sounddevice mic capture |
| Accessibility | System Settings → Privacy & Security → Accessibility | Quartz CGEvent typing at cursor |
| Input Monitoring | System Settings → Privacy & Security → Input Monitoring | Hammerspoon listens for F19 |
| Karabiner system extension | System Settings → Privacy & Security | Caps Lock → F19 HID-layer remap |

Karabiner-Elements also requires a one-click step: open Karabiner-Elements → Complex Modifications → Add rule → enable "Shout: Caps Lock → F19 (push-to-talk)".

Then run `shout doctor`. Every check should pass.

## CLI

```
shout daemon    # foreground daemon (use `brew services start shout` for the login-agent path)
shout start     # ask the running daemon to begin a PTT session
shout stop      # ask the running daemon to end the current session
shout ping      # health check; reports whether the model is loaded
shout quit      # ask the daemon to shut down
shout setup     # copy the Karabiner rule + Hammerspoon Lua + launchd plist into place
shout doctor    # diagnose the install
shout bench     # run the cold-start benchmark (development only)
```

## Architecture decisions

**Why a long-running daemon, not spawn-per-session.** Cold-start of a fresh Python interpreter + parakeet-mlx import + model load + first MLX encoder pass measured 2.3 – 4.4 seconds (`scripts/bench-cold-start.py`). That's well over the v0 acceptance criterion of <1.5 s, so the daemon variant won. Trade-off: ~2 GB resident RAM while idle.

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
│   ├── daemon.py                 Long-running daemon (Tk + worker + socket)
│   ├── stream.py                 parakeet-mlx + sounddevice loop
│   ├── inject.py                 Quartz CGEvent unicode keystrokes
│   ├── overlay.py                Tkinter floating overlay
│   ├── protocol.py               Tiny line-JSON protocol over Unix socket
│   └── paths.py                  Filesystem locations
├── hammerspoon/shout.lua         F19 hold/release + triple-tap → real CapsLock
├── karabiner/caps-to-f19.json    Complex-modification rule
└── launchd/com.greg.shout.plist  Login-agent template (substituted by setup)
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

This README covers v0 (Track 1 of the broader voice stack). The full plan — including Anarlog plugins for meeting transcription — lives in the PRD ([Voice Stack: Parakeet + Anarlog Plugins](https://github.com/yeutterg/shout/blob/master/docs/PRD.md), or whatever path the user resolves to). v1 polish layer: Swift menubar wrapper around the same Python inference subprocess, with NSPanel replacing Tk and the modern `KeyboardShortcuts` Swift package replacing Hammerspoon.

## License

MIT.

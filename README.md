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

Shout is distributed as a Homebrew formula. v0.1.0 is not yet tagged with a stable tarball, so use `--HEAD`:

```bash
brew tap yeutterg/shout https://github.com/yeutterg/shout
brew install --HEAD yeutterg/shout/shout

# Hammerspoon (no sudo needed):
brew install --cask hammerspoon
```

### Karabiner-Elements requires an interactive sudo prompt

The `karabiner-elements` cask installs a system extension and **requires `sudo` from a real terminal** — Homebrew shells out to `/usr/sbin/installer`, which fails with `sudo: a terminal is required to read the password` if invoked from a non-TTY context (a Claude Code `! command` prompt, an editor task runner, a `launchctl`-spawned shell, etc.).

Pick whichever path is convenient:

```bash
# Option A — from a real terminal (Terminal.app, iTerm2, Ghostty, …)
brew install --cask karabiner-elements
# sudo will prompt; type your password.

# Option B — let macOS's GUI installer prompt for the password
# Homebrew's `--cask` step downloads the .pkg even when the install fails;
# you can run it directly:
open /opt/homebrew/Caskroom/karabiner-elements/*/Karabiner-Elements.pkg
```

### First launch of Karabiner-Elements

Open Karabiner-Elements once from Spotlight. Karabiner uses a daemon-plus-helper architecture and asks for permissions in three separate places. Approve them all:

1. **Accessibility popup for `Karabiner-Core-Service.app`.** Click "Open System Settings" → enable the toggle for Karabiner-Core-Service.
2. **Karabiner-Elements Settings → "Background services"** wizard. Click through to choose background services. This kicks you into System Settings.
3. **System Settings → General → Login Items & Extensions** → under "Allow in the Background", enable both:
   - `Karabiner-Elements Non-Privileged Agents v2.app`
   - `Karabiner-Elements Privileged Daemons v2.app`

Without all three, the Caps Lock → F19 remap silently does nothing. After Karabiner is fully approved, `~/.config/karabiner/` exists and `shout setup` can drop the rule in.

### Wire up the configs and start the daemon

```bash
shout setup
brew services start shout
```

`shout setup` copies the Karabiner rule, the Hammerspoon Lua, and appends `require("shout")` to `~/.hammerspoon/init.lua`. It does NOT install a launch agent — `brew services` handles that. (Use `shout setup --launchagent` only when running outside brew.)

### Permissions (one-time)

| Permission | Granted to | Why | How |
| --- | --- | --- | --- |
| Microphone | the brew-installed Python (`/opt/homebrew/opt/shout/libexec/venv/bin/python3.12`) | sounddevice mic capture | macOS prompts on first PTT |
| Accessibility | same Python binary | Quartz CGEvent typing at cursor | System Settings → Privacy & Security → Accessibility → `+` → ⌘⇧G to paste the path above |
| Input Monitoring | Hammerspoon | listens for F19 | macOS prompts on Hammerspoon launch |
| System Extension | Karabiner-Elements | Caps Lock → F19 HID-layer remap | Karabiner prompts on first launch |

After granting Accessibility manually, **restart the daemon** so it inherits the new permission: `brew services restart shout`.

Then in Karabiner-Elements: **Complex Modifications** → **Add rule** → enable **"Shout: Caps Lock → F19 (push-to-talk)"**. Reload Hammerspoon (menu bar → Reload Config) so it picks up `shout.lua`.

Run `shout doctor`. Every check should be ✓.

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

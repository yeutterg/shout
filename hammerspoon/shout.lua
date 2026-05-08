-- Shout: F19 push-to-talk handler.
--
-- Karabiner-Elements remaps Caps Lock → F19 at the HID layer, so the OS
-- never sees a Caps Lock state change and the LED stays off. We listen
-- here for F19 and drive the Shout daemon.
--
-- Behavior:
--   F19 down  →  `shout start`
--   F19 up    →  `shout stop`
--   F19 tap × 3 within 400 ms  →  synthesize a real Caps Lock keystroke
--                                  (passes above Karabiner's HID layer),
--                                  so the OS toggles Caps Lock state for
--                                  real, lighting the LED.
--
-- Drop this file into ~/.hammerspoon/shout.lua and add `require("shout")`
-- to ~/.hammerspoon/init.lua. `shout setup` does both.

local M = {}

local SHOUT_BIN = nil  -- resolved lazily; first call to hs.execute("which shout")
local TRIPLE_TAP_WINDOW_MS = 400
local tap_history = {}

local function shout_bin()
  if SHOUT_BIN then return SHOUT_BIN end
  local out, ok = hs.execute("/usr/bin/which shout || echo /opt/homebrew/bin/shout", true)
  SHOUT_BIN = (out or "/opt/homebrew/bin/shout"):gsub("%s+$", "")
  return SHOUT_BIN
end

local function send(cmd)
  -- Fire-and-forget; the daemon responds quickly and we do not block on it.
  hs.task.new(shout_bin(), nil, { cmd }):start()
end

local function record_tap()
  local now = hs.timer.secondsSinceEpoch() * 1000
  table.insert(tap_history, now)
  while #tap_history > 0 and (now - tap_history[1]) > TRIPLE_TAP_WINDOW_MS do
    table.remove(tap_history, 1)
  end
  return #tap_history >= 3
end

local function on_press()
  if record_tap() then
    -- Triple-tap detected. Cancel any session-in-flight and emit a real
    -- Caps Lock keystroke. Hammerspoon's keyStroke is synthesized above
    -- the Karabiner HID intercept, so the OS processes it as an
    -- ordinary Caps Lock toggle.
    tap_history = {}
    send("stop")
    hs.eventtap.keyStroke({}, "capslock", 0)
    return
  end
  send("start")
end

local function on_release()
  send("stop")
end

hs.hotkey.bind({}, "f19", on_press, on_release)

hs.alert.show("Shout F19 PTT loaded", 1)

return M

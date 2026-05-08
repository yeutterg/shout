-- Shout: F19 push-to-talk handler.
--
-- Karabiner-Elements remaps Caps Lock → F19 at the HID layer, so the OS
-- never sees a Caps Lock state change and the LED stays off. We listen
-- here for F19 and drive the Shout daemon.
--
-- Behavior:
--   F19 hold ≥ HOLD_THRESHOLD_MS  →  `shout start`; release → `shout stop`
--   F19 quick tap                  →  no-op (debounced)
--   F19 tap × 3 within TRIPLE_TAP_WINDOW_MS  →  synthesize a real Caps
--     Lock keystroke (passes above Karabiner's HID layer), so the OS
--     toggles Caps Lock state for real, lighting the LED.
--
-- The HOLD_THRESHOLD_MS debounce is important. Without it, the two
-- preliminary taps of a triple-tap sequence each fire start+stop, and
-- the user sees the overlay flicker twice during what they intended as
-- "toggle Caps Lock for real." With the debounce, taps shorter than
-- the threshold are silent.
--
-- Drop this file into ~/.hammerspoon/shout.lua and add `require("shout")`
-- to ~/.hammerspoon/init.lua. `shout setup` does both.

local M = {}

local SHOUT_BIN = nil  -- resolved lazily on first send()
local TRIPLE_TAP_WINDOW_MS = 400
local HOLD_THRESHOLD_MS = 150
local tap_history = {}
local pending_start = nil   -- hs.timer that fires `shout start` on hold
local session_active = false

local function shout_bin()
  if SHOUT_BIN then return SHOUT_BIN end
  local out = hs.execute("/usr/bin/which shout || echo /opt/homebrew/bin/shout", true)
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

local function cancel_pending_start()
  if pending_start then
    pending_start:stop()
    pending_start = nil
  end
end

local function on_press()
  if record_tap() then
    -- Triple-tap detected. Cancel any pending start and emit a real
    -- Caps Lock keystroke. Hammerspoon's keyStroke is synthesized above
    -- the Karabiner HID intercept, so the OS processes it as an
    -- ordinary Caps Lock toggle.
    tap_history = {}
    cancel_pending_start()
    if session_active then
      send("stop")
      session_active = false
    end
    hs.eventtap.keyStroke({}, "capslock", 0)
    return
  end
  -- Defer the actual `shout start` until the key has been held past
  -- the threshold. A quick tap (release before threshold) cancels.
  cancel_pending_start()
  pending_start = hs.timer.doAfter(HOLD_THRESHOLD_MS / 1000, function()
    pending_start = nil
    session_active = true
    send("start")
  end)
end

local function on_release()
  if pending_start then
    -- Released before hold threshold: a tap, not a hold. Don't start.
    cancel_pending_start()
    return
  end
  if session_active then
    send("stop")
    session_active = false
  end
end

hs.hotkey.bind({}, "f19", on_press, on_release)

hs.alert.show("Shout F19 PTT loaded", 1)

return M

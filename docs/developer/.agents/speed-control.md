# Keyboard fly-speed controls

## Goal

Let a user alter the persistent base speed for free-flight cave movement with
the keyboard, while retaining the existing mouse-wheel behavior and temporary
Shift speed boost.

## Existing behavior to preserve

- `CaveViewerWindow.on_mouse_scroll_event()` calls
  `FlyCamera.adjust_speed(y_offset)`.
- `FlyCamera.adjust_speed()` changes `move_speed` multiplicatively by
  `1.1 ** step` and clamps it to `0.1` through `200.0` source-coordinate
  units per second. Map coordinate units are source-dependent; do not label
  them as metres, feet, or another real-world unit.
- Holding Shift is a separate, temporary `3x` multiplier applied only while
  moving; it must not alter the stored base speed.
- A newly loaded map creates a new `FlyCamera` with the existing default base
  speed. Do not add preference persistence in the first version.

## Key design

- `-` (the dash/minus key) decreases the base fly speed by one speed step.
- `=` increases the base fly speed by one speed step. It is adjacent to `-`
  and requires no modifier, avoiding the temporary Shift speed boost.
- Recognize equivalent keypad minus/plus aliases where available.
- Ignore persistent speed-adjustment shortcuts while Shift is held; Shift
  remains exclusively the temporary movement-speed boost.
- A press and a backend key-repeat each apply one step, so holding a key makes
  a controlled sequence of adjustments.
- Do not use arrow keys, because they already drive keyboard look.

Both keys must call the same `FlyCamera.adjust_speed()` method as the scroll
wheel. Do not duplicate its multiplier or clamp in the window/input code.

## Implementation boundaries

1. Add a pure key-to-signed-step helper to `caveviewer.gui.viewer_input`.
   It should return `-1`, `+1`, or `None` and keep backend key aliases out of
   the OpenGL window owner.
2. Add a one-shot speed-hotkey handler to `CaveViewerWindow.on_key_event()`.
   Handle it before adding the key to `_keys_down`; speed keys are not
   continuous movement keys.
3. Route the signed step to `self.camera.adjust_speed(step)` only when a
   usable camera exists.
4. Update the Controls Overlay and the `FlyCamera` controls documentation to
   list both `- / =` and Scroll as fly-speed controls.
5. Do not show a numeric speed banner or unit-bearing feedback: the source
   map's coordinate units are not known. The Controls Overlay supplies the
   necessary shortcut discoverability.

## Input-state rules

- Respect the existing input-suppression and startup `Press Space` gate.
- Ignore speed changes while a Recorded Dive is paused, matching the current
  scroll-wheel behavior.
- During an active, unpaused Recorded Dive, do not cancel playback; a base
  speed change has no effect on the authoritative playback route, just as the
  current wheel adjustment does.
- Allow the feature during normal free flight, recording, and manual dive
  tracing.
- It must not change benchmark route speed or streaming/cache policy.

## Tests

- Unit-test the pure key mapping, including aliases and unknown keys.
- Test window routing for increase, decrease, key-repeat, no camera, input
  suppression, startup gating, and paused Recorded Dive behavior.
- Test that an active Recorded Dive is not stopped by a speed key.
- Test `FlyCamera`'s multiplicative adjustment and minimum/maximum clamp if
  that behavior is not already covered.
- Update the Controls Overlay copy test to include `- / =`, and remove the
  speed-banner rendering test.

Run the focused GUI tests first, then the full suite when practical.

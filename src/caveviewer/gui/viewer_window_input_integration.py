"""Backend input interpretation helpers for the viewer window."""

from __future__ import annotations

import time

import numpy as np
from moderngl_window.context.base import KeyModifiers

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui import recorded_dive, viewer_bookmarks, viewer_input
from caveviewer.gui.viewer_action_dispatch import ViewerKeyPressActions
from caveviewer.gui.viewer_capture_workflow import CaptureOwner

_LOG = get_logger("CaveViewer")

class ViewerWindowInputIntegration:
    """Methods executed by ``CaveViewerWindow`` on its owning callback thread."""

    def _resolve_key(self, keys, *candidate_names):
        """
        Different moderngl-window/pyglet versions have used different names
        for the same key (e.g. LEFT_CONTROL vs LEFT_CTRL). Rather than hard-
        code one name and risk another AttributeError crash on a different
        installed version, try each known alias in turn and cache whichever
        one actually exists on this version's Keys class.
        """
        cache = getattr(self, "_key_resolve_cache", None)
        if cache is None:
            cache = {}
            self._key_resolve_cache = cache
        return viewer_input.resolve_key(keys, *candidate_names, cache=cache)

    def _install_backend_modifier_probe(self) -> None:
        """Capture raw backend modifier bitmasks before they are reduced to shift/ctrl/alt."""
        handler = getattr(self.wnd, "_handle_modifiers", None)
        if not callable(handler):
            return

        def wrapped_handle_modifiers(mods):
            try:
                self._last_raw_modifiers = int(mods)
            except Exception:
                self._last_raw_modifiers = 0
            return handler(mods)

        self.wnd._handle_modifiers = wrapped_handle_modifiers

    def _raw_command_modifier_down(self) -> bool:
        raw_mods = int(getattr(self, "_last_raw_modifiers", 0) or 0)
        backend_module = type(self.wnd).__module__
        return viewer_input.raw_command_modifier_down(raw_mods, backend_module)

    def _key_is_down(self, keys, *candidate_names) -> bool:
        """Return True if any candidate key exists on this backend and is currently held."""
        return viewer_input.key_is_down(keys, self._keys_down, *candidate_names)

    def _resolve_key_optional(self, keys, *candidate_names):
        """Return key code if present on this backend, else None."""
        return viewer_input.resolve_key_optional(keys, *candidate_names)

    def _digit_for_key(self, keys, key) -> int | None:
        """Return bookmark slot (1..9) for a key press across backend key name variants."""
        return viewer_input.digit_for_key(keys, key)

    def _is_zero_key(self, keys, key) -> bool:
        """Check if the key is the 0 key across backend key name variants."""
        return viewer_input.is_zero_key(keys, key)

    def _command_is_down(self, modifiers: KeyModifiers) -> bool:
        keys = self.wnd.keys
        return viewer_input.command_is_down(
            modifiers,
            keys,
            self._keys_down,
            command_modifier_uses_control_fallback=(
                self._active_presentation_profile()
                .command_modifier_uses_control_fallback
            ),
            raw_command_down=self._raw_command_modifier_down(),
        )

    def _control_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if Control/Ctrl modifier key is currently down."""
        keys = self.wnd.keys
        return viewer_input.control_is_down(modifiers, keys, self._keys_down)

    def _shift_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if Shift modifier key is currently down."""
        keys = self.wnd.keys
        return viewer_input.shift_is_down(modifiers, keys, self._keys_down)

    def _bookmark_save_modifier_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if the platform-specific bookmark save modifier is down."""
        save_modifier = self._active_presentation_profile().bookmark_save_modifier
        return viewer_input.bookmark_save_modifier_is_down(
            save_modifier=save_modifier,
            command_down=self._command_is_down(modifiers),
            control_down=self._control_is_down(modifiers),
        )

    def _load_bookmarks(self) -> None:
        self._bookmarks = viewer_bookmarks.load_bookmarks(
            self._bookmarks_path,
            logger=_LOG,
        )

    def _save_bookmarks(self) -> None:
        viewer_bookmarks.save_bookmarks(
            self._bookmarks_path,
            self._bookmarks,
            logger=_LOG,
        )

    def _save_bookmark_slot(self, slot: int) -> None:
        if not self._has_map_loaded:
            return
        self._bookmarks[slot] = viewer_bookmarks.bookmark_from_camera(
            self.camera.position,
            yaw=self.camera.yaw,
            pitch=self.camera.pitch,
        )
        self._save_bookmarks()
        _LOG.info(f"Saved camera bookmark {slot}.")

    def _recall_bookmark_slot(self, slot: int) -> bool:
        if not self._has_map_loaded:
            return False
        data = self._bookmarks.get(slot)
        if not data:
            _LOG.info(f"Bookmark {slot} is empty.")
            return False

        if self._recorded_dive_is_active():
            self._stop_recorded_dive(reason="bookmark_recall")
        trace_pose_before_recall = self._manual_dive_trace_pose()
        pos = data["position"]
        self.camera.position = np.array([float(pos[0]), float(pos[1]), float(pos[2])], dtype=np.float64)
        self.camera.yaw = float(data["yaw"])
        pitch = float(data["pitch"])
        pitch_limit = getattr(self.camera, "_pitch_limit", None)
        if pitch_limit is not None:
            pitch = max(-float(pitch_limit), min(float(pitch_limit), pitch))
        self.camera.pitch = pitch
        self.camera.roll = 0.0  # Reset roll when loading a bookmark
        self._mark_manual_dive_trace_discontinuity(
            trace_pose_before_recall,
            reason="bookmark_recall",
        )

        self.controls_overlay.show_panel()
        _LOG.info(f"Recalled camera bookmark {slot}.")
        return True

    def _delete_bookmark_slot(self, slot: int) -> None:
        if not self._has_map_loaded:
            return
        if slot not in self._bookmarks:
            _LOG.info(f"Bookmark {slot} does not exist; nothing to delete.")
            return
        del self._bookmarks[slot]
        self._save_bookmarks()
        _LOG.info(f"Deleted camera bookmark {slot}.")

    def _handle_bookmark_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        if not self._has_map_loaded:
            return False
        keys = self.wnd.keys
        slot = self._digit_for_key(keys, key)
        if slot is None:
            return False

        # Platform-specific bookmark save modifier (Command on macOS, Control on Windows/Linux).
        # Shift+digit is accepted as a fallback on macOS for backends that don't report Command.
        save_modifier_down = self._bookmark_save_modifier_is_down(modifiers)
        shift_down = self._key_is_down(keys, "LEFT_SHIFT", "RIGHT_SHIFT", "LSHIFT", "RSHIFT")
        ctrl_down = self._control_is_down(modifiers)
        backspace_down = self._key_is_down(
            keys,
            "DELETE", "DEL",
            "FORWARD_DELETE", "FWDDELETE",
        )

        action = viewer_bookmarks.bookmark_hotkey_action(
            slot,
            save_modifier_down=save_modifier_down,
            shift_down=shift_down,
            ctrl_down=ctrl_down,
            backspace_down=backspace_down,
            shift_digit_save_fallback=(
                self._active_presentation_profile()
                .shift_digit_bookmark_save_fallback
            ),
        )
        if action is viewer_bookmarks.BookmarkHotkeyAction.NONE:
            return False
        if action is viewer_bookmarks.BookmarkHotkeyAction.DELETE:
            self._delete_bookmark_slot(slot)
            return True
        if action is viewer_bookmarks.BookmarkHotkeyAction.SAVE:
            self._save_bookmark_slot(slot)
            return True
        self._recall_bookmark_slot(slot)
        return True

    def _handle_fly_speed_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        """Apply one persistent fly-speed step without entering motion state."""
        speed_step = viewer_input.fly_speed_adjustment_step_for_key(
            self.wnd.keys,
            key,
            shift_down=self._shift_is_down(modifiers),
        )
        if speed_step is None:
            return False
        if self._recorded_dive_is_paused():
            return True

        camera = getattr(self, "camera", None)
        if camera is None:
            return True
        camera.adjust_speed(speed_step)
        return True

    def _option_look_active(self) -> bool:
        if not self._active_presentation_profile().option_left_mouse_look_enabled:
            return False
        return self._key_is_down(
            self.wnd.keys,
            "LEFT_ALT", "RIGHT_ALT", "LEFT_OPTION", "RIGHT_OPTION", "LALT", "RALT",
        )

    def _continuous_input_intent(self, dt: float) -> viewer_input.ContinuousInputIntent:
        return viewer_input.continuous_input_intent(
            keys=self.wnd.keys,
            keys_down=self._keys_down,
            dt=dt,
            key_look_pixels_per_second=self._KEY_LOOK_PIXELS_PER_SECOND,
        )

    def _continuous_input_has_navigation_intent(self, dt: float) -> bool:
        intent = self._continuous_input_intent(dt)
        return bool(intent.has_motion or intent.has_look or intent.has_roll)

    def _handle_continuous_input(self, dt: float):
        intent = self._continuous_input_intent(dt)
        if intent.has_motion:
            self._move_camera(
                intent.forward_amount,
                intent.right_amount,
                intent.up_amount,
                dt,
                intent.speed_multiplier,
            )
        if intent.has_look:
            self.camera.look(intent.yaw_delta, intent.pitch_delta)
        if intent.has_roll:
            self.camera.barrel_roll(intent.roll_delta)

    def _handle_paused_recorded_dive_input(self, dt: float) -> None:
        """Permit look-only inspection without turning a paused dive into flight."""
        intent = self._continuous_input_intent(dt)
        if intent.has_look:
            self.camera.look(intent.yaw_delta, intent.pitch_delta)

    def _handle_manual_input_frame(self, dt: float, *, now: float) -> None:
        """Apply manual camera controls for the current frame."""
        del now
        self._handle_continuous_input(dt)

    def _primary_shortcut_is_down(self, modifiers: KeyModifiers) -> bool:
        """Return whether the platform-native application modifier is active."""
        if self._active_presentation_profile().tk_primary_modifier_name == "Command":
            return self._command_is_down(modifiers)
        return self._control_is_down(modifiers)

    def _primary_shortcut_label(self) -> str:
        """Return the platform-native label for an application shortcut."""
        return self._active_presentation_profile().primary_shortcut_modifier_label

    def _handle_window_shortcut(self, key, modifiers: KeyModifiers) -> bool:
        """Handle modifier-based import-pause and open-map shortcuts."""
        if not self._primary_shortcut_is_down(modifiers):
            return False

        pause_key = self._resolve_key_optional(self.wnd.keys, "P")
        if (
            pause_key is not None
            and key == pause_key
            and self._shift_is_down(modifiers)
        ):
            if self._import_active:
                self._request_import_pause()
                return True

        open_key = self._resolve_key_optional(self.wnd.keys, "O")
        if open_key is not None and key == open_key:
            if self._has_map_loaded and not self._import_active:
                self._handle_open_button_click()
            return True

        return False

    def _request_import_pause(self) -> None:
        self._ensure_import_controller().request_pause()

    def _handle_begin_screen_hotkey(self, key) -> bool:
        """Keep the introductory overlay's single-key input boundary intact."""
        if not self.controls_overlay.is_waiting_for_begin:
            return False
        space_key = self._resolve_key_optional(
            self.wnd.keys,
            "SPACE",
            "SPACEBAR",
        )
        if (
            space_key is not None
            and key == space_key
            and self.controls_overlay.is_ready_to_begin
        ):
            self.controls_overlay.dismiss_begin_screen()
        return True

    def _handle_manual_dive_trace_hotkey(
        self,
        key,
        modifiers: KeyModifiers,
    ) -> bool:
        """Use Ctrl/Cmd+T to start or stop a map-local manual route trace."""
        if not self._has_map_loaded:
            return False
        trace_key = self._resolve_key_optional(self.wnd.keys, "T")
        if trace_key is None or key != trace_key:
            return False
        if not self._primary_shortcut_is_down(modifiers):
            return False
        if self._capture_shortcut_is_ignored(CaptureOwner.DIVE_TRACE):
            return True
        self._toggle_manual_dive_trace()
        return True

    def _handle_slice_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        """Use Ctrl/Cmd+C to arm, cancel, or finish a portable cave slice."""
        if not getattr(self, "_has_map_loaded", False):
            return False
        slice_key = self._resolve_key_optional(self.wnd.keys, "C")
        if (
            slice_key is None
            or key != slice_key
            or not self._primary_shortcut_is_down(modifiers)
        ):
            return False
        if self._capture_shortcut_is_ignored(CaptureOwner.SLICE):
            return True
        return self._toggle_slice()

    def _handle_capture_escape_hotkey(self, key) -> bool:
        """Own Escape so capture cancellation precedes delayed viewer close."""
        escape_key = self._resolve_key_optional(self.wnd.keys, "ESCAPE", "ESC")
        if escape_key is None or key != escape_key:
            return False
        if self._capture_owner() is not None:
            return self._begin_escape_capture_cancellation()
        self.on_close()
        return True

    def _handle_recorded_dive_hotkey(
        self,
        key,
        modifiers: KeyModifiers,
    ) -> bool:
        """Use Space to pause or resume an opened Recorded Dive."""
        del modifiers
        if not self._recorded_dive_is_active():
            return False
        pause_key = self._resolve_key_optional(self.wnd.keys, "SPACE", "SPACEBAR")
        if pause_key is None or key != pause_key:
            return False
        return self._toggle_recorded_dive_pause()

    def _handle_recording_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        """Use Ctrl/Cmd+R to start, cancel, or stop recording."""
        if not self._has_map_loaded:
            return False
        record_key = self._resolve_key_optional(self.wnd.keys, "R")
        if record_key is None or key != record_key:
            return False
        if not self._primary_shortcut_is_down(modifiers):
            return False
        if self._capture_shortcut_is_ignored(CaptureOwner.VIDEO):
            return True
        self._toggle_recording()
        return True

    def _handle_reset_view_shortcut(self, key, modifiers: KeyModifiers) -> bool:
        """Handle CMD+0 (macOS) or CTRL+0 (Windows/Linux) to reset view."""
        keys = self.wnd.keys

        # Check if this is the 0 key
        if not self._is_zero_key(keys, key):
            return False

        if self._primary_shortcut_is_down(modifiers):
            if self._recorded_dive_is_active():
                self._stop_recorded_dive(reason="view_reset")
            self.camera.reset_view()
            return True

        return False

    def _request_startup_focus_once(self) -> None:
        """Attempt to bring the app window to foreground once after startup."""
        if self._startup_focus_requested:
            return
        self._startup_focus_requested = True

        self._active_presentation_actions_adapter().focus_viewer_window(self.wnd)

    def _reset_transient_input_state(self, reason: str) -> None:
        """Clear transient input/capture flags that can get stuck across sleep/focus changes."""
        self._keys_down.clear()
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None
        self.color_picker.on_mouse_release()
        if hasattr(self.wnd, "mouse_exclusivity"):
            self.wnd.mouse_exclusivity = False

        now = time.time()
        if now - self._last_input_reset_log > 3.0:
            _LOG.info(f"Input state reset ({reason}).")
            self._last_input_reset_log = now

    def _query_runtime_iconified_state(self) -> bool:
        """Best-effort minimized/backgrounded detection across window backends."""
        for target in (getattr(self.wnd, "_window", None), self.wnd):
            if target is None:
                continue
            for attr in ("minimized", "is_minimized", "iconified"):
                try:
                    if hasattr(target, attr) and bool(getattr(target, attr)):
                        return True
                except Exception:
                    pass
            for attr in ("visible", "is_visible"):
                try:
                    if hasattr(target, attr):
                        value = getattr(target, attr)
                        value = value() if callable(value) else value
                        if value is False:
                            return True
                except Exception:
                    pass
        return False

    def _set_background_pause(self, should_pause: bool, reason: str) -> None:
        self._is_iconified = bool(should_pause)
        if self._is_background_paused == self._is_iconified:
            return

        self._is_background_paused = self._is_iconified
        if self._is_background_paused:
            self._reset_transient_input_state(reason)
            controller = getattr(self, "_recorded_dive_controller", None)
            if (
                controller is not None
                and controller.active
                and controller.state
                is not recorded_dive.RecordedDivePlaybackState.PAUSED
            ):
                self._recorded_dive_background_paused = controller.pause(
                    now=time.perf_counter()
                )
            if self._has_map_loaded and hasattr(self, "world"):
                self.world.pause()
        else:
            if self._has_map_loaded and hasattr(self, "world"):
                self.world.resume()
            controller = getattr(self, "_recorded_dive_controller", None)
            if (
                getattr(self, "_recorded_dive_background_paused", False)
                and controller is not None
                and controller.state
                is recorded_dive.RecordedDivePlaybackState.PAUSED
            ):
                controller.resume(self.camera, now=time.perf_counter())
            self._recorded_dive_background_paused = False

    def _handle_mouse_look_motion(self, x, y, dx, dy):
        # Cocoa can deliver passive mouse-move callbacks while the native
        # window exists but before our Python-side controls are fully built.
        # Treat those early/late events as no-ops so ctypes does not print
        # ignored callback exceptions to stderr.
        if (
            not getattr(self, "_window_setup_complete", False)
            or self._input_is_suppressed()
        ):
            return

        # Color picker's RGB sliders still use continuous drag (a
        # separate feature from the brightness/render-distance controls
        # below, which were converted to discrete +/- steppers) -- this
        # still needs to take priority over camera look while one of its
        # sliders is being dragged, same reasoning as before.
        color_picker = getattr(self, "color_picker", None)
        if color_picker is not None and color_picker.is_dragging:
            color_picker.on_mouse_drag(x, y, self.wnd.size)
            return
        # macOS-friendly fallback: Option + pointer movement can drive
        # look even without a physical click/drag gesture.
        if self._option_look_active() or self._mouse_look_active:
            # On the first event after mouse exclusivity is enabled the
            # backend warps the cursor to the window centre, generating a
            # large spurious delta.  _last_mouse_pos being None is the
            # sentinel for "just activated": absorb that one event and
            # record a real position so subsequent deltas are applied.
            if self._last_mouse_pos is None:
                self._last_mouse_pos = (x, y)
                return
            self._last_mouse_pos = (x, y)
            if (
                self._recorded_dive_is_active()
                and not self._recorded_dive_is_paused()
            ):
                self._stop_recorded_dive(reason="mouse_look")
            self.camera.look(dx, dy)

    def _pointer_press_intent(self, button) -> viewer_input.PointerPressIntent:
        """Normalize backend and modal state into one pointer intent."""
        setup_complete = bool(getattr(self, "_window_setup_complete", False))
        input_suppressed = (
            self._input_is_suppressed() if setup_complete else False
        )
        if not setup_complete or input_suppressed:
            return viewer_input.pointer_press_intent(
                viewer_input.PointerPressFacts(
                    setup_complete=setup_complete,
                    input_suppressed=input_suppressed,
                    waiting_for_begin=False,
                    help_visible=False,
                    recording_hides_hud=False,
                    is_left_button=False,
                    is_look_button=False,
                    option_look_active=False,
                )
            )

        look_button_name = (
            self._active_presentation_profile().mouse_look_button_name
        )
        left_button = self.wnd.mouse.left
        look_button = (
            left_button
            if look_button_name == "left"
            else self.wnd.mouse.right
        )
        return viewer_input.pointer_press_intent(
            viewer_input.PointerPressFacts(
                setup_complete=True,
                input_suppressed=False,
                waiting_for_begin=self.controls_overlay.is_waiting_for_begin,
                help_visible=self.controls_overlay.is_manual_mode,
                recording_hides_hud=self._recording_hides_hud(),
                is_left_button=button == left_button,
                is_look_button=button == look_button,
                option_look_active=self._option_look_active(),
            )
        )

    def _resolve_hud_pointer_intent(
        self,
        x: float,
        y: float,
    ) -> viewer_input.PointerPressIntent:
        """Resolve pure HUD hit results before applying any state change."""
        column = self._right_column_layout(self.wnd.size)
        stepper_hits = (
            (
                "brightness",
                self.light_stepper,
                column["brightness_anchor"],
            ),
            (
                "ambient",
                self.ambient_stepper,
                column["ambient_anchor"],
            ),
            (
                "render_distance",
                self.render_distance_stepper,
                column["render_distance_anchor"],
            ),
        )
        for target, stepper, anchor in stepper_hits:
            adjustment = stepper.adjustment_for_click(x, y, *anchor)
            if adjustment is not None:
                return viewer_input.PointerPressIntent(
                    viewer_input.PointerPressKind.STEPPER,
                    target=target,
                    adjustment=adjustment,
                )

        clicked_button = self.render_mode_buttons.button_for_click(
            x,
            y,
            self.wnd.size,
            column["buttons_top_y"],
            column["button_right_inset"],
        )
        if clicked_button is not None:
            if self._buttons_locked_for_loading():
                return viewer_input.PointerPressIntent(
                    viewer_input.PointerPressKind.IGNORE
                )
            return viewer_input.PointerPressIntent(
                viewer_input.PointerPressKind.VIEW_BUTTON,
                target=clicked_button,
            )

        if self.color_picker.is_active:
            kind = (
                viewer_input.PointerPressKind.COLOR_PICKER
                if self.color_picker.hit_test_panel(x, y, self.wnd.size)
                else viewer_input.PointerPressKind.DISMISS_COLOR_PICKER
            )
            return viewer_input.PointerPressIntent(kind)

        if self._has_map_loaded and self.minimap is not None:
            world_xz = self.minimap.world_xz_for_click(x, y, self.wnd.size)
            if world_xz is not None:
                return viewer_input.PointerPressIntent(
                    viewer_input.PointerPressKind.MINIMAP_TELEPORT,
                    world_xz=world_xz,
                )

        if self._active_presentation_profile().mouse_look_button_name == "left":
            return viewer_input.PointerPressIntent(
                viewer_input.PointerPressKind.START_MOUSE_LOOK
            )
        return viewer_input.PointerPressIntent(viewer_input.PointerPressKind.IGNORE)

    def _start_pointer_mouse_look(self, *, option_left: bool = False) -> None:
        """Apply backend capture state for a resolved mouse-look press."""
        self._mouse_look_active = True
        if option_left:
            self._mouse_look_left_option_active = True
        self._last_mouse_pos = None
        self.wnd.mouse_exclusivity = True

    def _apply_view_button_intent(self, target: str) -> None:
        """Apply a resolved right-column button action."""
        self.render_mode_buttons.apply_button_click(target)
        if target == "shade":
            self._apply_shading_toggle()
        elif target == "help":
            if self.controls_overlay.is_manual_mode:
                self.controls_overlay.hide_help()
            else:
                self.controls_overlay.show_help()
        elif target == "color":
            if self.color_picker.is_active:
                self.color_picker.hide()
            else:
                self.color_picker.show()
        elif target == "open":
            self._handle_open_button_click()

    def _apply_minimap_teleport(self, world_xz: tuple[float, float]) -> None:
        """Apply one resolved minimap landing on the render-thread camera."""
        if self._recorded_dive_is_active():
            self._stop_recorded_dive(reason="minimap_teleport")
        trace_pose_before_teleport = self._manual_dive_trace_pose()
        target_x, target_z = world_xz
        landing = chunker.find_landing_position(
            self.manifest,
            target_x,
            target_z,
            preferred_y=float(self.camera.position[1]),
        )
        pose = viewer_input.minimap_teleport_pose(
            self.camera.position,
            landing,
        )
        self.camera.position[:] = pose.position
        if pose.yaw is not None:
            self.camera.yaw = pose.yaw
            self.camera.pitch = 0.0
            self.camera.roll = 0.0
        self._mark_manual_dive_trace_discontinuity(
            trace_pose_before_teleport,
            reason="minimap_teleport",
        )
        self.controls_overlay.show_panel()

    def _apply_pointer_press_intent(
        self,
        intent: viewer_input.PointerPressIntent,
        *,
        x: float,
        y: float,
    ) -> None:
        """Apply a typed pointer action to window-owned state."""
        if intent.kind is viewer_input.PointerPressKind.IGNORE:
            return
        if intent.kind is viewer_input.PointerPressKind.DISMISS_HELP:
            self.controls_overlay.hide_help()
            return
        if intent.kind is viewer_input.PointerPressKind.START_MOUSE_LOOK:
            self._start_pointer_mouse_look(
                option_left=intent.option_left_look,
            )
            return
        if intent.kind is viewer_input.PointerPressKind.HUD:
            self._apply_pointer_press_intent(
                self._resolve_hud_pointer_intent(x, y),
                x=x,
                y=y,
            )
            return
        if intent.kind is viewer_input.PointerPressKind.STEPPER:
            stepper = {
                "brightness": self.light_stepper,
                "ambient": self.ambient_stepper,
                "render_distance": self.render_distance_stepper,
            }[intent.target]
            if intent.adjustment < 0:
                stepper.decrement()
            else:
                stepper.increment()
            return
        if intent.kind is viewer_input.PointerPressKind.VIEW_BUTTON:
            self._apply_view_button_intent(intent.target)
            return
        if intent.kind is viewer_input.PointerPressKind.COLOR_PICKER:
            self.color_picker.on_mouse_press(x, y, self.wnd.size)
            return
        if intent.kind is viewer_input.PointerPressKind.DISMISS_COLOR_PICKER:
            self.color_picker.hide()
            return
        if intent.kind is viewer_input.PointerPressKind.MINIMAP_TELEPORT:
            self._apply_minimap_teleport(intent.world_xz)

    def _pointer_release_kind(self, button) -> viewer_input.PointerReleaseKind:
        """Normalize one backend release into a testable cleanup action."""
        setup_complete = bool(getattr(self, "_window_setup_complete", False))
        input_suppressed = (
            self._input_is_suppressed() if setup_complete else False
        )
        if setup_complete and not input_suppressed:
            look_button_name = (
                self._active_presentation_profile().mouse_look_button_name
            )
            left_button = self.wnd.mouse.left
            look_button = (
                left_button
                if look_button_name == "left"
                else self.wnd.mouse.right
            )
            is_left_button = button == left_button
            is_look_button = button == look_button
        else:
            is_left_button = False
            is_look_button = False
        return viewer_input.pointer_release_kind(
            viewer_input.PointerReleaseFacts(
                setup_complete=setup_complete,
                input_suppressed=input_suppressed,
                is_left_button=is_left_button,
                is_look_button=is_look_button,
                option_left_look_active=bool(
                    getattr(self, "_mouse_look_left_option_active", False)
                ),
                mouse_look_active=bool(
                    getattr(self, "_mouse_look_active", False)
                ),
            )
        )

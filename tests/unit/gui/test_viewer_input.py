"""Tests for backend-neutral viewer input policy."""

from types import SimpleNamespace

import pytest

from caveviewer.gui import viewer_input


def test_digit_and_zero_key_aliases_include_backend_names_and_ascii_fallback():
    """Digit matching supports backend constants and ASCII key codes."""
    keys = SimpleNamespace(KEY_5=500, NUMPAD_0=1000)

    assert viewer_input.digit_for_key(keys, 500) == 5
    assert viewer_input.digit_for_key(SimpleNamespace(), ord("7")) == 7
    assert viewer_input.is_zero_key(keys, 1000)
    assert viewer_input.is_zero_key(SimpleNamespace(), ord("0"))


def test_raw_command_modifier_down_decodes_backend_masks():
    """Command/Super raw modifier masks differ by window backend."""
    assert viewer_input.raw_command_modifier_down(
        1 << 6,
        "moderngl_window.context.pyglet.window",
    )
    assert viewer_input.raw_command_modifier_down(
        1 << 3,
        "moderngl_window.context.glfw.window",
    )
    assert viewer_input.raw_command_modifier_down(
        0x0C00,
        "moderngl_window.context.sdl2.window",
    )
    assert not viewer_input.raw_command_modifier_down(
        0,
        "moderngl_window.context.pyglet.window",
    )


def test_command_and_control_modifier_policy_uses_flags_and_pressed_keys():
    """Command policy combines modifier flags, fallback behavior, and key state."""
    keys = SimpleNamespace(LEFT_COMMAND=1, LEFT_CONTROL=2)

    assert viewer_input.command_is_down(
        SimpleNamespace(ctrl=True),
        keys,
        set(),
        command_modifier_uses_control_fallback=True,
        raw_command_down=False,
    )
    assert viewer_input.command_is_down(
        SimpleNamespace(),
        keys,
        {1},
        command_modifier_uses_control_fallback=False,
        raw_command_down=False,
    )
    assert viewer_input.control_is_down(SimpleNamespace(), keys, {2})


def test_continuous_input_intent_combines_motion_look_and_roll():
    """Continuous key state maps to camera movement, look, and roll deltas."""
    keys = SimpleNamespace(
        W=1,
        S=2,
        A=3,
        D=4,
        E=5,
        Q=6,
        LEFT_SHIFT=7,
        LEFT=8,
        RIGHT=9,
        UP=10,
        DOWN=11,
        I=12,
        J=13,
        K=14,
        L=15,
        Z=16,
        X=17,
    )

    intent = viewer_input.continuous_input_intent(
        keys=keys,
        keys_down={1, 4, 5, 7, 9, 12, 16},
        dt=0.5,
        key_look_pixels_per_second=100.0,
    )

    assert intent.forward_amount == 1.0
    assert intent.right_amount == 1.0
    assert intent.up_amount == 1.0
    assert intent.speed_multiplier == 3.0
    assert intent.yaw_delta == 50.0
    assert intent.pitch_delta == -50.0
    assert intent.roll_delta == 1.0


def test_fly_speed_adjustment_step_supports_standard_and_keypad_keys():
    keys = SimpleNamespace(
        MINUS=1,
        EQUAL=2,
        PLUS=3,
        NUMPAD_SUBTRACT=4,
        NUMPAD_ADD=5,
    )

    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 1) == -1
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 2) == 1
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 2, shift_down=True) is None
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 3) == 1
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 4) == -1
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 5) == 1
    assert viewer_input.fly_speed_adjustment_step_for_key(keys, 99) is None


def test_key_event_press_or_repeat_policy_supports_explicit_and_legacy_backends():
    explicit_keys = SimpleNamespace(
        ACTION_PRESS=1,
        ACTION_RELEASE=0,
        ACTION_REPEAT=2,
    )
    legacy_keys = SimpleNamespace(ACTION_PRESS=1, ACTION_RELEASE=0)

    assert viewer_input.key_event_is_press_or_repeat(explicit_keys, 1)
    assert viewer_input.key_event_is_press_or_repeat(explicit_keys, 2)
    assert not viewer_input.key_event_is_press_or_repeat(explicit_keys, 0)
    assert not viewer_input.key_event_is_press_or_repeat(explicit_keys, 3)
    assert viewer_input.key_event_is_press_or_repeat(legacy_keys, 2)


def _pointer_facts(**overrides):
    values = {
        "setup_complete": True,
        "input_suppressed": False,
        "waiting_for_begin": False,
        "help_visible": False,
        "recording_hides_hud": False,
        "is_left_button": True,
        "is_look_button": True,
        "option_look_active": False,
    }
    values.update(overrides)
    return viewer_input.PointerPressFacts(**values)


def test_pointer_press_policy_preserves_modal_and_mouse_look_priority():
    assert viewer_input.pointer_press_intent(
        _pointer_facts(setup_complete=False)
    ).kind is viewer_input.PointerPressKind.IGNORE
    assert viewer_input.pointer_press_intent(
        _pointer_facts(help_visible=True)
    ).kind is viewer_input.PointerPressKind.DISMISS_HELP

    option_look = viewer_input.pointer_press_intent(
        _pointer_facts(
            recording_hides_hud=True,
            is_look_button=False,
            option_look_active=True,
        )
    )
    assert option_look.kind is viewer_input.PointerPressKind.START_MOUSE_LOOK
    assert option_look.option_left_look is True
    assert viewer_input.pointer_press_intent(
        _pointer_facts(
            recording_hides_hud=True,
            is_left_button=False,
            is_look_button=False,
        )
    ).kind is viewer_input.PointerPressKind.IGNORE
    assert viewer_input.pointer_press_intent(
        _pointer_facts(is_look_button=False)
    ).kind is viewer_input.PointerPressKind.HUD


def test_pointer_release_policy_cleans_up_option_and_platform_look_buttons():
    facts = viewer_input.PointerReleaseFacts(
        setup_complete=True,
        input_suppressed=False,
        is_left_button=True,
        is_look_button=False,
        option_left_look_active=True,
        mouse_look_active=True,
    )
    assert (
        viewer_input.pointer_release_kind(facts)
        is viewer_input.PointerReleaseKind.STOP_OPTION_LOOK
    )
    assert viewer_input.pointer_release_kind(
        viewer_input.PointerReleaseFacts(
            setup_complete=True,
            input_suppressed=False,
            is_left_button=False,
            is_look_button=True,
            option_left_look_active=False,
            mouse_look_active=True,
        )
    ) is viewer_input.PointerReleaseKind.STOP_MOUSE_LOOK
    assert viewer_input.pointer_release_kind(
        viewer_input.PointerReleaseFacts(
            setup_complete=True,
            input_suppressed=False,
            is_left_button=True,
            is_look_button=False,
            option_left_look_active=False,
            mouse_look_active=False,
        )
    ) is viewer_input.PointerReleaseKind.RELEASE_COLOR_PICKER


def test_minimap_teleport_pose_faces_distant_target_without_nearby_jitter():
    far_pose = viewer_input.minimap_teleport_pose(
        (10.0, 2.0, 20.0),
        (10.0, 8.0, 30.0),
    )
    near_pose = viewer_input.minimap_teleport_pose(
        (10.0, 2.0, 20.0),
        (10.2, 8.0, 20.2),
    )

    assert far_pose.position == (10.0, 8.0, 30.0)
    assert far_pose.yaw == pytest.approx(1.5707963267948966)
    assert near_pose.yaw is None

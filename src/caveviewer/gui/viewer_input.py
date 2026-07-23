"""Backend-neutral viewer input policy and key alias handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContinuousInputIntent:
    """Camera movement/look deltas derived from the current key state."""

    forward_amount: float = 0.0
    right_amount: float = 0.0
    up_amount: float = 0.0
    speed_multiplier: float = 1.0
    yaw_delta: float = 0.0
    pitch_delta: float = 0.0
    roll_delta: float = 0.0

    @property
    def has_motion(self) -> bool:
        return bool(self.forward_amount or self.right_amount or self.up_amount)

    @property
    def has_look(self) -> bool:
        return bool(self.yaw_delta or self.pitch_delta)

    @property
    def has_roll(self) -> bool:
        return bool(self.roll_delta)


def resolve_key(
    keys,
    *candidate_names: str,
    cache: dict[tuple[str, ...], Any] | None = None,
):
    """
    Resolve one backend key code from known moderngl-window key aliases.

    Different backends and versions expose different names for the same key.
    Keep that compatibility policy outside the OpenGL window owner so command
    routing can be tested with simple fake key objects.
    """
    cache_key = tuple(candidate_names)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    for name in candidate_names:
        if hasattr(keys, name):
            value = getattr(keys, name)
            if cache is not None:
                cache[cache_key] = value
            return value
    raise AttributeError(
        f"None of the key names {candidate_names} exist on this "
        f"moderngl-window version's Keys class. Available attributes: "
        f"{[attr for attr in dir(keys) if not attr.startswith('_')]}"
    )


def resolve_key_optional(keys, *candidate_names: str):
    """Return a backend key code if any alias exists, otherwise ``None``."""
    for name in candidate_names:
        if hasattr(keys, name):
            return getattr(keys, name)
    return None


def key_is_down(keys, keys_down: set, *candidate_names: str) -> bool:
    """Return whether any aliased key is present in the active key set."""
    for name in candidate_names:
        if hasattr(keys, name) and getattr(keys, name) in keys_down:
            return True
    return False


def digit_for_key(keys, key) -> int | None:
    """Return bookmark slot ``1..9`` for backend-specific digit key aliases."""
    for digit in range(1, 10):
        candidates = (
            f"_{digit}",
            f"KEY_{digit}",
            f"NUMBER_{digit}",
            f"NUM_{digit}",
            f"NUMPAD_{digit}",
        )
        for name in candidates:
            if hasattr(keys, name) and getattr(keys, name) == key:
                return digit

    if isinstance(key, int) and ord("1") <= key <= ord("9"):
        return key - ord("0")

    return None


def is_zero_key(keys, key) -> bool:
    """Return whether ``key`` is a top-row or keypad zero key."""
    candidates = (
        "_0",
        "KEY_0",
        "NUMBER_0",
        "NUM_0",
        "NUMPAD_0",
    )
    for name in candidates:
        if hasattr(keys, name) and getattr(keys, name) == key:
            return True

    return isinstance(key, int) and key == ord("0")


def raw_command_modifier_down(raw_mods: int, backend_module: str) -> bool:
    """Decode backend-specific raw modifier masks for Command/Super."""
    if raw_mods == 0:
        return False

    backend_module = backend_module.lower()
    if ".pyglet." in backend_module:
        return (raw_mods & (1 << 6)) != 0
    if ".glfw." in backend_module:
        return (raw_mods & (1 << 3)) != 0
    if ".sdl2." in backend_module or ".pygame2." in backend_module:
        return (raw_mods & 0x0C00) != 0
    return False


def modifier_flag_is_down(modifiers, *attrs: str) -> bool:
    """Read a truthy modifier attribute while tolerating backend objects."""
    for attr in attrs:
        if hasattr(modifiers, attr):
            try:
                if bool(getattr(modifiers, attr)):
                    return True
            except Exception:
                pass
    return False


def command_is_down(
    modifiers,
    keys,
    keys_down: set,
    *,
    command_modifier_uses_control_fallback: bool,
    raw_command_down: bool,
) -> bool:
    """Return whether the platform primary Command/Super key is active."""
    if command_modifier_uses_control_fallback and raw_command_down:
        return True

    if modifier_flag_is_down(modifiers, "super", "command", "logo", "meta"):
        return True

    if command_modifier_uses_control_fallback and modifier_flag_is_down(
        modifiers,
        "ctrl",
        "control",
    ):
        return True

    return key_is_down(
        keys,
        keys_down,
        "LEFT_SUPER",
        "RIGHT_SUPER",
        "LEFT_COMMAND",
        "RIGHT_COMMAND",
        "COMMAND",
        "LCOMMAND",
        "RCOMMAND",
        "CMD",
        "LSUPER",
        "RSUPER",
        "LGUI",
        "RGUI",
        "LEFT_WINDOWS",
        "RIGHT_WINDOWS",
        "LWIN",
        "RWIN",
    )


def control_is_down(modifiers, keys, keys_down: set) -> bool:
    """Return whether Control/Ctrl is active."""
    if modifier_flag_is_down(modifiers, "ctrl", "control"):
        return True
    return key_is_down(
        keys,
        keys_down,
        "LEFT_CONTROL",
        "RIGHT_CONTROL",
        "LCTRL",
        "RCTRL",
        "CONTROL",
        "LCONTROL",
        "RCONTROL",
    )


def shift_is_down(modifiers, keys, keys_down: set) -> bool:
    """Return whether Shift is active."""
    if modifier_flag_is_down(modifiers, "shift"):
        return True
    return key_is_down(
        keys,
        keys_down,
        "LEFT_SHIFT",
        "RIGHT_SHIFT",
        "LSHIFT",
        "RSHIFT",
        "SHIFT",
    )


def bookmark_save_modifier_is_down(
    *,
    save_modifier: str,
    command_down: bool,
    control_down: bool,
) -> bool:
    """Return whether the platform-specific bookmark-save modifier is active."""
    if save_modifier == "command":
        return command_down
    if save_modifier == "control":
        return control_down
    return False


def continuous_input_intent(
    *,
    keys,
    keys_down: set,
    dt: float,
    key_look_pixels_per_second: float,
    roll_speed: float = 2.0,
) -> ContinuousInputIntent:
    """Translate pressed movement/look keys into camera-side deltas."""
    forward_amount = 0.0
    right_amount = 0.0
    up_amount = 0.0
    if keys.W in keys_down:
        forward_amount += 1.0
    if keys.S in keys_down:
        forward_amount -= 1.0
    if keys.D in keys_down:
        right_amount += 1.0
    if keys.A in keys_down:
        right_amount -= 1.0

    e_key = resolve_key(keys, "E")
    q_key = resolve_key(keys, "Q")
    if e_key in keys_down:
        up_amount += 1.0
    if q_key in keys_down:
        up_amount -= 1.0

    shift_key = resolve_key(keys, "LEFT_SHIFT", "LSHIFT")
    speed_multiplier = 3.0 if shift_key in keys_down else 1.0

    left_key = resolve_key(keys, "LEFT", "ARROW_LEFT")
    right_key = resolve_key(keys, "RIGHT", "ARROW_RIGHT")
    up_key = resolve_key(keys, "UP", "ARROW_UP")
    down_key = resolve_key(keys, "DOWN", "ARROW_DOWN")
    i_key = resolve_key_optional(keys, "I")
    j_key = resolve_key_optional(keys, "J")
    k_key = resolve_key_optional(keys, "K")
    l_key = resolve_key_optional(keys, "L")

    yaw_dir = 0.0
    if left_key in keys_down or (j_key is not None and j_key in keys_down):
        yaw_dir -= 1.0
    if right_key in keys_down or (l_key is not None and l_key in keys_down):
        yaw_dir += 1.0

    pitch_dir = 0.0
    if up_key in keys_down or (i_key is not None and i_key in keys_down):
        pitch_dir -= 1.0
    if down_key in keys_down or (k_key is not None and k_key in keys_down):
        pitch_dir += 1.0

    look_amount = float(key_look_pixels_per_second) * float(dt)
    yaw_delta = yaw_dir * look_amount
    pitch_delta = pitch_dir * look_amount

    z_key = resolve_key_optional(keys, "Z")
    x_key = resolve_key_optional(keys, "X")
    roll_dir = 0.0
    if z_key is not None and z_key in keys_down:
        roll_dir += 1.0
    if x_key is not None and x_key in keys_down:
        roll_dir -= 1.0

    return ContinuousInputIntent(
        forward_amount=forward_amount,
        right_amount=right_amount,
        up_amount=up_amount,
        speed_multiplier=speed_multiplier,
        yaw_delta=yaw_delta,
        pitch_delta=pitch_delta,
        roll_delta=roll_dir * roll_speed * float(dt),
    )

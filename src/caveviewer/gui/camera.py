"""Free-fly camera state and movement math for the viewer.

Free-fly (6DOF, "noclip"/spectator style) camera. Cave traversal needs
full pitch/yaw with no ground constraint -- divers move in 3D, not on a
walkable surface -- so this deliberately does NOT behave like an FPS
walking camera. Movement is always relative to the camera's current
look direction (forward = into the screen, regardless of pitch), like
a flight-sim free camera.

Controls (bound in caveviewer.gui.viewer_window, documented here for reference):
    W/S       - move forward/backward along view direction
    A/D       - strafe left/right
    E/Q       - move up/down along world Y (or could be view-relative; see note)
    I,J,K,L   - look (yaw/pitch)
    Z/X       - barrel roll (counterclockwise/clockwise from diver perspective)
    Shift     - speed boost multiplier
    Scroll    - adjust base fly speed (useful since cave scale varies a lot)
"""

from __future__ import annotations

import math
import numpy as np


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    if n < 1e-9:
        return v
    return v / n


class FlyCamera:
    def __init__(self, position=(0.0, 0.0, 0.0), yaw_deg=-90.0, pitch_deg=0.0,
                 move_speed=4.0, mouse_sensitivity=0.12):
        self.position = np.array(position, dtype=np.float64)
        self.move_speed = move_speed
        self.mouse_sensitivity = mouse_sensitivity
        self.fov_deg = 75.0
        self.near = 0.05
        self.far = 1000.0
        # Kept for backward-compat: viewer_window reads this when clamping
        # bookmark pitch values on load.
        self._pitch_limit = math.radians(89.0)

        # Orientation is stored as a 3×3 matrix whose rows are the
        # world-space directions of the camera's local right (row 0),
        # up (row 1), and forward (row 2) axes.  Storing orientation this
        # way instead of three Euler scalars means that barrel-rolled look
        # rotations are always applied around the *camera's own* axes, not
        # world axes -- so a pilot who has rolled 90° and then moves the
        # mouse sideways gets a yaw around their tilted local up (which
        # happens to be the world side-axis), not a world-Y yaw that would
        # slide them along a plane.
        self._orient = np.eye(3, dtype=np.float64)
        self._rebuild_from_euler(math.radians(yaw_deg), math.radians(pitch_deg), 0.0)

    # -- orientation helpers -----------------------------------------------

    def _rebuild_from_euler(self, yaw: float, pitch: float, roll: float) -> None:
        """Reconstruct _orient from Euler yaw / pitch / roll angles."""
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(yaw),   math.sin(yaw)
        f = np.array([cp * cy, sp, cp * sy], dtype=np.float64)
        world_up = np.array([0.0, 1.0, 0.0])
        r = np.cross(f, world_up)
        rn = np.linalg.norm(r)
        r = np.array([1.0, 0.0, 0.0]) if rn < 1e-9 else r / rn
        u = _normalize(np.cross(r, f))
        if roll != 0.0:
            c, s = math.cos(roll), math.sin(roll)
            r = _normalize(r * c + np.cross(f, r) * s)
            u = _normalize(u * c + np.cross(f, u) * s)
        self._orient = np.array([r, u, f], dtype=np.float64)

    def _rotate_orient(self, axis: np.ndarray, angle: float) -> None:
        """Rotate all three orientation rows around `axis` by `angle` (Rodrigues).

        `axis` must already be a copy -- not a live view into self._orient --
        when the axis is one of the orientation rows itself.
        """
        c, s = math.cos(angle), math.sin(angle)
        k = axis  # assumed unit-length
        for i in range(3):
            v = self._orient[i]
            self._orient[i] = v * c + np.cross(k, v) * s + k * np.dot(k, v) * (1.0 - c)

    def _reorthonormalize(self) -> None:
        """Gram-Schmidt pass to suppress floating-point drift."""
        f = _normalize(self._orient[2])
        r = self._orient[0] - np.dot(self._orient[0], f) * f
        rn = np.linalg.norm(r)
        if rn < 1e-9:
            # Degenerate (right became parallel to forward): pick a safe fallback.
            r = np.cross(f, np.array([0.0, 1.0, 0.0]))
            rn = np.linalg.norm(r)
            if rn < 1e-9:
                r = np.cross(f, np.array([1.0, 0.0, 0.0]))
                rn = np.linalg.norm(r)
        r /= rn
        u = np.cross(r, f)   # orthogonal by construction; normalize for safety
        self._orient[0] = r
        self._orient[1] = _normalize(u)
        self._orient[2] = f

    # -- orientation vectors -----------------------------------------------

    def forward(self) -> np.ndarray:
        return self._orient[2].copy()

    def right(self) -> np.ndarray:
        return self._orient[0].copy()

    def up(self) -> np.ndarray:
        return self._orient[1].copy()

    def set_orientation_basis(self, *, right, up, forward) -> None:
        """Replace orientation from a recorded orthogonal camera basis.

        Recorded Dive playback uses this direct render-thread seam so an
        authoritative trace does not have to round-trip through Euler angles.
        The Gram-Schmidt pass only removes accumulated floating-point drift.
        """
        candidate = np.asarray([right, up, forward], dtype=np.float64)
        if candidate.shape != (3, 3) or not np.all(np.isfinite(candidate)):
            raise ValueError("camera orientation basis must contain finite 3D vectors")
        self._orient = candidate.copy()
        self._reorthonormalize()

    # -- Euler-angle properties (backward-compat for bookmark save / load) --
    #
    # viewer_window reads and writes camera.yaw / .pitch / .roll directly
    # when saving and restoring bookmark slots.  These properties let that
    # code continue to work unchanged while the real orientation state is
    # the matrix above.

    @property
    def yaw(self) -> float:
        f = self._orient[2]
        return math.atan2(float(f[2]), float(f[0]))

    @yaw.setter
    def yaw(self, value: float) -> None:
        self._rebuild_from_euler(value, self.pitch, self.roll)

    @property
    def pitch(self) -> float:
        return math.asin(max(-1.0, min(1.0, float(self._orient[2][1]))))

    @pitch.setter
    def pitch(self, value: float) -> None:
        self._rebuild_from_euler(self.yaw, value, self.roll)

    @property
    def roll(self) -> float:
        f = self._orient[2]
        world_up = np.array([0.0, 1.0, 0.0])
        expected_r = np.cross(f, world_up)
        en = np.linalg.norm(expected_r)
        if en < 1e-9:
            return 0.0
        expected_r /= en
        cos_a = max(-1.0, min(1.0, float(np.dot(expected_r, self._orient[0]))))
        sin_a = float(np.dot(np.cross(expected_r, self._orient[0]), f))
        return math.atan2(sin_a, cos_a)

    @roll.setter
    def roll(self, value: float) -> None:
        self._rebuild_from_euler(self.yaw, self.pitch, value)

    # -- look & movement ---------------------------------------------------

    def look(self, dx_pixels: float, dy_pixels: float) -> None:
        """Apply mouse (or keyboard) delta to orientation.

        Rotations are applied around the camera's own local axes so that a
        rolled camera behaves like a pilot in a plane: moving the mouse
        left/right yaws around the *tilted* local up, and moving up/down
        pitches around the *tilted* local right.  Before this fix, both
        axes were world-fixed, so after a barrel roll the mouse was stuck
        sliding the view along the original world plane.
        """
        if dx_pixels:
            # Yaw around local up.  Negated so mouse-right → turn right,
            # matching the original Euler convention.
            up_axis = self._orient[1].copy()
            self._rotate_orient(up_axis, -math.radians(dx_pixels * self.mouse_sensitivity))
        if dy_pixels:
            # Pitch around local right.
            right_axis = self._orient[0].copy()
            self._rotate_orient(right_axis, -math.radians(dy_pixels * self.mouse_sensitivity))
        self._reorthonormalize()

    # -- movement ----------------------------------------------------------

    def move(self, forward_amt: float, right_amt: float, up_amt: float,
              dt: float, speed_multiplier: float = 1.0) -> None:
        """
        forward_amt/right_amt/up_amt are typically -1/0/1 from key state.
        up_amt moves along WORLD up (not view-relative pitch), which feels
        more controllable when navigating tight cave geometry -- you don't
        accidentally fly into the ceiling just because you're looking up.
        """
        speed = self.move_speed * speed_multiplier * dt
        delta = (self.forward() * forward_amt + self.right() * right_amt) * speed
        delta[1] += up_amt * speed  # world-vertical, independent of pitch
        self.position += delta

    def adjust_speed(self, scroll_amt: float) -> None:
        """Multiplicative scroll-wheel speed adjustment; cave passages range
        from <1m crawls to 50m+ rooms, so an additive adjustment would be
        annoying at either extreme -- multiplicative scales naturally."""
        factor = 1.1 ** scroll_amt
        self.move_speed = max(0.1, min(200.0, self.move_speed * factor))

    def barrel_roll(self, droll_rad: float) -> None:
        """Roll around the local forward axis (positive = CCW from pilot POV)."""
        fwd_axis = self._orient[2].copy()
        self._rotate_orient(fwd_axis, droll_rad)
        self._reorthonormalize()

    def reset_view(self) -> None:
        """Level the camera: keep forward direction, re-align up to world Y."""
        f = self._orient[2].copy()
        world_up = np.array([0.0, 1.0, 0.0])
        r = np.cross(f, world_up)
        rn = np.linalg.norm(r)
        r = np.array([1.0, 0.0, 0.0]) if rn < 1e-9 else r / rn
        self._orient[0] = r
        self._orient[1] = _normalize(np.cross(r, f))
        # self._orient[2] (forward) is intentionally left unchanged

    # -- matrices ----------------------------------------------------------

    def view_matrix(self) -> np.ndarray:
        r   = self._orient[0]
        u   = self._orient[1]
        f   = self._orient[2]
        pos = self.position

        m = np.identity(4, dtype=np.float32)
        m[0, 0:3] = r
        m[1, 0:3] = u
        m[2, 0:3] = -f          # OpenGL convention: camera looks down -Z
        m[0, 3] = -np.dot(r, pos)
        m[1, 3] = -np.dot(u, pos)
        m[2, 3] =  np.dot(f, pos)
        return m

    def projection_matrix(self, aspect_ratio: float) -> np.ndarray:
        fov_rad = math.radians(self.fov_deg)
        f = 1.0 / math.tan(fov_rad / 2.0)
        near, far = self.near, self.far
        m = np.zeros((4, 4), dtype=np.float32)
        m[0, 0] = f / aspect_ratio
        m[1, 1] = f
        m[2, 2] = (far + near) / (near - far)
        m[2, 3] = (2 * far * near) / (near - far)
        m[3, 2] = -1.0
        return m

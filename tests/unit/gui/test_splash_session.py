"""Tests for splash session state and Tk after-callback ownership."""

from caveviewer.gui.splash_session import SplashSession


class FakeRoot:
    """Small Tk root double for scheduling and canceling after callbacks."""

    def __init__(self) -> None:
        self.callbacks: dict[str, tuple[int, object]] = {}
        self.cancelled: list[str] = []
        self._next_after_id = 0

    def after(self, delay_ms: int, callback):
        self._next_after_id += 1
        after_id = f"after-{self._next_after_id}"
        self.callbacks[after_id] = (delay_ms, callback)
        return after_id

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.append(after_id)
        self.callbacks.pop(after_id, None)

    def run_after(self, after_id: str) -> None:
        _delay_ms, callback = self.callbacks.pop(after_id)
        callback()


def test_schedule_after_runs_callback_and_releases_after_id():
    """A live splash session runs scheduled callbacks once."""
    root = FakeRoot()
    session = SplashSession()
    calls: list[str] = []

    after_id = session.schedule_after(root, 100, lambda: calls.append("ran"))
    root.run_after(after_id)

    assert calls == ["ran"]
    assert not session._after_ids


def test_schedule_after_skips_callback_after_closing():
    """Closing the session prevents queued callbacks from mutating Tk widgets."""
    root = FakeRoot()
    session = SplashSession()
    calls: list[str] = []

    after_id = session.schedule_after(root, 100, lambda: calls.append("ran"))
    session.mark_closing()
    root.run_after(after_id)

    assert calls == []
    assert not session._after_ids


def test_cancel_after_callbacks_cancels_all_outstanding_callbacks():
    """Session shutdown cancels every callback still owned by the splash."""
    root = FakeRoot()
    session = SplashSession()

    first_id = session.schedule_after(root, 100, lambda: None)
    second_id = session.schedule_after(root, 200, lambda: None)
    session.cancel_after_callbacks(root)

    assert set(root.cancelled) == {first_id, second_id}
    assert root.callbacks == {}
    assert not session._after_ids


def test_select_folder_records_selected_path():
    """The session owns the selected folder result returned by the splash."""
    session = SplashSession()

    session.select_folder("/maps/example")

    assert session.selected_folder == "/maps/example"

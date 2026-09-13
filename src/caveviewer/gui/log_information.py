"""Persist Help's logging choice without changing the active logging session."""

from dataclasses import dataclass
from typing import Callable

from caveviewer.gui.preferences import Preferences, load_preferences, save_preferences


@dataclass(frozen=True)
class LogInformationState:
    value: str = "essential"
    error: str = ""


class LogInformationController:
    """Merge one Help setting into the latest saved preference snapshot."""

    def __init__(
        self, *, load: Callable[[], Preferences] = load_preferences,
        save: Callable[[Preferences], None] = save_preferences,
        on_saved: Callable[[Preferences], None] | None = None,
    ) -> None:
        self._load = load
        self._save = save
        self._on_saved = on_saved
        self.state = LogInformationState()

    def refresh(self) -> LogInformationState:
        try:
            self.state = LogInformationState(self._load()["log_information"])
        except OSError:
            self.state = LogInformationState(self.state.value, "Couldn’t read the saved log setting.")
        return self.state

    def select(self, value: str) -> LogInformationState:
        if value not in ("essential", "all"):
            raise ValueError("Unknown log information choice")
        try:
            latest = self._load()
            preferences = Preferences({**latest, "log_information": value})
            self._save(preferences)
        except OSError:
            self.state = LogInformationState(
                self.state.value, "Couldn’t save the log setting. Try again.",
            )
            return self.state
        self.state = LogInformationState(value)
        if self._on_saved is not None:
            self._on_saved(preferences)
        return self.state

from __future__ import annotations

from caveviewer.gui import sample_maps_dialog


class FakeOwner:
    def __init__(self, topmost=False):
        self.topmost = topmost
        self.exists = True
        self.calls = []

    def attributes(self, name, *value):
        assert name == "-topmost"
        if not value:
            self.calls.append(("get_topmost", self.topmost))
            return self.topmost
        self.topmost = value[0]
        self.calls.append(("set_topmost", self.topmost))

    def lift(self):
        self.calls.append(("lift",))

    def focus_force(self):
        self.calls.append(("focus_force",))

    def update_idletasks(self):
        self.calls.append(("update_idletasks",))

    def winfo_exists(self):
        return self.exists


class FakeFileDialog:
    def __init__(self, owner, result="/chosen/folder"):
        self.owner = owner
        self.result = result
        self.options = None

    def askdirectory(self, **options):
        assert self.owner.topmost is True
        self.options = options
        return self.result


def test_download_start_reuses_action_area_as_cancel_button_without_prompt():
    action_button = object()
    configured_actions = []

    def set_action_button(button, text, command):
        configured_actions.append((button, text, command))

    cancel_event = sample_maps_dialog._activate_download_cancel_button(
        action_button, set_action_button
    )

    assert len(configured_actions) == 1
    configured_button, text, command = configured_actions[0]
    assert configured_button is action_button
    assert text == "Cancel"
    assert not cancel_event.is_set()

    command()

    assert cancel_event.is_set()


def test_save_directory_chooser_is_owned_focused_and_temporarily_topmost():
    owner = FakeOwner(topmost=False)
    file_dialog = FakeFileDialog(owner)

    result = sample_maps_dialog._ask_directory_in_front(
        file_dialog,
        owner,
        title="Save Test Cave to...",
        initialdir="/maps",
    )

    assert result == "/chosen/folder"
    assert file_dialog.options == {
        "title": "Save Test Cave to...",
        "initialdir": "/maps",
        "parent": owner,
    }
    assert owner.topmost is False
    assert owner.calls == [
        ("get_topmost", False),
        ("set_topmost", True),
        ("lift",),
        ("focus_force",),
        ("update_idletasks",),
        ("set_topmost", False),
        ("lift",),
        ("focus_force",),
    ]


def test_save_directory_chooser_restores_topmost_state_after_failure():
    owner = FakeOwner(topmost=True)

    class FailingFileDialog:
        def askdirectory(self, **_options):
            assert owner.topmost is True
            raise RuntimeError("native chooser failed")

    try:
        sample_maps_dialog._ask_directory_in_front(
            FailingFileDialog(),
            owner,
            title="Save Test Cave to...",
            initialdir="/maps",
        )
    except RuntimeError as error:
        assert str(error) == "native chooser failed"
    else:
        raise AssertionError("chooser failure should propagate")

    assert owner.topmost is True

"""Exercise choice popover interaction without touching application preferences."""

import tkinter as tk

import pytest

from caveviewer.gui.choice_control import RoundedChoiceControl
from caveviewer.gui.help_style import HELP_VISUAL_METRICS, HELP_VISUAL_PALETTE


pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    yield root
    root.destroy()


@pytest.fixture(params=[1.0, 1.5])
def control(tk_root, request):
    root = tk_root
    px = lambda value: round(value * request.param)
    root.geometry("500x360+20+20")
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    before = tk.Button(root, text="Before")
    before.pack()
    selected = []
    choice = RoundedChoiceControl(
        root, choices=(("essential", "Essential"), ("all", "All")),
        value="essential", on_selected=selected.append, font=("Arial", -14, "bold"),
        metrics=HELP_VISUAL_METRICS.scaled(px), palette=HELP_VISUAL_PALETTE,
        width=px(160), px=px,
    )
    choice.widget.pack()
    after = tk.Button(root, text="After")
    after.pack()
    root.update()
    root.focus_force()
    root.update()
    try:
        yield root, choice, selected, before, after
    finally:
        for widget in list(root.winfo_children()):
            if widget.winfo_exists():
                widget.destroy()
        assert not errors


def open_choice(root, choice):
    choice.button.focus_set()
    root.update()
    choice.button.event_generate("<Down>")
    root.update()
    assert choice._menu is not None
    assert root.focus_get() == choice._menu


def test_keyboard_cancel_and_commit_leave_value_with_owner(control):
    root, choice, selected, _, _ = control
    open_choice(root, choice)
    choice._menu.event_generate("<Down>")
    choice._menu.event_generate("<Escape>")
    root.update()
    assert selected == []
    assert choice.button.cget("text") == "Essential"
    assert root.focus_get() == choice.button
    assert choice._menu is None

    open_choice(root, choice)
    choice._menu.event_generate("<End>")
    choice._menu.event_generate("<Return>")
    root.update()
    assert selected == ["all"]
    # A failed save need not revert an optimistic change: the control waits
    # for the owner to confirm the saved value.
    assert choice.button.cget("text") == "Essential"
    choice.set_value("all")
    open_choice(root, choice)
    assert choice._active == 1
    assert choice.button.cget("text") == "All"


@pytest.mark.parametrize("label", [False, True])
def test_pointer_can_select_label_or_full_row(control, label):
    root, choice, selected, _, _ = control
    open_choice(root, choice)
    target = choice._rows[1] if label else choice._menu
    x, y = (4, 4) if label else (choice._px(145), choice._px(60))
    target.event_generate("<ButtonPress-1>", x=x, y=y)
    target.event_generate("<ButtonRelease-1>", x=x, y=y)
    root.update()
    assert selected == ["all"]
    assert choice._menu is None


@pytest.mark.parametrize("event", ["<Tab>", "<Shift-Tab>"])
def test_tab_dismisses_and_follows_opener_focus_order(control, event):
    root, choice, selected, before, after = control
    open_choice(root, choice)
    choice._menu.event_generate(event)
    root.update()
    assert root.focus_get() == (before if event == "<Shift-Tab>" else after)
    assert choice._menu is None
    assert selected == []


@pytest.mark.parametrize("action", ["outside", "focus", "resize", "scroll", "hide", "destroy"])
def test_dismissal_removes_only_owned_root_bindings_and_callbacks(control, action):
    root, choice, selected, _, after = control
    existing = root.bind("<ButtonPress-1>", lambda _event: None, add="+")
    open_choice(root, choice)
    bindings = tuple(choice._bindings)
    if action == "outside":
        after.event_generate("<ButtonPress-1>")
    elif action == "focus":
        after.focus_set()
    elif action == "resize":
        root.geometry("520x380")
    elif action == "scroll":
        root.event_generate("<MouseWheel>", delta=-120)
    elif action == "hide":
        choice.widget.pack_forget()
    else:
        choice._schedule_focus_check(None)
        choice.widget.destroy()
    root.update()
    assert choice._menu is None
    assert choice._focus_check is None
    assert choice._bindings == []
    assert existing in root.bind("<ButtonPress-1>")
    assert all(callback not in root.bind(sequence) for sequence, callback in bindings)
    assert selected == []


def test_menu_flips_above_near_window_bottom(control):
    root, choice, _, _, _ = control
    choice.widget.pack_forget()
    choice.widget.place(x=400, y=300)
    root.update()
    open_choice(root, choice)
    assert choice._menu.winfo_y() + choice._menu.winfo_height() == 300 - choice._px(4)
    assert choice._menu.winfo_x() + choice._menu.winfo_width() <= root.winfo_width()


def test_hover_keeps_check_on_saved_choice_and_matches_scaled_geometry(control):
    root, choice, selected, _, _ = control
    open_choice(root, choice)
    menu = choice._menu
    assert choice.widget.winfo_height() == choice._px(44)
    assert menu.winfo_width() == choice._px(160)
    assert menu.winfo_height() == choice._px(40) * 2
    assert menu.winfo_y() - (choice.widget.winfo_y() + choice.height) == choice._px(4)
    menu.event_generate("<Motion>", x=choice._px(140), y=choice._px(60))
    root.update()
    check = next(item for item in menu.find_withtag("choice-state") if menu.type(item) == "line")
    assert choice._active == 1
    assert max(menu.coords(check)[1::2]) < choice._px(40)
    assert selected == []
    choice.set_value("all")
    check = next(item for item in menu.find_withtag("choice-state") if menu.type(item) == "line")
    assert min(menu.coords(check)[1::2]) > choice._px(40)


def test_tab_wraps_to_opener_when_no_other_controls_exist(control):
    root, choice, _, before, after = control
    before.destroy()
    after.destroy()
    root.update()
    open_choice(root, choice)
    choice._menu.event_generate("<Tab>")
    root.update()
    assert choice._menu is None
    assert root.focus_get() == choice.button

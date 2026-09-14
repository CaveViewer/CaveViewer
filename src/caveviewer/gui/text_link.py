"""Keep native cursor and focus presentation consistent for Tk text links."""


def configure_text_link(label, *, enabled: bool = True) -> None:
    """Style link interactivity; callers own activation bindings and actions."""
    label.config(takefocus=enabled, cursor="hand2" if enabled else "")

"""Resolve the focused platform adapters used by a viewer launch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caveviewer.branding import BrandingAssets, resolve_branding_assets
from caveviewer.gui.platform.presentation import PresentationProfile, get_presentation_profile
from caveviewer.gui.platform.presentation_actions import (
    PresentationActionsAdapter,
    create_presentation_actions_adapter,
)
from caveviewer.gui.platform.recording_process import (
    RecordingProcessAdapter,
    create_recording_process_adapter,
)
from caveviewer.gui.platform.saved_artifact_reveal import (
    SavedArtifactRevealAdapter,
    create_saved_artifact_reveal_adapter,
)
from caveviewer.gui.platform.window_backend import (
    WindowBackendAdapter,
    create_window_backend_adapter,
)

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


def presentation_profile_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> PresentationProfile:
    profile = getattr(platform_runtime, "presentation_profile", None)
    return profile or get_presentation_profile()


def presentation_actions_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> PresentationActionsAdapter:
    actions = getattr(platform_runtime, "presentation_actions_adapter", None)
    return actions or create_presentation_actions_adapter()


def window_backend_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> WindowBackendAdapter:
    adapter = getattr(platform_runtime, "window_backend_adapter", None)
    return adapter or create_window_backend_adapter()


def saved_artifact_reveal_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> SavedArtifactRevealAdapter:
    if platform_runtime is not None:
        return platform_runtime.saved_artifact_reveal_adapter
    return create_saved_artifact_reveal_adapter()


def recording_process_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> RecordingProcessAdapter:
    if platform_runtime is not None:
        return platform_runtime.recording_process_adapter
    return create_recording_process_adapter()


def branding_assets_for_runtime(
    platform_runtime: PlatformRuntime | None,
) -> BrandingAssets:
    assets = getattr(platform_runtime, "branding_assets", None)
    return assets or resolve_branding_assets(environ={})


def runtime_app_icon_path(platform_runtime: PlatformRuntime | None) -> str:
    assets = branding_assets_for_runtime(platform_runtime)
    profile = getattr(platform_runtime, "profile", None)
    platform_name = (
        profile.platform_name
        if profile is not None
        else get_presentation_profile().platform_name
    )
    return str(assets.application_icon_for(platform_name))

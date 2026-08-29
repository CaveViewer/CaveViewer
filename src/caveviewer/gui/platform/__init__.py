"""Public factories and types for platform-specific desktop integration."""

from .app_identity import LINUX_WINDOW_INSTANCE_NAME, tk_root_options
from .desktop_services import (
    DesktopServiceError,
    DesktopInhibitor,
    DesktopServices,
    DirectorySelection,
    FileSelection,
    get_desktop_services,
)
from .presentation import (
    DialogLayoutPolicy,
    PreferencesDialogLayoutPolicy,
    PresentationProfile,
    SplashLayoutPolicy,
    font_candidates_for_profile,
    get_presentation_profile,
    select_presentation_profile,
)
from .presentation_actions import PresentationActionsAdapter
from .runtime import (
    DesktopNotificationPreflight,
    DirectorySelectionPreflight,
    FileSelectionPreflight,
    IdleSuspendInhibitionPreflight,
    PlatformProfile,
    PlatformRuntime,
    VideoRecordingPreflight,
    ViewerLaunchPreflight,
    create_platform_runtime,
)
from .update_package_reveal import UpdatePackageRevealAdapter
from .update_package_storage import UpdatePackageStorageAdapter
from .update_package_install import UpdatePackageInstallerAdapter
from .saved_artifact_reveal import SavedArtifactRevealAdapter
from .diagnostic_log_reveal import DiagnosticLogRevealAdapter
from .recording_process import RecordingProcessAdapter
from .tls_trust import TlsTrustAdapter
from .window_backend import (
    ViewerWindowLaunchRequest,
    WindowBackendAdapter,
)

__all__ = [
    "get_presentation_profile",
    "select_presentation_profile",
    "font_candidates_for_profile",
    "get_desktop_services",
    "LINUX_WINDOW_INSTANCE_NAME",
    "tk_root_options",
    "DialogLayoutPolicy",
    "PreferencesDialogLayoutPolicy",
    "SplashLayoutPolicy",
    "PresentationProfile",
    "PresentationActionsAdapter",
    "DesktopServiceError",
    "DesktopInhibitor",
    "DesktopServices",
    "DirectorySelection",
    "FileSelection",
    "DesktopNotificationPreflight",
    "DirectorySelectionPreflight",
    "FileSelectionPreflight",
    "IdleSuspendInhibitionPreflight",
    "PlatformProfile",
    "PlatformRuntime",
    "VideoRecordingPreflight",
    "ViewerLaunchPreflight",
    "UpdatePackageRevealAdapter",
    "UpdatePackageStorageAdapter",
    "UpdatePackageInstallerAdapter",
    "SavedArtifactRevealAdapter",
    "DiagnosticLogRevealAdapter",
    "RecordingProcessAdapter",
    "TlsTrustAdapter",
    "ViewerWindowLaunchRequest",
    "WindowBackendAdapter",
    "create_platform_runtime",
]

"""Public factories and types for platform-specific desktop integration."""

from .app_identity import LINUX_WINDOW_INSTANCE_NAME, tk_root_options
from .base import DialogLayoutPolicy, PreferencesDialogLayoutPolicy, SplashLayoutPolicy
from .desktop_services import (
    DesktopServiceError,
    DesktopInhibitor,
    DesktopServices,
    DirectorySelection,
    FileSelection,
    get_desktop_services,
)
from .factory import get_platform_adapter, get_splash_platform_adapter
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
from .saved_recording_reveal import SavedRecordingRevealAdapter
from .recording_process import RecordingProcessAdapter
from .tls_trust import TlsTrustAdapter
from .window_backend import (
    ViewerWindowLaunchRequest,
    WindowBackendAdapter,
)
from .linux import LinuxSplashPlatformAdapter
from .macos import MacOSSplashPlatformAdapter
from .windows import WindowsSplashPlatformAdapter

__all__ = [
    "get_platform_adapter",
    "get_splash_platform_adapter",
    "get_desktop_services",
    "LINUX_WINDOW_INSTANCE_NAME",
    "tk_root_options",
    "DialogLayoutPolicy",
    "PreferencesDialogLayoutPolicy",
    "SplashLayoutPolicy",
    "DesktopServiceError",
    "DesktopInhibitor",
    "DesktopServices",
    "DirectorySelection",
    "FileSelection",
    "MacOSSplashPlatformAdapter",
    "WindowsSplashPlatformAdapter",
    "LinuxSplashPlatformAdapter",
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
    "SavedRecordingRevealAdapter",
    "RecordingProcessAdapter",
    "TlsTrustAdapter",
    "ViewerWindowLaunchRequest",
    "WindowBackendAdapter",
    "create_platform_runtime",
]

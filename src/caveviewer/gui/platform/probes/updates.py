"""Static typed update profiles, target configuration, and capability probes.

The application composes one immutable :class:`UpdateProfile` from its
platform and process architecture.  That profile contains release-policy data
only: it does not create platform adapters, contact the network, or inspect a
manifest.  Configuration overrides then produce an ``UpdateTarget`` that the
network client can use without depending on ``SplashPlatformAdapter``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from caveviewer.core.capabilities import CapabilityResult, CapabilitySource
from caveviewer.gui.update_signature import default_manifest_signature_url


_VALID_MANIFEST_CHANNELS = frozenset({"stable", "prerelease"})
_DEFAULT_UPDATE_REPOSITORY = "CaveViewer/CaveViewer"
_DEFAULT_UPDATE_USER_AGENT = "CaveViewer-UpdateChecker"
_PACKAGE_KINDS_BY_CHANNEL: dict[str, frozenset[str]] = {
    "macos_app": frozenset({"dmg", "pkg"}),
    "windows_app": frozenset({"zip", "msi", "exe"}),
    "linux_app": frozenset({"appimage", "deb", "rpm", "tar.gz"}),
}


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _normalized_keys(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(_clean(value) for value in values if _clean(value))
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one key")
    return normalized


@dataclass(frozen=True, slots=True)
class UpdateManifestSchema:
    """Manifest aliases and package policy for one install channel.

    The schema is immutable process metadata, not data obtained from a remote
    manifest.  Keeping it alongside the target prevents the update checker
    from consulting a broad platform adapter while parsing signed metadata.
    """

    download_url_keys: tuple[str, ...]
    download_size_keys: tuple[str, ...]
    download_sha256_keys: tuple[str, ...]
    allowed_package_kinds: frozenset[str] | None
    missing_download_url_message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "download_url_keys",
            _normalized_keys(self.download_url_keys, field_name="download URL keys"),
        )
        object.__setattr__(
            self,
            "download_size_keys",
            _normalized_keys(self.download_size_keys, field_name="download size keys"),
        )
        object.__setattr__(
            self,
            "download_sha256_keys",
            _normalized_keys(
                self.download_sha256_keys,
                field_name="download SHA-256 keys",
            ),
        )
        if self.allowed_package_kinds is not None:
            normalized_kinds = frozenset(
                _clean(kind).lower()
                for kind in self.allowed_package_kinds
                if _clean(kind)
            )
            if not normalized_kinds:
                raise ValueError(
                    "allowed package kinds must be non-empty when constrained"
                )
            object.__setattr__(self, "allowed_package_kinds", normalized_kinds)
        missing_message = _clean(self.missing_download_url_message)
        if not missing_message:
            raise ValueError("missing download URL message must be non-empty")
        object.__setattr__(self, "missing_download_url_message", missing_message)


@dataclass(frozen=True, slots=True)
class UpdateProfile:
    """Process-stable release policy for one operating-system install target."""

    install_channel: str
    supports_automatic_update: bool
    unsupported_message: str
    default_repository: str
    manifest_directory: str | None
    user_agent: str
    manifest_schema: UpdateManifestSchema

    def __post_init__(self) -> None:
        install_channel = _clean(self.install_channel).lower()
        if not install_channel:
            raise ValueError("update install channel must be non-empty")
        object.__setattr__(self, "install_channel", install_channel)

        unsupported_message = _clean(self.unsupported_message)
        if not unsupported_message:
            raise ValueError("unsupported update message must be non-empty")
        object.__setattr__(self, "unsupported_message", unsupported_message)

        default_repository = _clean(self.default_repository)
        if not default_repository:
            raise ValueError("default update repository must be non-empty")
        object.__setattr__(self, "default_repository", default_repository)

        manifest_directory = (
            None if self.manifest_directory is None else _clean(self.manifest_directory)
        )
        if manifest_directory == "":
            manifest_directory = None
        object.__setattr__(self, "manifest_directory", manifest_directory)

        user_agent = _clean(self.user_agent)
        if not user_agent:
            raise ValueError("update user agent must be non-empty")
        object.__setattr__(self, "user_agent", user_agent)

    def default_manifest_url(
        self,
        repository: str,
        branch: str,
        manifest_channel: str,
    ) -> str:
        """Return this profile's signed-manifest URL without platform calls."""
        if self.manifest_directory is None:
            return ""
        return (
            f"https://raw.githubusercontent.com/{_clean(repository)}/"
            f"{_clean(branch)}/{self.manifest_directory}/"
            f"{_clean(manifest_channel)}.json"
        )


@dataclass(frozen=True, slots=True)
class UpdateConfiguration:
    """Resolved signed-manifest configuration for one application process."""

    repository: str
    branch: str
    manifest_channel: str
    manifest_url: str
    manifest_signature_url: str
    source: CapabilitySource


@dataclass(frozen=True, slots=True)
class UpdateTarget:
    """One fully configured signed-manifest target for the network client."""

    install_channel: str
    manifest_url: str
    manifest_signature_url: str
    user_agent: str
    manifest_schema: UpdateManifestSchema

    def __post_init__(self) -> None:
        for field_name in (
            "install_channel",
            "manifest_url",
            "manifest_signature_url",
            "user_agent",
        ):
            value = _clean(getattr(self, field_name))
            if not value:
                raise ValueError(f"update target {field_name} must be non-empty")
            object.__setattr__(self, field_name, value)


def _manifest_schema(
    *,
    download_url_keys: tuple[str, ...],
    download_size_keys: tuple[str, ...],
    download_sha256_keys: tuple[str, ...],
    install_channel: str,
    missing_download_url_message: str,
) -> UpdateManifestSchema:
    return UpdateManifestSchema(
        download_url_keys=download_url_keys,
        download_size_keys=download_size_keys,
        download_sha256_keys=download_sha256_keys,
        allowed_package_kinds=_PACKAGE_KINDS_BY_CHANNEL.get(install_channel),
        missing_download_url_message=missing_download_url_message,
    )


def select_update_profile(
    *,
    platform_name: str,
    machine: str,
) -> UpdateProfile:
    """Select static release metadata from injected process facts.

    This is deliberately a pure transform.  ``PlatformRuntime`` calls it once
    after command-line overrides, while tests can provide exact platform and
    architecture inputs without constructing a broad platform adapter.
    """
    normalized_platform = _clean(platform_name).lower()
    normalized_machine = _clean(machine).lower()

    if normalized_platform == "darwin":
        architecture = (
            "arm64"
            if normalized_machine in {"arm64", "aarch64"}
            else "x86_64"
            if normalized_machine in {"x86_64", "amd64", "x64"}
            else "unsupported"
        )
        return UpdateProfile(
            install_channel="macos_app",
            supports_automatic_update=True,
            unsupported_message=(
                "Unsupported install channel 'macos_app'. macOS updates are DMG-only."
            ),
            default_repository=_DEFAULT_UPDATE_REPOSITORY,
            manifest_directory=f"updates/macos/{architecture}",
            user_agent=_DEFAULT_UPDATE_USER_AGENT,
            manifest_schema=_manifest_schema(
                download_url_keys=(
                    "download_url_macosx_dmg",
                    "download_url_macos",
                    "download_url",
                ),
                download_size_keys=(
                    "download_size_bytes_macosx_dmg",
                    "download_size_bytes_macos",
                    "download_size_bytes",
                ),
                download_sha256_keys=(
                    "sha256_macosx_dmg",
                    "sha256_macos",
                    "sha256",
                ),
                install_channel="macos_app",
                missing_download_url_message=(
                    "Update manifest is missing required field: "
                    "download_url_macosx_dmg."
                ),
            ),
        )

    if normalized_platform.startswith("win"):
        return UpdateProfile(
            install_channel="windows_app",
            supports_automatic_update=True,
            unsupported_message=(
                "Unsupported install channel 'windows_app' on Windows. "
                "Expected channel: windows_app."
            ),
            default_repository=_DEFAULT_UPDATE_REPOSITORY,
            manifest_directory="updates/windows",
            user_agent=_DEFAULT_UPDATE_USER_AGENT,
            manifest_schema=_manifest_schema(
                download_url_keys=(
                    "download_url_windows_msi",
                    "download_url_windows_exe",
                    "download_url_windows_zip",
                    "download_url_windows",
                ),
                download_size_keys=(
                    "download_size_bytes_windows_msi",
                    "download_size_bytes_windows_exe",
                    "download_size_bytes_windows_zip",
                    "download_size_bytes_windows",
                ),
                download_sha256_keys=(
                    "sha256_windows_msi",
                    "sha256_windows_exe",
                    "sha256_windows_zip",
                    "sha256_windows",
                ),
                install_channel="windows_app",
                missing_download_url_message=(
                    "Update manifest is missing a Windows download URL."
                ),
            ),
        )

    if normalized_platform.startswith("linux"):
        supported = normalized_machine in {"x86_64", "amd64"}
        reported_machine = _clean(machine) or "unknown"
        return UpdateProfile(
            install_channel="linux_app",
            supports_automatic_update=supported,
            unsupported_message=(
                "Linux automatic updates are available only for x86_64 builds. "
                f"This machine reports architecture '{reported_machine}', so automatic "
                "updates are disabled."
                if not supported
                else (
                    "Unsupported install channel 'linux_app' on Linux. "
                    "Expected channel: linux_app."
                )
            ),
            default_repository=_DEFAULT_UPDATE_REPOSITORY,
            manifest_directory="updates/linux/x86_64" if supported else None,
            user_agent=_DEFAULT_UPDATE_USER_AGENT,
            manifest_schema=_manifest_schema(
                download_url_keys=(
                    "download_url_linux_appimage",
                    "download_url_linux_deb",
                    "download_url_linux_rpm",
                    "download_url_linux_tar_gz",
                    "download_url_linux",
                    "download_url",
                ),
                download_size_keys=(
                    "download_size_bytes_linux_appimage",
                    "download_size_bytes_linux_deb",
                    "download_size_bytes_linux_rpm",
                    "download_size_bytes_linux_tar_gz",
                    "download_size_bytes_linux",
                    "download_size_bytes",
                ),
                download_sha256_keys=(
                    "sha256_linux_appimage",
                    "sha256_linux_deb",
                    "sha256_linux_rpm",
                    "sha256_linux_tar_gz",
                    "sha256_linux",
                    "sha256",
                ),
                install_channel="linux_app",
                missing_download_url_message=(
                    "Update manifest is missing a Linux download URL."
                ),
            ),
        )

    return UpdateProfile(
        install_channel="unsupported",
        supports_automatic_update=False,
        unsupported_message="Unsupported install channel 'unsupported'.",
        default_repository=_DEFAULT_UPDATE_REPOSITORY,
        # Preserve the old conservative default's URL shape for legacy
        # diagnostics while the unavailable target remains fail-closed.
        manifest_directory="updates/macos",
        user_agent=_DEFAULT_UPDATE_USER_AGENT,
        manifest_schema=_manifest_schema(
            download_url_keys=("download_url",),
            download_size_keys=("download_size_bytes",),
            download_sha256_keys=("sha256",),
            install_channel="unsupported",
            missing_download_url_message=(
                "Update manifest is missing required field: download_url."
            ),
        ),
    )


def _environment_value(
    environment: Mapping[str, str], key: str, default: str
) -> tuple[str, bool]:
    if key not in environment:
        return default, False
    return _clean(environment[key]), True


def build_update_configuration(
    update_profile: UpdateProfile,
    *,
    environment: Mapping[str, str] | None = None,
) -> UpdateConfiguration:
    """Resolve update settings when the runtime is composed, never at import.

    Explicit environment values are a local configuration override.  They may
    disable update checking by setting a manifest or signature URL to blank,
    but they cannot make an unsupported profile eligible.
    """
    values = environment if environment is not None else os.environ
    overridden = False

    repository, is_overridden = _environment_value(
        values,
        "CAVEVIEWER_GITHUB_REPO",
        update_profile.default_repository,
    )
    overridden = overridden or is_overridden
    branch, is_overridden = _environment_value(values, "CAVEVIEWER_UPDATE_BRANCH", "main")
    overridden = overridden or is_overridden
    branch = branch or "main"

    manifest_channel, is_overridden = _environment_value(
        values,
        "CAVEVIEWER_UPDATE_CHANNEL",
        "stable",
    )
    overridden = overridden or is_overridden
    manifest_channel = (manifest_channel or "stable").lower()
    if manifest_channel not in _VALID_MANIFEST_CHANNELS:
        manifest_channel = "stable"

    default_manifest_url = update_profile.default_manifest_url(
        repository,
        branch,
        manifest_channel,
    )
    manifest_url, is_overridden = _environment_value(
        values,
        "CAVEVIEWER_UPDATE_MANIFEST_URL",
        default_manifest_url,
    )
    overridden = overridden or is_overridden

    default_signature_url = (
        default_manifest_signature_url(manifest_url) if manifest_url else ""
    )
    manifest_signature_url, is_overridden = _environment_value(
        values,
        "CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL",
        default_signature_url,
    )
    overridden = overridden or is_overridden

    return UpdateConfiguration(
        repository=repository,
        branch=branch,
        manifest_channel=manifest_channel,
        manifest_url=manifest_url,
        manifest_signature_url=manifest_signature_url,
        source=(
            CapabilitySource.USER_OVERRIDE
            if overridden
            else CapabilitySource.DETECTED
        ),
    )


def probe_automatic_update(
    update_profile: UpdateProfile,
    configuration: UpdateConfiguration,
) -> CapabilityResult[UpdateTarget]:
    """Report whether one typed profile can safely use automatic updates."""
    install_channel = update_profile.install_channel
    if not update_profile.supports_automatic_update:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_target_unsupported",
            evidence={"install_channel": install_channel or "unknown"},
        )
    if not configuration.manifest_url:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_manifest_unconfigured",
            source=configuration.source,
            evidence={"install_channel": install_channel},
        )
    if not configuration.manifest_signature_url:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_signature_unconfigured",
            source=configuration.source,
            evidence={"install_channel": install_channel},
        )

    return CapabilityResult.available(
        UpdateTarget(
            install_channel=install_channel,
            manifest_url=configuration.manifest_url,
            manifest_signature_url=configuration.manifest_signature_url,
            user_agent=update_profile.user_agent,
            manifest_schema=update_profile.manifest_schema,
        ),
        reason_code="automatic_update_target_available",
        source=configuration.source,
        evidence={"install_channel": install_channel},
    )


def detect_update_package_kind(download_url: str) -> str:
    """Classify a package URL using the stable release-format suffix rules."""
    normalized_url = _clean(download_url).lower()
    for suffix in ("tar.gz", "appimage", "msi", "exe", "deb", "rpm", "dmg", "pkg", "zip"):
        if normalized_url.endswith(f".{suffix}"):
            return suffix
    return "unknown"

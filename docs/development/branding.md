# Branding

This document defines CaveViewer's visual-branding surface and the boundary
between replaceable artwork and stable product identity. The implementation
described here is intentionally developer-facing: a branding profile may be
selected while evaluating or packaging a build, but an installed signed build
does not change its brand at runtime.

## Ownership model

Branding inputs are semantic source artwork and presentation tokens. Derived
files such as ICO frames, an iconset, and hicolor PNGs are exports of those
inputs. Runtime and packaging consumers must request a semantic role instead
of selecting an unrelated concrete image path.

The branding system may control these roles:

- application mark;
- Windows, macOS, and Linux application-icon overrides;
- About-page mark;
- loading-indicator mark;
- bounded loading-ring presentation tokens; and
- optional DMG volume or background artwork.

The application composition boundary resolves the selected profile once and
passes an immutable snapshot to GUI consumers. The exporter resolves the same
profile for packaging. Core domain code does not depend on GUI image formats,
Tk, OpenGL, or platform packaging tools.

## Consumer and output matrix

| Platform or area | User-visible surface | Semantic input | Derived or inherited output | Owner |
| --- | --- | --- | --- | --- |
| All desktop platforms | About page | About mark | Runtime RGBA image | GUI composition |
| All desktop platforms | Import/loading progress | Loading mark and ring tokens | Texture, ring, text-only, or ring-only fallback | GUI presentation |
| Windows | Window upper-left icon, taskbar, title bar, native dialogs | Windows application icon | Runtime PNG and inherited window icon | GUI/platform adapter |
| Windows | Executable, installer, Start menu, shortcuts, pinned taskbar, Installed Apps and uninstaller | Windows application icon | Multi-frame ICO embedded by PyInstaller and Inno Setup | Windows packaging |
| macOS | Application windows, Dock, Command-Tab, Finder and Applications | macOS application icon | Complete iconset and ICNS in the application bundle | macOS packaging |
| macOS | Distributed disk image | macOS application icon; optional DMG artwork | Bundled `.app`; optional volume icon/background | macOS packaging |
| Linux | Runtime window and task switcher grouping | Linux application icon | Runtime RGBA image; shell grouping inherits stable application identity | GUI/platform adapter |
| Linux | App grid, Dock, launcher, AppImage and desktop integration | Linux application icon | Hicolor PNGs, root icon and `.DirIcon` | Linux packaging |
| Store and release metadata | AppStream/store presentation | Accepted application icon and screenshots | Metadata images and screenshots | Release/documentation workflow |
| Web and documentation | Favicon, social preview, site imagery and screenshots | Accepted brand artwork | Web-specific exports | Documentation workflow |
| Future document integration | File associations for supported map formats | Optional document marks | Platform-specific document icons | Separate approved feature |

Operating-system surfaces that inherit the executable or bundle icon do not
receive independent artwork unless native behavior requires it. Cache refresh
instructions are part of platform verification because old taskbar, Dock, or
desktop-shell icons can survive a correct package update.

## Stable product identity

A visual profile must not change any of the following:

- the product name `CaveViewer` or executable and installer names;
- the macOS bundle identifier `com.caveviewer.CaveViewer`;
- the Linux application ID `io.github.caveviewer.caveviewer`, desktop filename,
  icon basename, Wayland application ID, or `StartupWMClass`;
- signing identities, notarization configuration, or release channels;
- update URLs, update-manifest paths, or artifact naming contracts; or
- user preference, cache, log, download, and other application-data roots.

Changing one of these values is a product-identity or compatibility migration,
not a branding-profile swap. It requires its own plan and release validation.

## Source and derived-asset policy

Profiles and their source artwork are versioned inputs. Every source records
its provenance and license. Export policy defines dimensions, alpha handling,
safe areas, resampling, and platform overrides centrally. Temporary iconsets,
ICNS containers, package staging trees, contact sheets, and package artifacts
remain ignored build output unless the repository explicitly adopts a tracked
derived-asset policy.

Production packages reject incomplete profiles. Source runs may use the
documented default profile when no developer override is selected. A frozen,
signed package embeds its resolved brand and does not honor a mutable external
profile afterward.

## Review contract

Automated checks validate the profile schema, source provenance, required
dimensions, alpha behavior, deterministic exports, and agreement between
runtime and packaging consumers. Human review covers exact 16-, 24-, and
32-pixel previews on light and dark surfaces plus native package behavior on
Windows, macOS, Ubuntu, and Fedora.

Screenshots, website assets, and store metadata are updated after a candidate
brand is accepted rather than for every developer experiment.

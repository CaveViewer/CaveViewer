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
- Linux full-color scalable and High Contrast symbolic application icons;
- About-page mark;
- loading-indicator mark and progress mask;
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
| All desktop platforms | Initial startup | Dark cave background, launch copy, and shared progress colors | Text-and-bar launch surface gated by minimum display time and composed-main-screen readiness | GUI composition |
| All desktop platforms | Import/loading progress | Loading mark, progress mask, and ring tokens | Static mark plus masked progress rim; circular ring, text-only, or ring-only failure fallback | GUI presentation |
| Windows | Window upper-left icon, taskbar, title bar, native dialogs | Windows application icon | Runtime PNG and inherited window icon | GUI/platform adapter |
| Windows | Executable, installer, Start menu, shortcuts, pinned taskbar, Installed Apps and uninstaller | Windows application icon | Multi-frame ICO embedded by PyInstaller and Inno Setup | Windows packaging |
| macOS | Application windows, Dock, Command-Tab, Finder and Applications | macOS application icon | Complete iconset and ICNS in the application bundle | macOS packaging |
| macOS | Distributed disk image | macOS application icon; optional DMG artwork | Bundled `.app`; optional volume icon/background | macOS packaging |
| Linux | Runtime window and task switcher grouping | Linux application icon | Runtime RGBA image; shell grouping inherits stable application identity | GUI/platform adapter |
| Linux | App grid, Dock, launcher, AppImage and desktop integration | Linux application icon plus scalable and symbolic variants | Hicolor PNGs/SVGs, root icon and `.DirIcon` | Linux packaging |
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

## Developer workflow

A profile is a directory containing `branding.v1.json` and its referenced PNG
sources. Copy the bundled default profile to an ignored working directory,
update its identity, provenance, roles, hashes, and artwork, then validate it:

```powershell
caveviewer-branding --profile .work/brands/candidate validate
```

Generate every platform derivative and the comparison sheet together:

```powershell
caveviewer-branding --profile .work/brands/candidate export `
  --output .work/branding-preview --replace
```

Review `previews/contact-sheet.png` for exact 16-, 24-, and 32-pixel samples
on light and dark surfaces. `export-summary.v1.json` records the selected
profile, semantic-role source hashes, and every output hash.

For an editable source run, select the candidate without changing Preferences:

```powershell
$env:CAVEVIEWER_BRAND_PROFILE = Resolve-Path .work/brands/candidate
caveviewer
Remove-Item Env:CAVEVIEWER_BRAND_PROFILE
```

Frozen signed applications ignore external runtime profiles. All native build
scripts accept the same variable and embed the selected profile and provenance.
Omitting it selects the bundled `default` profile.

## Profile fields

- `schema_version` is currently `1`.
- `profile_id` and `provenance` identify and license the source.
- `assets` declares relative paths, SHA-256 hashes, minimum dimensions, alpha,
  square geometry, and safe-area inset.
- `roles` maps application, About, loading mark/mask, platform raster-icon,
  Linux scalable-icon, and Linux symbolic-icon semantics. The loading progress
  mask is an alpha-only source aligned with the loading mark; its
  non-transparent pixels define the brand shape that receives track and fill
  colors.
- `loading_ring` selects `text_only`, `ring_only`, or `ring_with_mark` and
  supplies validated fill and track colors.

Correct validation errors in sources or the manifest, then export again. Never
repair a profile by editing generated output.

## Generated and tracked assets

Manifests and accepted source artwork are authoritative. Iconsets, contact
sheets, summaries, hicolor trees, ICNS files, and package staging trees belong
under `build/`, `dist/`, or ignored `.work/`. The checked Windows ICO is a
compatibility alias, and tests require it to match the default export exactly.

Update website imagery, AppStream and documentation screenshots, favicons, and
social previews only after a profile is accepted.

## Native verification and icon caches

- Windows: inspect executable, installer, title bar, taskbar, Start menu,
  shortcuts, Installed Apps, and uninstaller. Unpin old shortcuts and refresh
  the per-user icon cache when Windows retains old artwork.
- macOS ARM64 and Intel: inspect Finder, Applications, Dock, Command-Tab, and
  the mounted DMG. Remove old Dock items and relaunch Finder/Dock when needed.
- Ubuntu and Fedora on GNOME Wayland and Xorg: inspect AppImage root icon, app
  grid, Dock/task switcher grouping, launcher, fixed hicolor sizes, scalable
  rendering, and High Contrast symbolic selection. Refresh
  `gtk-update-icon-cache` and `update-desktop-database` after replacement.

## Current platform-native icon policy

The default profile uses one accepted cave/light metaphor with separate
optical compositions: an un-enclosed Windows small-icon master, an enclosed
1024-pixel macOS master, and a transparent GNOME-weighted Linux master. Linux
also ships self-contained full-color scalable and monochrome symbolic SVGs
named from the stable application ID.

The macOS package continues to generate the complete traditional iconset and
ICNS because CaveViewer is assembled through PyInstaller rather than an Xcode
asset catalog. Apple's layered Icon Composer format requires Xcode-owned bundle
integration and appearance annotations. Adopt it only in a separate native
toolchain change that preserves the ICNS fallback and can be tested on both
supported macOS architectures; do not treat a flattened approximation as a
layered source.

The initial startup surface intentionally carries no independent logo or
product-title lockup. It uses the dark cave photograph, the sentence
`Preparing to explore what lies beneath...`, and the same subdued-track/amber-
fill flat progress language used by map loading. Composition milestones are
monotonic and remain below 100 percent until the map library, Preferences
surface, final geometry, and idle layout work needed by the first interactive
frame are ready. The launch surface is then dismissed only after both that
readiness signal and a three-second minimum visible duration are satisfied;
the remaining delay is scheduled on the Tk event loop and never implemented
as a blocking sleep.

Run package-only workflows and native smoke scripts before release. Stable
names, application IDs, signing, updates, and storage paths remain unchanged.

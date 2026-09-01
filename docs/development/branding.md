# Branding

This document defines CaveViewer's visual-branding surface and the boundary
between replaceable artwork and stable product identity. The implementation
described here is intentionally developer-facing: a branding profile may be
selected while evaluating or packaging a build, but an installed signed build
does not change its brand at runtime.

This is the canonical brand reference for people and automation. Human-readable
guidance explains the intent behind the system. The delimited JSON contract in
[Machine-readable brand contract](#machine-readable-brand-contract) records the
same rules in a versioned form suitable for automated validation. When prose
and the contract disagree, stop and update both in the same change; neither
representation silently overrides the other.

## Brand foundation

CaveViewer helps people explore and understand cave-survey data. Its visual
language comes from three ideas: the darkness and irregular geometry of a cave,
the controlled amber light that makes exploration possible, and the technical
precision of survey and navigation work. The result should feel capable,
focused, exploratory, and purpose-built for cave divers without becoming
aggressive or theatrical.

The primary audience is cave divers and people working with cave-survey data.
The identity may be bold, but it must avoid death imagery, skulls, disaster
tropes, military styling, and inaccurate equipment illustrations. Do not copy
or closely imitate a dive-equipment manufacturer's mark. Authenticity comes
from cave geometry, light, navigation, and survey language rather than from a
generic extreme-sports aesthetic.

Core attributes are:

- **Exploratory:** the mark and imagery invite the viewer into an unknown
  space rather than presenting a warning sign.
- **Capable:** controls and visual hierarchy are direct, calm, and legible in
  adverse viewing conditions.
- **Technical:** typography, maps, contours, and data presentation remain
  precise without making the product feel clinical.
- **Dark and luminous:** near-black surfaces establish depth; amber and warm
  yellow provide the controlled light.
- **Authentic:** cave-diving references must be correct, restrained, and
  useful to the concept.

## Iconography

The signature application mark is an irregular cave profile surrounding a
near-black void. A compact circular light source sits left of center and casts
a widening amber beam to the right. Large presentation artwork may add amber
survey contours and restrained material texture. Small shell icons reduce the
idea to the cave silhouette, thick rim, light source, and beam so it remains
recognizable at taskbar size.

Apply these rules to every production variant:

- Preserve the cave/light metaphor, left-to-right beam direction, near-black
  interior, and amber-to-yellow monochromatic treatment.
- Use optical centering. The irregular cave profile and beam do not need to be
  mathematically symmetrical, but their combined visual weight must appear
  centered in the platform container.
- Keep negative space around the light source and between the beam tip and cave
  rim. Neither element may touch or visually merge with the rim at small sizes.
- Keep one strong outer cave rim in 16-, 24-, and 32-pixel icons. Remove fine
  contours, reflections, bevels, or texture before they turn into noise.
- Use contours and texture only where the rendered size preserves them, such
  as About and high-resolution marketing artwork. Never depend on those details
  for recognition.
- Do not rotate, mirror, stretch, recolor, add a wordmark to, or place unrelated
  symbols inside the accepted mark. Do not introduce cyan or blue into the
  production mark.
- Preserve transparency for Windows and Linux runtime marks. Use the dedicated
  enclosed macOS composition rather than placing a transparent desktop mark on
  an arbitrary tile.
- Use the GNOME symbolic asset for High Contrast instead of reducing the
  full-color icon to grayscale.

The default profile deliberately uses separate optical masters. Windows uses
an un-enclosed, high-occupancy small-icon composition. Linux uses a transparent
GNOME-weighted composition plus scalable and symbolic SVGs. macOS uses a
purpose-built rounded-square composition with appropriate internal breathing
room. These are one brand, not interchangeable files.

## Color

Amber is the only brand accent. Near-black, warm charcoal, chalk, and neutral
slate support it. Red is reserved for error semantics; it is not part of the
identity. Slate focus and border colors may lean cool for usability, but blue
or cyan must not become a logo, highlight, progress, or primary-action color.

| Token | Value | Purpose |
| --- | --- | --- |
| Void | `#0A0A0D` | Primary application and website background; cave darkness. |
| Panel | `#12121A` | Raised application and website surfaces. |
| Raised panel | `#181822` | Website depth layer; use sparingly. |
| Icon amber | `#FFB000` | Application mark and branded loading fill. |
| Action amber | `#E5A11F` | Desktop primary actions and progress fill. |
| Light amber | `#F2D98C` | Titles, links, and warm high-emphasis text. |
| Brass | `#CAA23E` | Website secondary accent and restrained detail. |
| Dim brass | `#8A7030` | Low-emphasis website accent. |
| Chalk | `#CCCDD6` | Primary body copy on dark surfaces. |
| Muted chalk | `#9A9AA6` | Supporting copy and secondary information. |
| Slate line | `#5C5C6E` | Borders and structural separation. |
| Faint line | `#2A2A36` | Quiet website dividers and background detail. |
| Loading track | `#3B3428` | Branded loading-ring track paired with icon amber. |

Use the semantic token for the role instead of choosing the nearest amber by
eye. The application theme currently lives in
`src/caveviewer/gui/tk_theme.py`; website tokens live in `docs/index.html`;
profile-controlled loading colors live in
`src/caveviewer/resources/branding/default/branding.v1.json`. Where these
surfaces intentionally use different amber values, preserve the role
distinction. Do not average the palette into a new color.

All text and essential controls must retain appropriate contrast on their
actual background. Color cannot be the only indication of selection, focus,
progress, warning, or failure. Pair it with text, shape, weight, enabled state,
or motion as defined in [UX guidelines](ux-guidelines.md).

## Typography

Typography is part of the brand system but is not currently a branding-profile
field. Do not add fonts to `branding.v1.json` without a separately planned
schema and packaging change.

Desktop interfaces use a native-feeling sans serif selected by the platform
presentation profile: Segoe UI on Windows, Helvetica Neue on macOS, and the
resolved system sans-serif stack on Linux. Components use semantic roles from
`src/caveviewer/gui/tk_typography.py`, never one-off sizes. The canonical role
scale, accessibility scaling, and component mapping are documented in
[Design system](design-system.md).

The website uses three complementary families:

- Fraunces for expressive display headings;
- Source Sans 3 for readable interface and body copy; and
- IBM Plex Mono for technical data, compact labels, and code-like values.

Use sentence case for titles, labels, and actions unless a technical identifier
requires exact casing. Keep body copy direct and readable. Do not use decorative
display typography inside the desktop app merely to make a panel feel branded;
the mark, palette, and imagery already provide identity while the platform font
preserves usability.

## Imagery and composition

Use real cave, survey, or exploration imagery when available. Photography
should preserve deep shadow while keeping its focal subject readable; darken
busy imagery behind interface text instead of adding multiple competing
overlays. Survey contours, guideline references, and restrained rock texture
may support the composition when they remain subordinate to the content.

Do not fabricate cave-diving equipment details, use generic AI-looking divers,
or imply functionality that CaveViewer does not provide. Product screenshots
must begin from the current application UI and show real surfaces. Do not
invent navigation, data, or controls for a marketing composition.

Favor one focal mark, one primary message, and ample negative space. Avoid
repeating the logo in navigation, loading, and content at the same time. The
startup surface intentionally uses the dark cave photograph, a single sentence,
and a flat progress bar without an additional logo or product title.

## Motion, voice, and accessibility

Motion communicates progress or transition; it is not decoration. Use calm,
bounded animation, respect reduced-motion preferences, and never block the UI
thread to hold a branded frame. Progress uses the same dark-track/amber-fill
language across startup and map loading.

Brand voice is calm, capable, concise, and exploratory. Explain the user's
state or next action without dramatizing risk. Prefer language such as
`Preparing to explore what lies beneath...` over slogans, danger language, or
technical implementation detail.

Every branded asset must work on light and dark backgrounds where its consumer
allows both. Review exact-size raster output rather than judging only the
high-resolution master. Symbolic and high-contrast variants must remain
recognizable without color, and branded UI must follow the keyboard, scaling,
contrast, and non-color communication rules in [UX guidelines](ux-guidelines.md).

## Implementation overview

Branding inputs are semantic source artwork and presentation tokens. Derived
files such as ICO frames, an iconset, and hicolor PNGs are exports of those
inputs. Runtime and packaging consumers must request a semantic role instead
of selecting an unrelated concrete image path.

The implementation is divided into five layers:

1. `src/caveviewer/resources/branding/default/branding.v1.json` is the bundled
   default profile. It maps semantic roles to versioned PNG or SVG source
   assets, records provenance and hashes, and defines loading presentation
   tokens.
2. `src/caveviewer/branding.py` is the GUI-free schema validator and resolver.
   It validates paths, hashes, dimensions, alpha, safe area, SVG safety, roles,
   and loading colors, then returns immutable profile and runtime snapshots.
3. The application composition boundary resolves one snapshot and injects it
   into About, loading, window-icon, and platform consumers. Source builds may
   select a profile through `CAVEVIEWER_BRAND_PROFILE`; frozen builds ignore an
   external override.
4. `src/caveviewer/branding_export.py` deterministically derives runtime PNGs,
   a multi-frame Windows ICO, the macOS iconset inputs, Linux hicolor raster and
   vector assets, contact sheets, and a hash summary from the same profile.
5. `packaging/pyinstaller/CaveViewer.spec` and the Windows, macOS, and Linux
   build scripts consume that resolved export so runtime and native package
   surfaces do not drift.

The current version-1 profile controls artwork roles and loading-ring colors.
It does **not** control the Tk theme, typography, website CSS, startup
photography or copy, screenshots, store metadata, signing identity, product
name, or platform application IDs. Those remain separate sources listed in the
machine-readable contract. Adding any of them to the profile requires a
versioned schema change and matching runtime, exporter, packaging, and test
updates.

The version-1 branding profile controls these roles:

- application mark;
- Windows, macOS, and Linux application-icon overrides;
- Linux full-color scalable and High Contrast symbolic application icons;
- About-page mark;
- loading-indicator mark and progress mask;
- bounded loading-ring presentation tokens.

Optional DMG volume/background artwork, file-association icons, store imagery,
and web exports are extension points, not current profile roles.

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

## Machine-readable brand contract

Automation must extract exactly one JSON object between the start and end
markers below. The object uses repository-relative POSIX paths and six-digit
uppercase hexadecimal colors. Increment `contract_version` when a consumer
must change how it interprets the object. An additive clarification that does
not change machine behavior may retain the current version.

<!-- caveviewer-brand-contract:start -->
```json
{
  "contract_id": "caveviewer-brand-guidelines",
  "contract_version": 1,
  "document": "docs/development/branding.md",
  "brand": {
    "product_name": "CaveViewer",
    "audiences": [
      "cave divers",
      "cave-survey teams",
      "people exploring cave-survey data"
    ],
    "attributes": [
      "exploratory",
      "capable",
      "technical",
      "dark-and-luminous",
      "authentic"
    ],
    "voice": [
      "calm",
      "concise",
      "direct",
      "exploratory",
      "non-dramatic"
    ]
  },
  "authority": {
    "profile_manifest": "src/caveviewer/resources/branding/default/branding.v1.json",
    "profile_schema_and_resolver": "src/caveviewer/branding.py",
    "exporter": "src/caveviewer/branding_export.py",
    "desktop_theme": "src/caveviewer/gui/tk_theme.py",
    "desktop_typography": "src/caveviewer/gui/tk_typography.py",
    "platform_presentation": "src/caveviewer/gui/platform/presentation.py",
    "website_tokens": "docs/index.html",
    "ux_rules": "docs/development/ux-guidelines.md",
    "design_system": "docs/development/design-system.md",
    "pyinstaller_spec": "packaging/pyinstaller/CaveViewer.spec"
  },
  "identity": {
    "stable_values": {
      "product_name": "CaveViewer",
      "macos_bundle_id": "com.caveviewer.CaveViewer",
      "linux_application_id": "io.github.caveviewer.caveviewer",
      "linux_icon_basename": "io.github.caveviewer.caveviewer",
      "executable_name": "CaveViewer"
    },
    "must_not_change_in_brand_swap": [
      "product and executable names",
      "installer and artifact names",
      "application IDs and StartupWMClass",
      "signing and notarization identities",
      "release channels and update paths",
      "preference, cache, log, download, and application-data roots"
    ]
  },
  "iconography": {
    "concept": "Irregular cave profile enclosing darkness, with a circular light source left of center casting a widening beam to the right.",
    "signature_elements": [
      "irregular cave silhouette",
      "one strong outer rim",
      "near-black cave interior",
      "circular light source",
      "left-to-right widening light beam",
      "amber-to-yellow monochromatic treatment"
    ],
    "large_artwork_optional_details": [
      "amber survey contours",
      "restrained material texture",
      "subtle depth and highlights"
    ],
    "small_icon_rules": [
      "reduce to silhouette, rim, light source, and beam",
      "preserve negative space around the light source",
      "keep the beam clear of the cave rim",
      "use optical rather than geometric centering",
      "remove detail that aliases at exact output size",
      "do not rotate, mirror, stretch, or add a wordmark"
    ],
    "forbidden_motifs": [
      "skulls",
      "death or disaster imagery",
      "military styling",
      "inaccurate diving equipment",
      "copied or imitated equipment-manufacturer marks",
      "unrelated symbols inside the mark",
      "cyan or blue logo accents"
    ],
    "exact_preview_sizes_px": [
      16,
      24,
      32
    ],
    "platform_composition": {
      "windows": "transparent, un-enclosed, high-occupancy small-icon master",
      "macos": "dedicated rounded-square 1024-pixel master",
      "linux": "transparent GNOME-weighted master plus full-color scalable and monochrome symbolic SVGs"
    }
  },
  "colors": {
    "accent_policy": "Amber is the only brand accent. Blue and cyan are not brand colors. Red is semantic error feedback only.",
    "tokens": {
      "void": {
        "hex": "#0A0A0D",
        "uses": [
          "primary application background",
          "website background",
          "cave darkness"
        ]
      },
      "panel": {
        "hex": "#12121A",
        "uses": [
          "raised application surface",
          "website panel"
        ]
      },
      "panel_raised": {
        "hex": "#181822",
        "uses": [
          "website depth layer"
        ]
      },
      "icon_amber": {
        "hex": "#FFB000",
        "uses": [
          "application mark",
          "profile loading fill"
        ]
      },
      "action_amber": {
        "hex": "#E5A11F",
        "uses": [
          "desktop primary action",
          "desktop progress fill"
        ]
      },
      "light_amber": {
        "hex": "#F2D98C",
        "uses": [
          "title",
          "link",
          "warm high-emphasis text"
        ]
      },
      "brass": {
        "hex": "#CAA23E",
        "uses": [
          "website secondary accent"
        ]
      },
      "brass_dim": {
        "hex": "#8A7030",
        "uses": [
          "low-emphasis website accent"
        ]
      },
      "chalk": {
        "hex": "#CCCDD6",
        "uses": [
          "primary body text on dark surfaces"
        ]
      },
      "chalk_muted": {
        "hex": "#9A9AA6",
        "uses": [
          "supporting and secondary text"
        ]
      },
      "slate_line": {
        "hex": "#5C5C6E",
        "uses": [
          "border",
          "structural separation"
        ]
      },
      "line_faint": {
        "hex": "#2A2A36",
        "uses": [
          "quiet website divider",
          "background detail"
        ]
      },
      "loading_track": {
        "hex": "#3B3428",
        "uses": [
          "profile loading track"
        ]
      }
    },
    "rules": [
      "use semantic tokens rather than choosing an amber by eye",
      "do not average role-specific amber values into a new color",
      "do not communicate state through color alone",
      "verify contrast on the actual background"
    ]
  },
  "typography": {
    "profile_swappable": false,
    "desktop": {
      "windows_family": "Segoe UI",
      "macos_family": "Helvetica Neue",
      "linux_family": "resolved system sans-serif",
      "roles_source": "src/caveviewer/gui/tk_typography.py",
      "rules": [
        "use semantic roles",
        "apply accessibility scaling exactly once",
        "do not add one-off widget sizes",
        "use sentence case unless exact technical casing is required"
      ]
    },
    "website": {
      "display": "Fraunces",
      "body": "Source Sans 3",
      "technical": "IBM Plex Mono",
      "fallbacks_source": "docs/index.html"
    }
  },
  "imagery": {
    "preferred": [
      "authentic cave photography",
      "real CaveViewer screenshots",
      "survey contours",
      "restrained guideline references",
      "subtle rock texture"
    ],
    "rules": [
      "darken busy photography behind text",
      "keep imagery subordinate to content",
      "start UI concepts from a current application screenshot",
      "do not invent application functionality or navigation",
      "do not fabricate cave-diving equipment details"
    ]
  },
  "composition": {
    "rules": [
      "use one focal mark and one primary message",
      "preserve ample negative space",
      "avoid repeating the logo across adjacent surfaces",
      "prefer hierarchy and alignment over decorative containers",
      "startup uses no logo or product-title lockup"
    ]
  },
  "motion": {
    "purpose": [
      "progress",
      "state transition"
    ],
    "rules": [
      "keep animation calm and bounded",
      "respect reduced-motion preferences",
      "never block the UI thread for presentation timing",
      "use dark-track and amber-fill progress language"
    ]
  },
  "accessibility": {
    "rules": [
      "review light and dark backgrounds where supported",
      "review exact 16-, 24-, and 32-pixel output",
      "provide symbolic or high-contrast variants where required",
      "do not encode meaning through color alone",
      "preserve keyboard, focus, scaling, and contrast behavior"
    ]
  },
  "implementation": {
    "profile_schema_version": 1,
    "profile_selector_environment_variable": "CAVEVIEWER_BRAND_PROFILE",
    "external_runtime_profile_allowed_in_source_build": true,
    "external_runtime_profile_allowed_in_frozen_build": false,
    "required_roles": [
      "application_mark",
      "about_mark",
      "loading_mark",
      "loading_progress_mask",
      "windows_app_icon",
      "macos_app_icon",
      "linux_app_icon",
      "linux_scalable_icon",
      "linux_symbolic_icon"
    ],
    "profile_controlled": [
      "semantic artwork roles",
      "asset hashes and provenance",
      "minimum dimensions and alpha policy",
      "safe-area inset",
      "loading-ring mode, fill color, and track color"
    ],
    "not_profile_controlled": [
      "desktop Tk theme",
      "desktop typography",
      "website CSS and fonts",
      "startup photograph and copy",
      "screenshots and store metadata",
      "DMG background or volume artwork",
      "stable product identity"
    ],
    "loading_modes": [
      "text_only",
      "ring_only",
      "ring_with_mark"
    ],
    "windows_icon_sizes_px": [
      16,
      24,
      32,
      48,
      64,
      128,
      256
    ],
    "linux_raster_sizes_px": [
      48,
      64,
      128,
      256,
      512
    ],
    "macos_icon_sizes_px": [
      16,
      32,
      64,
      128,
      256,
      512,
      1024
    ]
  },
  "workflow": {
    "candidate_root": ".work/brands/<candidate>",
    "preview_root": ".work/branding-preview",
    "validate_command": [
      "caveviewer-branding",
      "--profile",
      ".work/brands/<candidate>",
      "validate"
    ],
    "export_command": [
      "caveviewer-branding",
      "--profile",
      ".work/brands/<candidate>",
      "export",
      "--output",
      ".work/branding-preview",
      "--replace"
    ],
    "review_outputs": [
      ".work/branding-preview/previews/contact-sheet.png",
      ".work/branding-preview/previews/macos-contact-sheet.png",
      ".work/branding-preview/previews/linux-contact-sheet.png",
      ".work/branding-preview/export-summary.v1.json"
    ],
    "sequence": [
      "copy the default profile to an ignored candidate directory",
      "edit semantic sources and manifest, never generated outputs",
      "record provenance and license",
      "update source SHA-256 values",
      "validate the profile",
      "export all derivatives together",
      "review exact-size contact sheets on light and dark surfaces",
      "run automated branding and packaging contract tests",
      "run native platform package smoke checks",
      "update web, store, and screenshot assets only after acceptance"
    ]
  },
  "native_review": {
    "windows": [
      "window upper-left icon",
      "taskbar",
      "executable",
      "installer",
      "Start menu and shortcuts",
      "Installed Apps and uninstaller"
    ],
    "macos": [
      "application windows",
      "Finder and Applications",
      "Dock and Command-Tab",
      "mounted DMG",
      "ARM64 and Intel packages"
    ],
    "linux": [
      "AppImage root icon and .DirIcon",
      "application grid",
      "Dock and task switcher grouping",
      "launcher and hicolor sizes",
      "scalable icon",
      "High Contrast symbolic icon",
      "Ubuntu and Fedora on GNOME Wayland and Xorg"
    ]
  }
}
```
<!-- caveviewer-brand-contract:end -->

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

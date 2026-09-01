---
name: caveviewer-branding
description: "Create, validate, preview, and integrate CaveViewer branding profiles and platform icon artifacts. Use for logo, icon, color, About-mark, loading-mark, startup-brand, or branding-profile changes; not for general desktop layout or interaction work."
---

# CaveViewer branding

Treat branding as replaceable artwork behind stable semantic roles and product
identity.

## Establish the contract

1. Read `docs/development/branding.md` completely, including its
   machine-readable contract. If prose and JSON disagree, stop and update both
   in the same change.
2. Read `docs/development/design-system.md` and
   `docs/development/ux-guidelines.md` only when the work also changes branded
   UI composition, motion, typography, feedback, or accessibility.
3. Separate a branding-profile change from a product-identity migration.
   Product names, application IDs, storage roots, signing identities, release
   channels, update paths, and artifact naming are not brand-profile fields.

## Work with a candidate

- Start experiments by copying the default profile to
  `.work/brands/<candidate>/`; do not overwrite accepted source artwork while a
  concept is still under review.
- Preserve semantic roles, source provenance, licensing, hashes, safe areas,
  alpha behavior, and platform-specific optical masters.
- Use existing profile and export tooling. Validate with
  `caveviewer-branding --profile .work/brands/<candidate> validate`, then export
  every platform derivative together with `caveviewer-branding --profile
  .work/brands/<candidate> export --output .work/branding-preview --replace`.
- Review the generated contact sheet at exact 16-, 24-, and 32-pixel sizes on
  light and dark backgrounds. For UI screenshot concepts, require a current
  CaveViewer screenshot as the baseline and do not invent functionality.
- Integrate an accepted candidate through the profile manifest and exporter;
  do not hand-edit derived ICO, ICNS, iconset, hicolor, contact-sheet, or
  packaging output.

## Preserve the visual language

- Keep the cave/light metaphor, amber-to-yellow monochromatic mark, near-black
  void, left-to-right beam, and adequate negative space.
- Simplify small icons before adding detail. Fine contours and texture belong
  only where the rendered size preserves them.
- Do not introduce cyan or blue into the production mark, communicate state by
  color alone, imitate equipment manufacturers, or use death, disaster,
  military, or inaccurate equipment imagery.
- Keep Windows, macOS, Linux full-color, and GNOME symbolic assets as separate
  optical compositions of one identity.

## Verify the change

Run the focused profile, export, packaging-contract, and GUI-composition tests:

```text
tests/unit/test_branding_profile.py
tests/unit/test_branding_export.py
tests/unit/test_branding_packaging_contract.py
tests/unit/gui/test_branding_composition.py
```

Also run the repository's standard validation. Perform the native icon-cache
and platform-surface checks in `docs/development/branding.md` for every affected
platform, and report platforms that were not tested directly.

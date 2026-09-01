---
name: caveviewer-screenshot-polish
description: "Clean and prepare CaveViewer screenshots with pixel-faithful crops, transparent window corners, redaction, sizing, and presentation checks. Use for screenshot post-processing or documentation imagery; not for changing application UX, inventing interface content, or generating brand artwork."
---

# CaveViewer screenshot polish

Prepare real CaveViewer captures for documentation, web, store, or review use
without turning them into invented interface mockups.

## Establish the source and destination

1. Read `docs/development/ux-guidelines.md`, especially its screenshot-baseline
   rule. Read `docs/development/branding.md` only when the request changes brand
   presentation, artwork, or brand color roles.
2. Inspect the supplied capture at original resolution. If no current baseline
   exists, ask for one before creating a visual concept.
3. Confirm the destination repository or output directory when placement
   matters, and read that destination's applicable instructions before writing
   there. Otherwise keep experiments under `.work/screenshots/`.
4. Preserve the source and write a descriptively named sibling output. Never
   overwrite the only original unless the user explicitly asks for replacement.

## Keep real screenshots truthful

- Prefer deterministic pixel operations for capture cleanup, cropping,
  transparency, padding, redaction, and resizing. Do not use generative editing
  to repair a real screenshot or reconstruct interface text and controls.
- Preserve application content, native window chrome, text, icons, colors, and
  layout unless the user explicitly requests a labeled concept image. Route an
  application-layout change through `$caveviewer-desktop-ux` and a brand-artwork
  change through `$caveviewer-branding`.
- Distinguish captured desktop pixels, operating-system shadows, and flattened
  corner backgrounds from intentional native border antialiasing. Remove only
  the confirmed exterior artifact.
- Prefer a transparent PNG for rounded window corners. Rebuild a corner mask
  only when its bounding box contains no meaningful UI pixels; stop rather than
  erase content when that precondition is false.
- Keep a full-resolution master. Resize only for a known destination, preserve
  aspect ratio, resample once, and inspect text and one-pixel strokes afterward.
- Use opaque redaction when the user requests removal of sensitive information;
  blur and pixelation can retain recoverable detail. Do not infer permission to
  redact or rewrite content merely to improve composition.

## Use the deterministic window-cleanup helper

Use `scripts/clean_window_capture.py` for exact edge trimming and antialiased
transparent-corner reconstruction. Measure every value from the current image;
do not reuse a trim or radius solely because it worked on an earlier laptop or
display scale.

Example for a Windows capture with a three-pixel exterior fringe and empty
22-pixel lower corner boxes:

```text
python .agents/skills/caveviewer-screenshot-polish/scripts/clean_window_capture.py \
  input.png output.png --trim 3 --corner-radius 22 --corners bottom \
  --edge-color "#0A0A0D"
```

Omit `--edge-color` to sample each selected corner from the adjacent window
edge. Use an explicit color when a flattened shadow or variable desktop
background makes automatic sampling ambiguous. The helper refuses in-place
edits and existing outputs unless `--replace` is explicit.

For other polish operations, use the smallest deterministic image operation
that can be inspected and repeated. If the user requests a creative composite
or conceptual alteration, label it as a concept and keep it separate from the
real screenshot.

## Verify and hand off

- Inspect the complete output and magnified edges on both light and dark
  backgrounds when transparency or antialiasing changed.
- Compare every unaffected pixel against the source at the documented crop
  offset. Report crop insets, masked corner boxes, output dimensions,
  transparency, any resize, and every intentional content change.
- For a batch, approve one representative result before applying the same
  transformation, and remeasure when captures differ in display scale, window
  state, or operating system.
- Report the saved path and retain the original until the user accepts the
  polished output.

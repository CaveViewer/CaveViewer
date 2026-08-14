# Cave Slice Export

## Goal

Let a diver save an interesting section of the currently open cave as a new,
independently loadable CaveViewer map.  The export must never modify the
parent cache.

## Interaction

There are no visible Slice buttons.

`Ctrl+C` (Windows or Linux) or `Cmd+C` (macOS) follows the same toggle lifecycle as video recording and Manual
Guided Dive tracing while the viewer has keyboard focus:

1. From idle, the first press performs a fresh slice preflight, hides an open
   color picker or manual-help surface, and arms the shared 3 → 2 → 1
   countdown.  It does not capture a start anchor yet.
2. The centered countdown uses the existing dimmed, import-style ring overlay,
with the title **Prepare to slice a cave** and the note **Press the same
shortcut again to cancel** (Ctrl+C on Windows/Linux; Cmd+C on macOS).
3. When the countdown elapses, capture the then-current camera position as the
   start anchor and enter active slicing.  Return to the normal viewer so the
   diver can proceed through the cave.
4. A second `Ctrl+C` or `Cmd+C` while active captures the current camera position as the
   end anchor, derives the slice volume, and starts background finalization.
5. A second `Ctrl+C` during the countdown cancels it and shows the shared
   transient **Slice canceled** status.  `Ctrl+C` while saving leaves the
   export running and reaffirms its saving status.

The shortcut is handled only by the viewer, not while a native dialog or a
text-entry control owns focus.  `Escape` may cancel a countdown, unfinished
selection, or user-initiated export, except after a window close has begun
finalizing the slice.

The Help overlay must document `Ctrl+C` as “start slice / finish and save
slice”.  Do not add a toolbar control, render-mode button, or other persistent
viewer control for slicing.

## Save destination and close behavior

Slices are app-managed maps.  Save every slice as a uniquely named child
directory of the map-storage location configured in Preferences (the
`storage.map_library_dir` / **Downloaded maps folder** setting), rather than
asking for an arbitrary destination.  Name each slice after its source cave,
for example `Ginnie Springs - Segment 1`, then `Ginnie Springs - Segment 2`.
Select one more than the highest matching segment already stored for that cave
(case-insensitively), so deleted segment numbers are not reused.  Preserve the
original cave name in additive slice metadata so a slice of a slice continues
the same sequence.  Names remain portable and collision-safe; never overwrite
an existing map.

Closing the viewer while a slice is in progress follows the existing
save-before-close pattern used for user captures:

1. If the pre-start countdown is active, cancel it and complete normal window
   close: no slice has begun yet.
2. If a start anchor exists, capture the current camera position at the first
   close request as the end anchor and immediately start export.
3. If planning or export is already active, continue that work; do not cancel
   it because the window is closing.
4. Defer the backend close, suppress further map input, retain the resources
   needed to present progress, and show a persistent **Finishing slice**
   status.
5. Close the viewer only after the exporter reaches a terminal state and any
   successfully produced slice has been published atomically.

The close path must not prompt for a destination or name.  A finalization
failure is reported before the viewer completes its normal shutdown; it must
never leave a partial map in the configured map-storage directory.

After every successful publication, reveal the exported **map directory** via
the platform saved-artifact reveal adapter, so the user sees Finder, Explorer,
or the platform equivalent with the new map.  Normal completion may retain the
shared saved-artifact confirmation delay before revealing it.  Close-time
slice finalization is an intentional exception to the current capture-close
behavior that discards pending reveals: reveal the saved slice before the
viewer is finally torn down.  A best-effort reveal failure does not make the
already-published slice fail or roll it back.

## Shared capture presentation

Use the existing recording/trace presentation components and wording rather
than creating a slice-specific dialog or notification style:

- While export finalizes, show the persistent centered information status
  **Saving slice…** with **Finishing the file. Keep CaveViewer open.**
- On success, show **Slice saved** with **Opening its location…** for the
  shared confirmation interval, then request the native directory reveal.
- On ordinary export failure, show **Could not save slice** with the useful
  failure detail using the shared error presentation.
- On user cancellation before publication, show **Slice canceled** using the
  shared transient cancellation presentation.
- During close-time finalization, use the capture-close presentation
  **Finishing slice** with **Saving the final slice. CaveViewer will close
  automatically.**

The slice controller should use the same testable countdown/status state
contract as recording and tracing.  Its state flow is:

```text
IDLE --Ctrl+C--> COUNTDOWN --elapsed--> ACTIVE --Ctrl+C--> SAVING
                  |                                  |
             Ctrl+C/Escape                         success/failure
                  v                                  v
              CANCELED                         SAVED / FAILED

ACTIVE or SAVING --window close--> EXIT_FINALIZING --published--> reveal + close
```

## Selection semantics

Version 1 exports an axis-aligned 3D box.  The two anchors form opposite
corners after applying a configurable padding value in source-coordinate
units.  The first anchor is stored as the initial landing position for the
new map.

This deliberately favors a deterministic, exactly-clippable selection over
copying complete cache chunks.  A later version may add route-and-radius
("corridor") slicing for winding passages.

## Output contract

Export a standalone precompiled-map directory, for example:

```text
Ginnie Springs - Segment 1/
  Ginnie Springs - Segment 1.cvslice
  manifest.json
  chunks/
  textures/...
```

The output uses the existing render-cache format version and therefore opens
through the existing precompiled-map path.  It contains only the selected
geometry and the texture assets referenced by that geometry.  It does not
copy parent bookmarks, navigation certificates, or Guided Dive recordings.

Additive slice metadata in the manifest records the slice schema version,
parent cache identity, original cave name, canonical bounds, entry position,
and exporter version.  Give the output a distinct Guided Dive identity derived
from the parent identity and canonical slice selection.  Use the `.cvslice`
marker as the output `source_obj` so it has a stable map name without
pretending that an OBJ source exists.

## Export implementation

Keep the implementation in the core layer, for example
`caveviewer.core.map.slicing`, with typed request, plan, result, and error
objects.  The GUI only supplies anchors, the configured map-storage parent,
progress, and cancellation.

The exporter must:

- validate the source manifest and find only chunks whose bounds overlap the
  requested box;
- stream material groups and triangle blocks rather than loading complete
  source chunks into memory;
- retain triangles wholly inside the box, reject outside triangles, and clip
  boundary triangles against all six planes while interpolating UVs and
  normals;
- preserve material names and source chunk IDs for the first implementation;
- copy each required cache-local texture through validated relative paths;
- write a private staging directory, then atomically publish the completed
  slice directory only after its manifest and assets validate;
- leave the source cache intact on success, cancellation, or failure.

Do not depend on the optional navigation certificate: ordinary maps can lack
one, and the viewer must not invoke the offline navigation planner at export
time.

Use a source-cache operation lock or revalidate its identity immediately
before publication so a concurrent cache rebuild cannot produce a mixed
slice.  Generate a new collision-safe child directory rather than overwriting
an existing map; a future explicit replacement flow must ask the user first.

## GUI process model

Run parsing, clipping, texture copying, and disk writes in a bounded worker
process, following the cache-rebuild/import controller patterns.  Keep
OpenGL and all UI updates on the main thread.  Reuse the shared countdown
overlay and saved-artifact presentation path used by recording and tracing.
Progress should distinguish planning, geometry export, asset copy,
publication, success, cancellation, failure, and close-time finalization.

On success, add the exported folder to recent maps, reveal its directory, and
do not automatically open it or move the diver away from the parent map.

## Required tests

- `Ctrl+C` starts the shared 3 → 2 → 1 countdown, captures the start anchor
  only after it ends, and hides the same transient viewer surfaces as capture;
- a second `Ctrl+C` cancels the countdown with **Slice canceled**, ends an
  active slice, and leaves a saving slice running;
- stopping shows persistent **Saving slice…**, then shared success, failure,
  cancellation, and delayed-reveal presentation states;
- output placement beneath the configured Preferences map-storage directory,
  cave-name segment numbering (including gaps, case variants, and long names),
  original-name retention when slicing a slice, and collision-safe naming;
- closing after the first anchor uses the camera position at close as the end
  anchor, defers teardown, and finishes publication; closing during an active
  export does not cancel it;
- normal and close-time successful exports request a native reveal of the map
  directory; reveal failure leaves the published map intact;
- exact box clipping, interpolated UVs/normals, empty output, and padding;
- bounded-memory chunk streaming;
- material/texture copying and rejection of missing or unsafe texture paths;
- independent opening after the parent cache is unavailable;
- distinct slice and Guided Dive identities;
- atomic publication, cancellation cleanup, source-cache rebuild races, and
  destination collision behavior.

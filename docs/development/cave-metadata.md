# Cave metadata

`src/caveviewer/resources/cave_metadata_catalog.v1.json` is an offline,
versioned catalog of descriptive facts about cave systems. It ships with the
application and is never fetched automatically, so a map remains usable when
the network is unavailable and catalog changes arrive with normal app updates.

The catalog describes a cave system, not the quality, completeness, or origin
of any particular 3D map. It must not change map opening, download, cache, or
file-management behavior.

## Catalog shape

The current schema is `1.0`:

```json
{
  "schema_version": "1.0",
  "catalog": {
    "name": "CaveViewer Famous Cave Systems",
    "caves": [
      {
        "id": "us-fl-devils-spring-system",
        "name": "Devil's Spring System",
        "aliases": ["Devils Eye"],
        "continent": "North America",
        "country": "United States",
        "region": "Florida",
        "type": "underwater_cave",
        "statistics": [
          {
            "label": "Surveyed underwater passage",
            "value": 8000,
            "unit": "ft",
            "qualifier": "greater_than"
          }
        ],
        "facts": ["One sourced cave-system fact."],
        "sources": [
          {
            "title": "Reference title",
            "url": "https://example.org/"
          }
        ]
      }
    ]
  }
}
```

Each cave id is stable and unique. Source URLs must be explicit `http` or
`https` links; CaveViewer opens them only after the user selects a source in
the **About Cave** view.

## Matching policy

The resolver is deliberately conservative:

1. A map source's optional `cave_metadata_id` is an exact lookup and wins.
2. Otherwise CaveViewer normalizes the map title and each catalog name/alias:
   accents, punctuation, case, safe CamelCase boundaries, and a short list of
   common cave-name plurals are normalized. Recognized technical suffixes such
   as `3D Map`, `Low Res`, and `Gold Line` are also considered without the
   suffix.
3. A unique normalized name or alias matches exactly.
4. Only then does it try a unique Levenshtein match: one edit for 5–7
   characters, two for 8–15, and three for 16 or more. Ties, short titles, and
   weak candidates do not match.

This policy favors showing no metadata over attributing the wrong cave. New
or renamed standard-library maps should add `cave_metadata_id` to their map
catalog entry once their association is known; name matching remains the
fallback for older and user-local maps.

## Map Library behavior

A confident match supplies the row's location and cave type. Operational
messages temporarily replace that line, while unavailable/former-source
warnings keep priority. The map archive's download size remains visible as
persistent metadata immediately before the row action; it does not replace the
cave detail or operational-status line. The overflow menu exposes **About
Cave**, which opens inside the existing splash content area and retains the Map
Library's scroll position when the user goes back. It shows the canonical cave
name, location, facts, supplied statistics, user-selected source links, and the
disclaimer that the cave system may not be the exact 3D map.

Recent-map slices keep their segment-specific display title but use the root
cave identity stored in their bounded `.cvslice` descriptor for metadata
matching. A confident parent match supplies the same location/type subtitle and
**About Cave** action as the original cave. Missing or malformed slice identity
does not trigger filename-based guessing. The first slice takes that root
identity from the visible original map-folder name, falling back to its source
model name only when no map root is available; slices of slices preserve the
root identity already stored by their parent.

`cave_metadata.py` owns catalog validation, formatting, and pure matching.
`cave_metadata_panel.py` only renders a match. `map_library_workflow.py`
passes a confident result to the splash composition root; it does not make a
metadata match an authorization or map-opening decision.

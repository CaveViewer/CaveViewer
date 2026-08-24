# Lognova CaveViewer static preview

This directory is an unpublished static conversion of the public CaveViewer
prototype at <https://lognova.com/caveviewer/>, captured on 2026-08-23 for
local design review.

It began as a faithful conversion of the prototype's rendered pages, CSS,
JavaScript, images, and navigation while replacing PHP/query-string routes
with ordinary `.html` files. The review version intentionally omits the
prototype's Features, Formats, Controls, Recording, and Preferences routes and
their page-specific assets. It contains no PHP runtime, server application,
persistent storage, or database.

The dedicated Contact Us page carries forward the current site's FormSubmit
submission contract: required name, email, and message fields, plus the
existing hidden `_honey` honeypot. It posts directly to FormSubmit, so do not
submit it during local design review unless a live message is intended.
CaveViewer stores no contact data and runs no contact backend or database.
The stronger server-verified human-validation work remains deferred until its
external configuration and publication boundary are authorized.

## Review locally

From the repository root:

```bash
python3 -m http.server 4173 --bind 127.0.0.1 --directory website-preview
```

Open <http://127.0.0.1:4173/>.

## Browser smoke checks

The browser checks reuse the review server above; they do not start another
server or publish any files. After starting the preview server, run:

```bash
cd tests/browser
npm ci
npx playwright install chromium
npm test
```

The suite covers the canonical pages at desktop, tablet, and mobile widths,
keyboard menu operation, a 200%-zoom-equivalent viewport, reduced motion, and
the no-JavaScript reveal baseline. To target another local host, set
`CAVEVIEWER_WEBSITE_URL` before running `npm test`.

## Image delivery budget

The preview keeps the original PNG and JPEG files as fallbacks, while modern
browsers select WebP candidates through `picture`/`srcset` or CSS `image-set`.
The markup supplies intrinsic image dimensions so each visual reserves space
before it loads. Initially visible visuals load eagerly; later feature and Team
portraits load lazily.

The following route budgets measure the highest-resolution WebP candidate for
each visual shown on that route. They intentionally exclude fallback files and
unused `srcset` alternatives, because a supporting browser downloads one
candidate rather than the complete asset catalog.

| Route | Before | Preferred modern candidates | Budget |
| --- | ---: | ---: | ---: |
| Home | 2.19 MB | 1.21 MB | 1.30 MB |
| Features | 9.10 MB | 0.33 MB | 0.40 MB |
| Team | 7.24 MB | 0.76 MB | 0.80 MB |

`tests/unit/test_website_preview.py` enforces these byte budgets and the
responsive markup contracts. Recheck the measurements whenever source imagery
or quality settings change.

## Deployment isolation

CaveViewer's Pages workflow uploads only `docs/`. This root-level
`website-preview/` directory is not part of the Pages artifact and cannot
replace `www.caveviewer.com` unless a later, explicitly approved change moves
selected files into the publishing tree.

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

## Deployment isolation

CaveViewer's Pages workflow uploads only `docs/`. This root-level
`website-preview/` directory is not part of the Pages artifact and cannot
replace `www.caveviewer.com` unless a later, explicitly approved change moves
selected files into the publishing tree.

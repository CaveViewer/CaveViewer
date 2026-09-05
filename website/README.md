# CaveViewer production website source

`website/` is the production source for CaveViewer's public, static website.
It has no server application, persistent storage, or database. The Pages
workflow builds an exported static artifact containing only the seven public
HTML routes, `assets/`, `storage/`, `CNAME`, and the retained
`/development/` documentation copied from `docs/development/`.

The artifact does not publish tests, scripts, Git metadata, or the archived
previous website under `docs/previous-site/`. `CNAME` remains
`www.caveviewer.com`. The site intentionally retains `noindex` while the
product uses the Stable download channel.

## Local build and review

Build the same bounded artifact that GitHub Pages uploads:

```bash
python3 scripts/build_site.py --output /tmp/caveviewer-site --replace
python3 -m http.server 4173 --bind 127.0.0.1 --directory /tmp/caveviewer-site
```

Run the static contracts from this directory:

```bash
python3 -m pytest -o addopts='' tests/unit/test_site.py -q
python3 scripts/sync_release.py --check
```

Run browser checks against the local artifact from a second terminal:

```bash
cd tests/browser
npm ci
npx playwright install chromium
npm test
```

## Release and contact maintenance

`assets/data/release.json` is the source of truth for the Stable download
chooser and Docs installation cards. After changing it, regenerate both marked
HTML blocks with `python3 scripts/sync_release.py`, then run `--check`; do not
edit generated blocks by hand.

The Contact page retains its approved FormSubmit action and honeypot. Its
default CAPTCHA remains enabled: do not add `_captcha=false` or automate a real
submission. CaveViewer has no contact backend or database.

## Image delivery budget

Modern browsers select preferred WebP images through `picture`/`srcset` or CSS
`image-set`, while PNG and JPEG originals remain fallbacks. The markup supplies
intrinsic dimensions to reserve layout space before images load.

| Route | Preferred modern candidates | Budget |
| --- | --- | ---: |
| Home | `ginnie1-faceted-survey.webp`, `software-hero-cave-strokes-full.webp` | 1.30 MB |
| Why CaveViewer | Rendering, Map Library, Capture, and Streaming WebP images | 0.45 MB |
| Documentation | Import, Streaming, Backup, and Troubleshooting WebP images | 0.13 MB |
| Team | Six responsive portrait WebP images | 0.80 MB |
| Sponsors | KISS Rebreathers and XDEEP logo WebP images | 0.05 MB |
| Projects | No local image assets; two privacy-enhanced YouTube embeds | — |

---
name: caveviewer-release
description: "Prepare, verify, and troubleshoot CaveViewer cross-platform releases and update metadata. Use for Windows, macOS, or Linux packaging, signing, notarization, update manifests, release branches, or GitHub release workflows; not for ordinary source runs."
---

# CaveViewer release

Treat publication as a gated, single-writer operation over an immutable source
revision. Planning, repair, build, and verification do not authorize publishing.

## Establish release state

1. Read `docs/development/releases.md` completely. Read
   `docs/development/source-setup.md` for local packaging configuration,
   `docs/development/licensing.md` for notices and corresponding source, and
   `docs/development/branding.md` when package artwork changes.
2. Inspect the full failed workflow, job, and step before changing scripts or
   YAML. Distinguish a product or workflow defect from runner or service
   instability.
3. Confirm source revision, branch, channel, numeric three-component version,
   publication state, existing tag/release state, and whether prior
   `release/next` metadata has returned to `main`.

## Respect authorization and ownership

- Do not publish, create or replace release assets, sign manifests, change
  GitHub releases, push release metadata, or dispatch a publishing workflow
  without explicit user authorization for that external mutation.
- Normal publication begins from protected `main`, promotes the exact revision
  to `release/next`, builds all four targets, and gives shared GitHub release
  and manifest writes to one finalizer. Do not create a parallel path around
  this sequence.
- Use `publish: false` package validation until the candidate is intentionally
  ready. Never weaken checks, broaden permissions, or use a new version merely
  to bypass a failed release.
- Never expose or commit private signing material. Preserve stable artifact
  names, bundle/application IDs, update paths, manifest signatures, hashes,
  sizes, channels, and package metadata.
- Stop after a partial publication until assets, metadata, tag source, and the
  authoritative prior manifest are reconciled according to the release guide.

## Use existing entrypoints

Prefer the shared **Create Preview Release** and **Create Stable Release**
PyCharm configurations for normal publication. Use the documented GitHub CLI
fallback or `scripts/release.sh` only for the workflow described in the release
guide; local publication is an exceptional recovery path.

For a fix, add the lowest reliable regression coverage: unit, integration,
workflow-contract, package-smoke, or native-platform validation. Relevant tests
commonly include `tests/unit/test_release_workflows.py`, release verifier tests,
and `tests/integration/test_release_finalizer.py`.

Run focused tests, the complete suite, package-only smoke coverage, and every
affected native target before publication. After publication, perform the full
post-release checklist and report any platform not tested directly.

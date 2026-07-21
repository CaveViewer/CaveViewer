# Documentation

This file defines how CaveViewer documentation is organized so people and AI
agents can find the applicable rules without reading duplicate policy blocks.

## File roles

- `AGENTS.md` files contain short, enforceable instructions for the files under
  their directory.
- `docs/development/*.md` files are the canonical human-readable development
  references for their subjects.
- `README.md` files are directory entry points and navigation aids.
- Tool-specific adapters, such as `.github/copilot-instructions.md`, should
  stay short and point to the canonical files.

## Policy placement

- Put universal rules in the repository root `AGENTS.md`.
- Put specialized rules in the nearest applicable `AGENTS.md`.
- Do not duplicate inherited policies. Link to the parent or canonical
  development document instead.
- Require explicit declarations for overrides.
- Keep architectural explanation in `docs/development/architecture.md`.
- Keep enforceable local policy in `AGENTS.md`.
- Give exact validation commands where practical.
- Prefer precise, verifiable requirements over broad aspirations.
- Keep each local policy file short enough to read before changing that area.
- Update policies in the same change that introduces a new architectural,
  release, testing, or compatibility constraint.

## Scoped policy header

Every scoped `AGENTS.md` should begin with this information:

```text
Applies to:
Inherits:
Overrides:
Validation:
```

Use `Overrides: none` when the local file only adds narrower rules. If a local
rule changes inherited behavior, identify the parent rule, the narrower scope,
the reason, and the validation command or test that protects the exception.

## Naming

- Use lowercase kebab-case for ordinary Markdown files.
- Keep established names under `docs/development/` unless a name is misleading.
- Keep only the project landing page as a repository-root `README.md`; place
  topical development documentation under `docs/development/`.
- Do not add root `README-*.md` files.
- Keep `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, and
  `THIRD_PARTY_NOTICES.md` unchanged because they are conventional entry
  points or externally meaningful files.
- Avoid suffix taxonomies such as `*-policy.md`, `*-guide.md`, or
  `*-process.md` unless the repository already uses that pattern.

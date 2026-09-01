Applies to: repository development work and `docs/development/`
Inherits: `../../AGENTS.md`
Overrides: none
Validation: focused tests for the affected area, the complete suite when practical, and `git diff --check`

# Development instructions and documentation

The root `AGENTS.md` routes all repository development work here. These rules
apply when changing source, tests, documentation, packaging, build and release
automation, or shared project configuration. A nearer scoped `AGENTS.md` may
add narrower instructions.

## Canonical documents

- [Work definition template](work-definition.md): required A3-style master
  table for planning and tracking all repository work before implementation.
- `work/`: tracked, reviewable execution plans committed with their
  implementation branches when a plan needs durable retention.
- [Architecture](architecture.md): component boundaries, data flow, and
  concurrency rules.
- [Repository layout](repository-layout.md): current paths, stable contracts,
  and the completed migration sequence.
- [Coding standards](coding-standards.md): implementation conventions and
  safety rules.
- [UX guidelines](ux-guidelines.md): interaction, layout, forms, dialogs,
  feedback, progress, accessibility, and platform-integration standards.
- [Testing](testing.md): test placement, commands, markers, and coverage policy.
- [Viewer benchmarking](benchmarking.md): automated FPS regression benchmark
  design, artifacts, workflow inputs, and calibration policy.
- [Releases](releases.md): canonical branch gates, `release/next` workflow,
  shared PyCharm launcher, channels, signing, and post-release verification.
- [Licensing](licensing.md): authoritative project license, corresponding
  release source, packaged notices, and third-party-license boundaries.
- [Rendering](rendering.md): import/chunking model, runtime streaming, tuning,
  and cache compilation options.
- [AI-assisted development](ai-assistance.md): canonical instructions,
  tool-specific adapters, and safe maintenance rules.
- [Repository skills](skills.md): checked-in skill inventory, routing
  boundaries, structure, authoring, and validation rules.
- [Documentation](documentation.md): documentation placement, inheritance,
  override, and naming rules.
- [Design system](design-system.md): shared Tk typography roles, scaling, and
  presentation rules.
- [Branding](branding.md): replaceable visual surfaces, derived platform
  artifacts, and the stable product-identity boundary.
- [Source setup](source-setup.md): source setup, runtime configuration, and
  detailed environment variables.
- [Runtime configuration](runtime-configuration.md): settings ownership,
  resolution, typed transport, diagnostics, and environment boundaries.
- [Cave metadata](cave-metadata.md): bundled catalog schema, conservative
  matching, and Map Library presentation behavior.

The focused documents in this directory are canonical for their subjects.
`source-setup.md` holds operational details that remain too detailed for the
root project README.

Read this index completely, then read the documents relevant to the task before
editing. Do not duplicate their detailed policy in `AGENTS.md` files.

## Work definition

Before editing repository files or changing repository-related external state,
copy [the work definition template](work-definition.md) to ignored root
`.work/<work-name>.md`. Complete and order its master table, then implement from
that table. Keep the document's current implementation, desired solution,
branch, and status fields synchronized through verification and merge. This
requirement applies to human contributors and automated agents.

Move or copy the plan to `docs/development/work/<work-name>.md` only when it
needs contributor sharing, pull-request review, or durable retention. Once a
tracked copy exists, it is authoritative and travels with the implementation.

The work document must identify the problem, current implementation, desired
solution, ordered task details, branch, and status before implementation begins.
Keep it current through verification and merge. Every implementation branch
starts from a fresh, clean `main` unless the work definition records why a
different base is required.

## Development working agreement

- Keep behavior changes, file moves, and formatting-only changes separate so
  each can be reviewed and reverted independently.
- Use project tooling and existing abstractions before adding parallel ones.
- Do not introduce a dependency, change a public cache or update format, alter
  a release path, or move an externally consumed file without documenting the
  compatibility impact and updating validation in the same change.
- Add or update tests for observable behavior and failure cleanup. Update code
  comments where they materially clarify the new behavior.

## Architecture and compatibility

- Follow dependency direction and component ownership in
  [Architecture](architecture.md).
- Follow stable path contracts in [Repository layout](repository-layout.md).
- Treat stable application IDs, package names, update paths, storage roots,
  public formats, and release metadata as compatibility boundaries.
- Keep core policy independent from GUI and platform side effects as defined by
  the architecture and coding standards.

## Shared run configurations

- Treat every file under `.run/` as shared, cross-machine configuration.
- Use `$PROJECT_DIR$`, repository-relative arguments, module SDK selection, and
  portable commands. Never store a contributor's username, home directory,
  absolute checkout path, interpreter path, credential, or token there.
- Keep personal run configurations and environment values in ignored IDE state.
- When changing `.run/`, parse the edited files as XML and audit the directory
  for absolute paths, user-specific paths, and secrets before handoff.

## Validation commands

Set up the development environment:

```bash
./scripts/dev/install.sh
```

Run a focused test while iterating:

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q tests/path/to/test_file.py
```

Run the complete suite:

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q
```

Check syntax without writing bytecode into the repository:

```bash
PYTHONPYCACHEPREFIX=/tmp/caveviewer-pycache \
  .venv/bin/python -m compileall -q src/caveviewer
```

Run focused tests first and the complete suite before handoff when practical.
Always run `git diff --check` and inspect the final diff for unrelated changes.
Use the platform-native verification described by the relevant canonical
document when automation cannot exercise the behavior.

## Definition of done

- The requested behavior or documentation is complete and proportionately
  tested.
- The active work document records final status, validation evidence, PR or
  merge references, and remaining external or platform action.
- Focused tests pass; complete-suite failures are investigated and reported,
  not silently omitted.
- Documentation changes accompany changes to commands, configuration,
  architecture, screenshots, compatibility boundaries, or release behavior.
- The final diff is checked for unrelated changes, generated output, secrets,
  and machine-local paths.
- Handoff reports what was verified and what remains.

## AI and agent instruction discovery

- Coding agents start with the tracked root [`AGENTS.md`](../../AGENTS.md), read
  this file for development work, and inherit the nearest scoped `AGENTS.md`
  for source, core, GUI, or tests.
- Repository-scoped workflows live under `.agents/skills/`. Codex selects them
  from their descriptions or an explicit `$skill-name`; the inventory and
  maintenance contract live in [Repository skills](skills.md).
- PyCharm AI Chat uses the tracked
  [JetBrains project rule](../../.aiassistant/rules/repository-instructions.md),
  which points back to the canonical `AGENTS.md` hierarchy without duplicating
  it. Follow the [PyCharm contributor setup](ai-assistance.md#pycharm-contributor-setup)
  to confirm that the tracked rule and shared workflow actions are available.
- Active plans live under ignored root `.work/` by default. Plans that need to
  be shared or retained move to `work/` and travel with their
  implementation branch.
- Shared, secret-free PyCharm workflow actions live under the tracked `.run/`
  directory. Personal run configurations and environment variables remain
  ignored.
- Provider selection, model choice, credentials, permissions, and
  `.idea/workspace.xml` are intentionally personal and are never the source of
  repository instructions.

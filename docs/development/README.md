# Development documentation

- [Work definition template](work-definition.md): required A3-style master
  table for planning and tracking all repository work before implementation.
- [`work/`](work/): tracked, reviewable execution plans committed with their
  implementation branches.
- [Architecture](architecture.md): component boundaries, data flow, and
  concurrency rules.
- [Repository layout](repository-layout.md): current paths, stable contracts,
  and the completed migration sequence.
- [Coding standards](coding-standards.md): implementation conventions and
  safety rules.
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

## Starting any work

Before editing repository files or changing repository-related external state,
copy [the work definition template](work-definition.md) to ignored root
`.work/<work-name>.md`. Complete and order its master table, then implement from
that table. Keep the document's current implementation, desired solution,
branch, and status fields synchronized through verification and merge. This
requirement applies to human contributors and automated agents.

Move or copy the plan to `docs/development/work/<work-name>.md` only when it
needs contributor sharing, pull-request review, or durable retention. Once a
tracked copy exists, it is authoritative and travels with the implementation.

## AI and agent instruction discovery

- Coding agents start with the tracked root [`AGENTS.md`](../../AGENTS.md) and
  inherit the nearest scoped `AGENTS.md` for source, core, GUI, or tests.
- PyCharm AI Chat uses the tracked
  [JetBrains project rule](../../.aiassistant/rules/repository-instructions.md),
  which points back to the canonical `AGENTS.md` hierarchy without duplicating
  it. Follow the [PyCharm contributor setup](ai-assistance.md#pycharm-contributor-setup)
  to confirm that the tracked rule and shared workflow actions are available.
- Active plans live under ignored root `.work/` by default. Plans that need to
  be shared or retained move to [`work/`](work/) and travel with their
  implementation branch.
- Shared, secret-free PyCharm workflow actions live under the tracked `.run/`
  directory. Personal run configurations and environment variables remain
  ignored.
- Provider selection, model choice, credentials, permissions, and
  `.idea/workspace.xml` are intentionally personal and are never the source of
  repository instructions.

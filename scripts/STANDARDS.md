# Script Standards

This file documents the conventions for shell scripts under `scripts/`.

## CLI Rules

- Public scripts must reject positional arguments.
- Public scripts must support `-h` and `--help`.
- Usage text should use the script basename, not a full repo path.
- Prefer explicit long options such as `--version=<version>` and `--target=<target>`.
- If `--option value` is supported, usage may still document `--option=<value>`.
- Unknown options must fail with `Error: unknown option '<option>'`.
- Missing option values must fail with `Error: --option requires a value.`

## Naming

- Use `x86_64` in user-facing text, artifact names, and examples.
- Use `linux-x86_64` as the public release target name.
- `linux-x86`, `linux-amd64`, `amd64`, and `x86` may exist only as quiet compatibility aliases or internal Docker/platform names.

## Help Text

- Keep usage blocks compact.
- Include examples only when they clarify a non-obvious workflow.
- Label internal helper scripts as internal.
- Point normal build, package, and release workflows to `release.sh`.

## Safety

- Scripts should use `#!/usr/bin/env bash`.
- Scripts should use `set -euo pipefail` unless they are meant to be sourced and have a reason not to.
- Optional array expansions under `set -u` should use guarded expansion, for example `${args[@]+"${args[@]}"}`.
- Destructive actions should be explicit and documented.
- Release publishing must require `CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`.

## Release Flow

- Prefer `release.sh` for normal build, package, and release work.
- Architecture wrappers should delegate to shared implementation scripts.
- Common scripts should not be documented as the first-choice entry point.

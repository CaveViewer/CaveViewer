# Explicit release-version bump support

This temporary work definition plans explicit release-version bump selection
without changing the existing updater channel model. The plan is intentionally
stored under ignored `docs/development/.agents/`; before implementation begins,
copy the approved plan into tracked `docs/development/work/` on its working
branch and keep that tracked copy current through merge.

## Master plan

Rows are ordered by implementation sequence. Preview and stable remain channel
metadata rather than version suffixes, and every published version remains a
dotted numeric identifier shared by all platform artifacts.

<style>
table th,
table td {
  vertical-align: top;
}
</style>

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Define the CaveViewer release-version policy. | Contributors lack a documented rule for choosing patch, minor, or major increments, so reaching `1.0.99` raises ambiguity about whether the next release is `1.0.100` or `1.1.0`. | Release automation selects the greatest dotted numeric stable or preview GitHub Release and increments only its final component. Preview and stable are separate metadata channels, but the meaning of each numeric component and the recovery behavior are not formally specified. | The canonical release documentation defines `MAJOR.MINOR.PATCH`: patch is the default unbounded release counter, minor is an explicit feature milestone, major is an explicit incompatible product/data/update generation, and channel is independent metadata. It explicitly states that `1.0.99` defaults to `1.0.100`, while `1.1.0` requires a minor selection. | Add the policy, examples, ordering rules, channel behavior, and operator guidance to `docs/development/releases.md`.<br>Document that normal automation selects a version greater than every non-draft stable or preview release.<br>Define exact-version use as a controlled resume/recovery path, not the normal way to create a new release.<br>Record compatibility implications for GitHub tags, manifests, package filenames, AppStream metadata, and updater comparisons. | `feature/explicit-version-bumps` | Implemented — PR open |
| 2 | Extend the version selector with explicit bump modes. | The selector can only increment the final component, so a maintainer cannot deliberately start a minor or major milestone through the shared release tooling. | `scripts/common/next_release_version.py` accepts dotted numeric candidates, selects the numerically greatest tuple, preserves its component count, and increments its last component. Its unit tests cover greatest-candidate selection, ignored invalid tags, leading zeros, and empty input. | A small, deterministic selector supports `patch`, `minor`, and `major` operations over normalized three-component product versions. Patch increments only PATCH without a rollover; minor increments MINOR and resets PATCH to zero; major increments MAJOR and resets MINOR and PATCH to zero. Existing valid CaveViewer versions remain orderable and migration behavior for two- or extra-component historical tags is explicitly tested. | Define normalization and rejection rules before changing code.<br>Add a typed bump-mode argument/API while retaining `patch` as the default.<br>Ensure examples produce `1.0.99 → 1.0.100`, `1.0.99 → 1.1.0`, and `1.0.99 → 2.0.0` for patch, minor, and major respectively.<br>Add focused tests for mixed stable/preview candidates, malformed tags, leading zeros, component-count edge cases, and numeric rather than lexical comparison.<br>Keep the implementation dependency-free and usable from both Python and shell launchers. | `feature/explicit-version-bumps` | Implemented — PR open |
| 3 | Add safe release-launcher controls. | The shared PyCharm release actions always choose the next patch automatically; selecting a milestone requires bypassing the launcher and manually entering workflow inputs. | `scripts/common/launch_github_workflow.py` automatically resolves required `version` from all non-draft GitHub Releases. Release actions ask only for preview or stable, default to preview, force publication, and dispatch from `release/next`. The older preview automation shell path independently invokes the same patch selector. | Release launchers support interactive and CLI `patch`, `minor`, and `major` choices while keeping patch as the default; exact recovery is CLI-only. The chosen mode and resulting version are displayed before dispatch. Exact mode requires an explicit version and is constrained to documented resume/recovery scenarios; it cannot silently replace automatic version selection. | Add CLI options such as `--bump {patch,minor,major}` and `--version VERSION`, with mutually exclusive validation and patch as the default.<br>For interactive PyCharm use, prompt for bump mode with patch as the default; keep exact recovery out of the normal menu.<br>Continue deriving patch/minor/major from the greatest non-draft stable or preview release regardless of the selected channel.<br>Validate dotted numeric syntax before dispatch, reject incompatible combinations, and preserve the `release/next` branch gate and forced publication fields.<br>Update the preview automation wrapper to call the shared policy or explicitly pass patch so the two paths cannot drift.<br>Add launcher tests for defaults, every mode, cancellation/invalid input, GitHub lookup failure, exact-version recovery, dispatch fields, and no-dispatch failure behavior. | `feature/explicit-version-bumps` | Implemented — PR open |
| 4 | Guard publication and metadata against invalid version choices. | A launcher-side check alone does not protect direct GitHub dispatches or future callers, and a mistaken lower or conflicting version could reach costly platform packaging before failing. | Release workflows require a dotted numeric `version` input, while publication/finalization scripts validate portions of release and metadata state. Direct GitHub dispatch permits maintainers to type the version manually. | Every release entry point rejects malformed versions early; normal new publication rejects versions that are not greater than the highest applicable published stable or preview version. A controlled rerun of an existing partial release remains possible only when its immutable source and channel match; the finalizer must still verify all resulting remote assets before metadata changes. | Audit reusable and individual-platform workflows plus finalization scripts for the earliest common validation point.<br>Add a shared validation step/script rather than duplicating shell logic across workflows.<br>Differentiate new publication from exact-version resume using verifiable GitHub Release channel and immutable tag-source evidence, not a permissive bypass flag; retain finalizer asset verification before metadata mutation.<br>Ensure package-only `publish: false` validation remains usable without mutating release state.<br>Add offline workflow-contract and validator tests covering higher versions, lower/equal rejection, valid partial-release resume, channel/source mismatch, malformed input, and failure before packaging or metadata mutation. | `feature/explicit-version-bumps` | Implemented — PR open |
| 5 | Update shared PyCharm actions and operator documentation. | Contributors need an obvious, consistent way to choose a milestone without editing run configurations or relying on GitHub workflow plug-ins. | Shared `GitHub - … Release` run configurations invoke the common launcher with `--release`; documentation says that the launcher selects the next version automatically but does not describe explicit bump selection. | Every shared all-platform and individual-platform release action exposes the same version-selection flow. Contributors can run it from PyCharm, understand the selected version before dispatch, and recover an interrupted release without accidentally skipping to a new number. | Keep shared `.run` files secret-free and generic; prefer common launcher behavior so separate run configurations are unnecessary unless PyCharm cannot present the prompt clearly.<br>Update `scripts/README.md`, `docs/development/releases.md`, and relevant PyCharm guidance with patch/minor/major/exact examples.<br>Document direct GitHub dispatch as an advanced manual path where the operator supplies and validates the exact version.<br>Add or update repository contract tests proving all shared Release actions use the common launcher and do not hard-code versions, channels, credentials, or bump modes. | `feature/explicit-version-bumps` | Implemented — PR open |
| 6 | Clean up any failed build or release workflow discovered during implementation. | Version-selection changes affect release dispatch and can expose a workflow failure that recurs if its failing path is repaired without regression coverage. | No failure is currently attached to this planned work. Existing release tests cover workflow structure, launcher behavior, publication, and finalization, but the exact gap will depend on any observed failing run. | Any affected workflow succeeds after the root cause is fixed, and the lowest reliable automated layer reproduces and prevents the failure. No security gate, branch restriction, signing check, or required test is weakened to obtain a passing run. | Inspect the complete failing run, workflow, job, and step before editing.<br>Distinguish repository defects from runner or GitHub service instability and reproduce locally when practical.<br>Implement the smallest root-cause fix and add focused unit, workflow-contract, packaging, or platform-smoke coverage.<br>Run focused tests, the complete suite, and the affected GitHub workflow; record the run URL and remaining manual validation in the tracked implementation copy.<br>If no workflow fails, record that outcome and the successful release-validation evidence rather than inventing cleanup work. | `feature/explicit-version-bumps` | In progress |

## Planned validation

- Focused tests for `next_release_version.py` and
  `launch_github_workflow.py`.
- Offline release workflow and finalization contract tests.
- Complete test suite:
  `.venv-dev/bin/python -m pytest -p no:cacheprovider -q`.
- `git diff --check` and final inspection for generated artifacts, credentials,
  unrelated edits, and hard-coded versions.
- GitHub Essential Tests followed by a non-publishing release validation from
  `release/next` before the first real publication using the new controls.

## Decisions required before implementation

- **Resolved:** milestone selection is available through both the interactive
  shared PyCharm launcher and `--bump`; patch is the default.
- **Resolved:** exact-version recovery is CLI-only through `--version` and does
  not appear in the normal interactive menu.
- **Resolved:** historical two-component versions normalize to a zero patch
  component for bump selection without rewriting existing tags. A greatest
  published version with more than three components stops automatic selection.

## Verification evidence

- Pull request [#305](https://github.com/CaveViewer/CaveViewer/pull/305) is open;
  required GitHub validation is pending.

- `bash -n scripts/common/validate_release_workflow.sh scripts/common/preview_release_automation.sh`
  — passed.
- Focused selector, launcher, workflow, guard, and instruction-contract tests —
  98 passed.
- `.venv-dev/bin/python -m pytest -p no:cacheprovider -q` — 1848 passed with
  one third-party pending-deprecation warning.
- `git diff --check` — passed.
- Ruff was not available in `.venv-dev`; no dependency was installed solely for
  this documentation and release-tooling change.
- GitHub Essential Tests and a non-publishing `release/next` validation remain
  pending until the branch is pushed and a pull request is opened.

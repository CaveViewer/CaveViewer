# AGPL-3.0-only license cleanup

This temporary work definition aligns CaveViewer's public declarations,
application UI, package metadata, notices, and validation with the existing
GNU Affero General Public License version 3 text in `LICENSE`. The selected
SPDX expression is `AGPL-3.0-only`; the project does not grant an automatic
option to use a later AGPL version.

The implementation branch must be created from the current `main` checkout
while preserving the already-staged deletions listed below. This is an explicit
exception to starting from a clean index: the user identified those removals as
part of this cleanup. No other pre-existing change may be absorbed.

## Master plan

Rows are ordered by implementation sequence. Third-party dependencies, fonts,
images, codecs, and other bundled works retain their own licenses; changing the
CaveViewer project license declaration must not overwrite or misrepresent
third-party terms.

<style>
table th,
table td {
  vertical-align: top;
}
</style>

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Establish the authoritative AGPL baseline. | CaveViewer gives recipients conflicting license information, creating uncertainty about whether ordinary GPLv3 or network-copyleft AGPLv3 governs the project. | `LICENSE` has contained the unmodified GNU Affero General Public License version 3 text since the initial commit, while `pyproject.toml`, README, Linux AppStream metadata, UI strings, developer documentation, Windows logging, and third-party notices identify GPLv3. The selected policy is now `AGPL-3.0-only`. | The work record identifies `LICENSE` and `AGPL-3.0-only` as authoritative, distinguishes the project license from third-party licenses, and records that this cleanup corrects inconsistent declarations rather than replacing the existing AGPL license text. Existing recipients' rights are not represented as revoked or retroactively altered. | Compare `LICENSE` with the official GNU AGPLv3 text without editing the license document itself.<br>Review repository history to record when the license and conflicting declarations were introduced.<br>Confirm whether any separately owned first-party files or assets carry incompatible grants; escalate uncertain copyright ownership instead of assuming relicensing authority.<br>Document `AGPL-3.0-only` rather than `AGPL-3.0-or-later` consistently. | `chore/agpl-license-cleanup` | In progress |
| 2 | Remove obsolete work records selected for cleanup. | Completed or disposable planning artifacts add noise and were intentionally removed before this plan was created. | The current `main` index stages deletion of `docs/development/.agents/refactor-code.md`, `docs/development/work/explicit-version-bump-support.md`, and `docs/development/work/work-definition-cleanup.md`. The first path is local-agent material; the latter two are tracked completed execution records. | The three user-selected removals are committed as part of this cleanup, no additional work records are removed, and canonical templates and active documentation remain intact. | Preserve the three staged deletions when creating the implementation branch.<br>Verify each target and its tracked/ignored status before commit.<br>Search canonical documentation for links to the removed records and repair only broken references.<br>Confirm `docs/development/work-definition.md`, `docs/development/README.md`, and active plans remain present. | `chore/agpl-license-cleanup` | In progress |
| 3 | Move temporary work into the root `.work/` directory. | Temporary plans currently live under `docs/development/.agents/`, which mixes disposable agent state into the canonical documentation tree and makes the temporary-work convention tool-specific. | `.gitignore`, root `AGENTS.md`, `docs/development/README.md`, `docs/development/work-definition.md`, `docs/development/ai-assistance.md`, repository-layout documentation, and instruction-contract tests identify `docs/development/.agents/` as the ignored temporary-notes location. This active plan is stored there. | Root `.work/` is the single ignored location for temporary work definitions and disposable investigation notes for people and agents. Tracked, reviewable execution records continue to live under `docs/development/work/`. No temporary file is accidentally committed, and canonical documentation consistently explains the distinction. | Add `/.work/` to `.gitignore` and remove the obsolete `docs/development/.agents/` ignore entry after migrating required local content.<br>Create root `.work/` and move this active temporary plan to `.work/license-cleanup.md`; migrate other still-needed local notes deliberately rather than copying obsolete files.<br>Update root/scoped agent guidance, the work-definition template, development README, AI-assistance guidance, repository-layout documentation, and any other canonical reference found by repository-wide search.<br>Update the JetBrains rule only if it embeds the old path rather than delegating to `AGENTS.md`.<br>Extend the instruction-discovery contract to require `.work/`, reject the obsolete temporary path in canonical instructions, and confirm tracked plans remain under `docs/development/work/`.<br>Verify `git check-ignore .work/license-cleanup.md` succeeds and no temporary files appear in `git status`. | `chore/agpl-license-cleanup` | In progress |
| 4 | Align first-party metadata, documentation, and UI. | Users, package indexes, and development tools currently receive GPLv3 declarations that contradict the repository's AGPLv3 license text. | Known conflicting locations include `pyproject.toml`, both README license statements, `THIRD_PARTY_NOTICES.md`, Linux AppStream `project_license`, `docs/development/source-setup.md`, splash-screen text, About-dialog text, Windows setup logging, and corresponding UI tests. | Every first-party declaration names the GNU Affero General Public License version 3 and uses `AGPL-3.0-only` where an SPDX expression is required. User-visible wording remains concise and consistent, while metadata licenses such as AppStream's `CC0-1.0` metadata license and all third-party license declarations remain unchanged. | Replace `GPL-3.0-only` with `AGPL-3.0-only` only for CaveViewer project-license fields.<br>Change prose and UI from “GNU General Public License”/“GNU GPL” to “GNU Affero General Public License”/“GNU AGPL” without altering unrelated uses of GPL in license or compatibility explanations.<br>Update exact-string presentation tests.<br>Search case-insensitively for stale first-party GPL declarations and classify every remaining occurrence before leaving it unchanged. | `chore/agpl-license-cleanup` | In progress |
| 5 | Preserve license and source availability in distributed artifacts. | A correct repository declaration is insufficient if installers or packaged applications omit the governing license, obscure corresponding-source access, or replace third-party notices. | macOS packaging explicitly copies `LICENSE`; repository-hosted releases include GitHub-generated source archives. Windows and Linux packaging behavior and frozen-bundle notice placement require verification. `THIRD_PARTY_NOTICES.md` documents dependencies and bundled assets separately. | Every supported Windows, Linux, and macOS distribution contains or clearly exposes the AGPLv3 license and third-party notices as required by its packaging contract. Release documentation identifies the corresponding source for the exact release. Network-service reuse is described accurately: operators modifying and offering CaveViewer over a network must comply with AGPLv3, without adding a project-specific restriction or legal interpretation to the license text. | Trace Windows installer, Linux AppImage, macOS DMG, and frozen-resource inputs for `LICENSE` and `THIRD_PARTY_NOTICES.md`.<br>Add the smallest missing package-copy or presentation behavior, if any, without duplicating the license text in generated code.<br>Ensure release source/tag URLs remain sufficient to locate the exact corresponding source and document any manual release check required.<br>Do not rewrite third-party notices or claim third-party components are AGPL-covered when they retain separate compatible licenses.<br>Add focused packaging-contract tests for any newly enforced artifact contents. | `chore/agpl-license-cleanup` | In progress |
| 6 | Add a repository license-consistency contract. | The current contradiction survived because no automated check compares the authoritative license text with project metadata, UI wording, packaging declarations, and documentation. | Existing tests assert the GPL splash/About wording but do not establish the repository-wide license identity. No focused test fails when the AGPL license file and SPDX declarations disagree. | An offline deterministic test verifies the AGPLv3 license header, `AGPL-3.0-only` project/AppStream declarations, approved UI wording, third-party-notice separation, and required package inclusion. It fails on the former GPL/AGPL mismatch without treating legitimate third-party GPL references as project declarations. | Add a narrow repository-contract test using explicit authoritative paths and expected values rather than an indiscriminate ban on the substring `GPL` (which is contained in `AGPL` and may appear in third-party terms).<br>Assert `LICENSE` begins with the official AGPLv3 title/version and does not begin with the ordinary GPLv3 title.<br>Assert package metadata and first-party documentation use the selected SPDX identifier and wording.<br>Assert packaging scripts retain required license/notice inputs.<br>Run the focused contract test before the complete suite. | `chore/agpl-license-cleanup` | In progress |
| 7 | Announce and verify the declaration correction. | Previously published repository snapshots and artifacts may have combined an AGPL license file with GPL metadata, so silent cleanup would leave users without context and could allow packaging regressions. | The current `main` branch has no `CHANGELOG.md`; the development documentation did not explain the mismatch. Local and CI validation cover application behavior and packages, but this cleanup has not yet been exercised across those gates. | Canonical `docs/development/licensing.md` clearly states that project declarations were corrected to match the existing AGPLv3-only license text. Focused and complete tests pass, relevant package-smoke workflows succeed, and the final diff contains only the intended license alignment and three approved document deletions. | Add a concise canonical licensing document without making legal conclusions about previously distributed copies, and link it from the development index.<br>Run focused UI, repository-license, metadata, and packaging-contract tests.<br>Run `.venv-dev/bin/python -m pytest -p no:cacheprovider -q`, Python syntax validation where applicable, and `git diff --check`.<br>Inspect staged and unstaged changes for accidental license-text edits, removed third-party notices, generated artifacts, or unrelated work.<br>Record the PR, CI runs, merge commit, and any residual legal/ownership review in the tracked implementation copy. | `chore/agpl-license-cleanup` | In progress |
| 8 | Clean up any failed build or release workflow. | License and packaging-contract changes may expose a build or package-smoke failure that could recur if repaired without regression coverage. | No failed workflow is currently attached to this work. Existing platform package-smoke workflows are expected to validate the affected distribution paths once a PR is opened. | Any observed failure is traced to its exact workflow and step, fixed at the root cause, and covered by the lowest reliable automated regression test. Linux, macOS, and Windows affected checks pass without weakening permissions, signing, branch gates, package checks, or source-availability requirements. | Inspect complete logs for any failed source or package job before editing.<br>Distinguish a repository defect from runner/service instability and reproduce locally when practical.<br>Add or strengthen unit, workflow-contract, packaging, or platform-smoke coverage for the former failure path.<br>Rerun focused and complete local tests plus the failed GitHub workflow, waiting at least one minute between status checks.<br>If no workflow fails, record the successful checks and close this conditional row without inventing cleanup work. | `chore/agpl-license-cleanup` | Pending |

## Planned validation

- Focused repository license-consistency and presentation tests.
- Focused Windows, Linux, and macOS packaging-contract tests for any affected
  notice-copy behavior.
- Complete suite:
  `.venv-dev/bin/python -m pytest -p no:cacheprovider -q`.
- Syntax validation for changed Python files with bytecode redirected outside
  the repository.
- `git diff --check`, staged/unstaged inspection, and a final search for stale
  first-party GPL declarations.
- Instruction-discovery contract validation plus `git check-ignore` evidence
  for `.work/license-cleanup.md` and absence of the obsolete temporary path in
  canonical contributor guidance.
- Required PR checks and all package-smoke workflows triggered by the affected
  packaging paths.

## External review boundary

This plan can make repository declarations technically consistent, but it does
not provide legal advice or infer ownership from Git author names. If review
finds substantive first-party material whose copyright owner did not authorize
AGPLv3-only distribution, implementation stops for explicit permission or
qualified legal review. Third-party licenses remain governed by their own
notices and compatibility terms.

## Verification evidence

- Official-license comparison confirmed that root `LICENSE` carries the GNU
  AGPLv3 title, version date, network-interaction section, and application
  guidance; the license text was not edited.
- `.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/test_license_contract.py tests/unit/test_agent_instruction_contract.py tests/unit/gui/test_presentation_profile.py tests/unit/test_linux_desktop_metadata.py tests/unit/test_windows_installer_contract.py tests/unit/test_macos_update_layout.py` — 44 passed.
- `.venv-dev/bin/python -m pytest -p no:cacheprovider -q` — 1853 passed with
  one third-party pending-deprecation warning.
- `git check-ignore -v .work/license-cleanup.md` — ignored by root `/.work/`.
- No package behavior change was required: existing PyInstaller, Linux,
  Windows, macOS, and source-package paths already preserve `LICENSE` and
  `THIRD_PARTY_NOTICES.md`; the new contract makes those guarantees explicit.
- Required PR and package-smoke validation remains pending until the branch is
  pushed and a pull request is opened.

# Work-definition and agent-discovery cleanup

This work updates the shared work-definition template and makes repository
instructions, active plans, JetBrains rules, and shared actions discoverable to
new contributors and agents without sharing personal IDE state.

## Master plan

<style>
table th,
table td {
  vertical-align: top;
}
</style>

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Top-align master-table content. | Long A3 cells can render with inconsistent vertical alignment, making rows harder to scan. | The shared template previously defined a Markdown table without explicit vertical alignment. | Every header and data cell in the rendered master table is top-aligned. | Added narrowly scoped `table th`/`table td` top-alignment styling and inspected the resulting Markdown structure. | `docs/work-definition-cleanup` | Complete |
| 2 | Add failed workflow regression cleanup. | A repaired build or release failure can recur when its failure path is not converted into automated coverage. | The template previously mentioned tests generally but had no standard task requiring inspection of the failed workflow and regression coverage after a fix. | Every build/release failure is investigated at the failing workflow and step; its fix includes the most focused practical regression test or an explicit documented reason when automation is impossible. | Added an ordered cleanup row covering complete log/workflow inspection, defect-versus-infrastructure classification, root-cause repair, regression coverage at the lowest reliable layer, focused/full verification, run evidence, and a prohibition against weakening gates or permissions. | `docs/work-definition-cleanup` | Complete |
| 3 | Track shareable work plans. | Active plans under ignored `.agents/` are unavailable to fresh checkouts and cannot be reviewed with their implementation. | Canonical instructions require work documents under `docs/development/.agents/`, while `.gitignore` excludes that directory. | Shareable plans live under tracked `docs/development/work/`; `.agents/` is explicitly disposable and non-authoritative. | Moved this active plan, updated canonical pointers, preserved the template, and documented the local-notes boundary. | `docs/work-definition-cleanup` | Complete |
| 4 | Share JetBrains AI rules safely. | The local PyCharm guideline path is stored in ignored personal workspace state, so contributors do not inherit it. | Root/scoped `AGENTS.md` files are tracked, but `.aiassistant/` is ignored and no shareable Chat-mode rule exists. | A tracked, minimal JetBrains rule directs AI Chat to the canonical `AGENTS.md` hierarchy without duplicating standards; personal provider/model state remains ignored. | Added `.aiassistant/rules/repository-instructions.md`, narrowed `.gitignore`, documented the contributor's one-time **Always** rule confirmation, and added the root startup checklist. | `docs/work-definition-cleanup` | Complete |
| 5 | Expose shared workflow actions to agents. | `.aiignore` hides all `.run/` files, including tracked GitHub actions useful to agents. | Git tracks `GitHub - *.run.xml`, but JetBrains agents that respect `.aiignore` cannot read them. | Shared GitHub actions remain visible while personal run configurations stay excluded. | Mirrored the `.gitignore` exception in `.aiignore` while retaining the credential exclusions. | `docs/work-definition-cleanup` | Complete |
| 6 | Document automatic instruction discovery. | Contributors cannot easily distinguish canonical agent instructions, JetBrains Chat rules, active work, local notes, and shared actions. | The information is distributed across several documents and local IDE state. | The development README provides one concise discovery map and manual PyCharm step. | Added the discovery map and aligned `ai-assistance.md` and `repository-layout.md` with the supported tracked paths. | `docs/work-definition-cleanup` | Complete |
| 7 | Guard instruction discovery with tests. | Ignore-rule or documentation cleanup can silently hide or break agent instructions. | No automated contract validates instruction inheritance, canonical links, JetBrains rules, visible shared actions, or required template columns. | A focused offline test fails when the discovery chain or work-definition contract breaks. | Added `tests/unit/test_agent_instruction_contract.py`; focused validation passed (5 tests), with complete-suite verification recorded below. | `docs/work-definition-cleanup` | Complete |

## Verification

- `.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/test_agent_instruction_contract.py` — 5 passed.
- `.venv-dev/bin/python -m pytest -p no:cacheprovider -q` — 1821 passed, 1
  third-party pending-deprecation warning.
- `git diff --check` — passed.

## Manual contributor action

In PyCharm, open **Settings → Tools → AI Assistant → Rules**, select
`.aiassistant/rules/repository-instructions.md`, and configure it as an
**Always** project rule. Provider, model, credential, and permission settings
remain personal.

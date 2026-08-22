# Work definition template

Use this template to define repository work before implementation begins. Copy
it to `docs/development/.agents/<work-name>.md`, replace every instruction and
placeholder, and keep its master table current throughout the work.

The work document is the execution record, not a speculative essay. Describe
the observed problem, current implementation, desired outcome, concrete work,
branch ownership, and status precisely enough that another contributor can
continue without reconstructing the investigation.

Every implementation branch must start from a fresh, clean `main` unless the
work definition records a different required base and explains why. Combine
related tasks on one branch; do not stack unrelated work on an unmerged branch.
Tasks that change GitHub or another external system without repository code use
`External settings — no branch`.

## Status values

- **Pending** — not started.
- **In progress** — implementation or external-settings work has begun.
- **Blocked** — cannot proceed until the stated dependency is resolved.
- **Implemented — PR open** — code is complete and awaiting merge.
- **Complete** — merged code or verified external configuration is active.

## Master plan

Rows must be ordered by implementation sequence. Use one row per independently
verifiable task. `<br>` separates independently verifiable details within a
table cell.

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | _Short outcome-oriented task name._ | _State the observed defect, risk, waste, or unmet need and its impact._ | _Describe the relevant behavior, code path, ownership, constraints, and evidence as they exist before this task._ | _State the measurable end condition. Include tests, safety properties, user-visible behavior, and boundaries that must hold._ | _List concrete implementation and verification actions in execution order.<br>Identify migrations, tests, documentation, cleanup, and external coordination.<br>Record dependencies on earlier rows._ | `type/descriptive-branch` | Pending |
| 2 | _Next task in implementation sequence._ | _Why this task is needed._ | _What exists now._ | _What must be true when complete._ | _Specific implementation and verification work._ | `type/descriptive-branch` | Pending |

## Work-document maintenance

- Add or split rows when investigation reveals a separately reviewable problem.
- Update **Current implementation** when the baseline changes before work starts.
- Change **Status** as work progresses; do not mark a task complete until its
  desired solution is active and verified.
- After a PR, record its number, merge commit, verification evidence, and any
  remaining manual action in the applicable cells rather than creating a
  disconnected status section.
- Keep problems and desired solutions stable unless evidence changes the work's
  scope. If scope changes materially, update the table before continuing.
- Preserve completed rows so the table remains an audit trail.

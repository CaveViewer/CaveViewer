# Work definition template

Use this template to define repository work before implementation begins. Copy
it to ignored root `.work/<work-name>.md` by default, replace every instruction
and placeholder, and keep its master table current throughout the work. Move or
copy it to `docs/development/work/<work-name>.md` only when the plan needs to be
shared, reviewed, or retained with the implementation.

The work document is the execution record, not a speculative essay. Describe
the observed problem, current implementation, desired outcome, concrete work,
branch ownership, and status precisely enough that another contributor can
continue without reconstructing the investigation.

Every implementation branch must start from a fresh, clean `main` unless the
work definition records a different required base and explains why. Combine
related tasks on one branch; do not stack unrelated work on an unmerged branch.
Tasks that change GitHub or another external system without repository code use
`External settings — no branch`.

Work documents remain ignored under root `.work/` unless a durable repository
record is useful. Shareable or long-lived work documents are committed with
their implementation under `docs/development/work/`; once promoted, that
tracked copy is authoritative.

## Status values

- **Pending** — not started.
- **In progress** — implementation or external-settings work has begun.
- **Blocked** — cannot proceed until the stated dependency is resolved.
- **Implemented — PR open** — code is complete and awaiting merge.
- **Complete** — merged code or verified external configuration is active.

## Master plan

Rows must be ordered by implementation sequence. Use one row per independently
verifiable task. `<br><br>` separates independently verifiable details with a
blank rendered line inside a table cell. Keep the style block below in every
work document so all master-table headers and contents remain vertically
aligned to the top.

In **Desired solution**, number every independently verifiable outcome and
separate outcomes with a blank rendered line using `<br><br>`. In **Task
details**, number every subtask in execution order and use the same separation.
For example:

```text
1. Decide the implementation approach.<br><br>2. Define bounded behavior and failure handling.<br><br>3. Add focused verification.
```

<style>
table th,
table td {
  vertical-align: top !important;
}
</style>

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | _Short outcome-oriented task name._ | _State the observed defect, risk, waste, or unmet need and its impact._ | _Describe the relevant behavior, code path, ownership, constraints, and evidence as they exist before this task._ | 1. _State the first measurable end condition._<br><br>2. _Identify required tests, safety properties, or user-visible behavior._<br><br>3. _State boundaries that must hold._ | 1. _List the first concrete implementation or verification action._<br><br>2. _Identify migrations, tests, documentation, cleanup, or external coordination._<br><br>3. _Record dependencies on earlier rows._ | `type/descriptive-branch` | Pending |
| 2 | _Next task in implementation sequence._ | _Why this task is needed._ | _What exists now._ | 1. _State the first required outcome._<br><br>2. _State the next required outcome._ | 1. _Describe the first implementation action._<br><br>2. _Describe the next verification action._ | `type/descriptive-branch` | Pending |
| 3 | Clean up a failed build or release workflow. | A build or release failure can recur when the failing workflow and step are repaired without converting the failure path into automated regression coverage. | _Identify the failed run, workflow, job, step, relevant implementation, existing tests, and evidence explaining why the current checks did not prevent or clearly diagnose the failure._ | 1. The root cause is fixed and the most focused practical automated test fails on the former defect and passes with the fix.<br><br>2. The affected workflow succeeds and related tests remain green.<br><br>3. The work record links the validating run.<br><br>4. If reliable automation is impossible, the row records concrete evidence and the smallest repeatable manual verification instead of silently omitting coverage. | 1. Inspect the complete failed workflow logs and the exact workflow/script/code path before changing behavior.<br><br>2. Reproduce or isolate the failure locally when practical and distinguish a product defect from runner/service instability.<br><br>3. Implement the smallest root-cause fix.<br><br>4. Add or strengthen unit, integration, workflow-contract, packaging, or platform-smoke coverage at the lowest reliable layer that prevents recurrence.<br><br>5. Run focused tests, the proportionate full suite, and the affected workflow; record commands, run links, residual platform validation, and cleanup artifacts.<br><br>6. Do not weaken security gates, skip required checks, or broaden permissions merely to make the workflow pass. | `fix/descriptive-workflow-failure` | Pending |

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
- Keep row 3 when build or release automation is in scope. Remove it only when
  the work cannot execute or affect those workflows, and record that scope
  decision in the copied work document.

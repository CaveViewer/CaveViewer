# Repository skills

CaveViewer stores reusable, repository-specific agent workflows under
`.agents/skills/`. These skills route an agent into the correct canonical
documents, project invariants, existing tooling, and validation for recurring
work. They supplement the `AGENTS.md` hierarchy; they do not replace it or
become a second source of product policy.

The format and discovery behavior follow the
[official OpenAI skill guidance](https://learn.chatgpt.com/docs/build-skills).
Codex scans `.agents/skills/` from the working directory toward the repository
root, initially reads each skill's name and description, and loads the complete
`SKILL.md` only when the user invokes the skill or its description matches the
task.

## Inventory

| Skill | Use it for | Do not route here for |
| --- | --- | --- |
| `$caveviewer-branding` | Branding profiles, icons, marks, brand colors, brand loading imagery, platform artwork, and brand export or integration. | General layout and interaction polish, packaging publication, or product-identity migrations. |
| `$caveviewer-desktop-ux` | Preferences, Help, dialogs, progress, loading layout, viewer overlays, typography, scaling, feedback, and accessibility. | Standalone brand artwork, import/cache ownership, or release packaging. |
| `$caveviewer-import-lifecycle` | Import processes, cache locks, staging, pause/resume, cancellation, viewer close during import, partial caches, and Map Library recovery. | FPS tuning, benchmark interpretation, ordinary layout, or packaging. |
| `$caveviewer-release` | Cross-platform packaging, release branches, workflows, signing, update metadata, publication, and release recovery. | Ordinary development runs or a functional map-import defect. |
| `$caveviewer-performance` | Viewer benchmarks, FPS regressions, scenarios, thresholds, streaming profiling, and result interpretation. | Functional import lifecycle failures or routine map opening. |
| `$caveviewer-work-cycle` | Starting, continuing, submitting, iterating, merging, and cleaning up planned repository work. | Release publication, one-off read-only analysis, or domain-specific implementation guidance. |

Skills remain available for automatic selection. A contributor may explicitly
invoke one by naming it with the `$` prefix. If a task crosses two genuine
domains, use the smallest applicable set and keep each domain's authority
separate. For example, a branded loading-layout change uses both branding and
desktop UX, while a release that merely packages an already accepted profile
uses release and reads the branding contract only where packaging requires it.
The work-cycle skill may orchestrate any implementation skill, but it does not
replace that skill's domain guidance.

## Ownership model

- Root and scoped `AGENTS.md` files define mandatory repository process and
  local rules. They are always applicable according to directory scope.
- `docs/development/*.md` files are the canonical, human-readable sources of
  truth for their subjects.
- A `SKILL.md` provides task routing, decision-changing project context, safety
  boundaries, and a workflow for one recurring job. Link to canonical documents
  rather than copying their full policy.
- Existing application CLIs, scripts, run configurations, and tests implement
  deterministic mechanics. Add a skill-local script only when repeated logic
  cannot be served cleanly by existing project tooling.

Do not place a general development rule only in a skill. An agent may not load
that skill, while applicable `AGENTS.md` instructions and indexed canonical
documents remain discoverable for every repository task.

## Skill structure

Each checked-in skill has this minimum form:

```text
.agents/skills/<skill-name>/
`-- SKILL.md
```

`SKILL.md` begins with YAML frontmatter containing `name` and `description`.
The directory name and frontmatter name must match, use lowercase kebab-case,
and begin with `caveviewer-` to avoid collisions with personal or system skills.
Descriptions must state both the intended trigger and the nearest likely
misrouting boundary.

Instruction-only skills are the default. Add `references/`, `scripts/`,
`assets/`, or `agents/openai.yaml` only when the workflow has a concrete need
for conditional reference material, repeatable automation, output assets, or UI
metadata. Do not create placeholders, per-skill README files, installation
guides, or duplicated quick references.

## Creating or changing a skill

1. Define the recurring request, intended trigger phrases, nearby requests that
   must not trigger it, authoritative documents, non-obvious invariants, and
   required verification in the work definition.
2. Add or revise the shortest `SKILL.md` that changes an agent's decisions in a
   useful way. Preserve user authorization boundaries, especially for releases
   and other external mutations.
3. Update the inventory above and the structural contract test when adding,
   renaming, or removing a skill.
4. Validate frontmatter and structure with the installed `$skill-creator`
   validator, then run the repository contract tests:

   ```bash
   .venv/bin/python -m pytest -p no:cacheprovider -q \
     tests/unit/test_skill_contract.py \
     tests/unit/test_agent_instruction_contract.py \
     tests/unit/test_repository_layout.py
   ```

5. Exercise realistic positive and negative prompts. Confirm that the intended
   skill is selected, adjacent skills are not selected, canonical documents are
   read before changes, and no skill treats diagnosis or verification as
   authorization for unrelated mutation.

Codex detects skill changes automatically in normal operation. If the skill
list in a client remains stale, restart that Codex client before diagnosing the
repository layout.

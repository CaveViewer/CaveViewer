"""Contract tests for shared agent instructions and work definitions."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_instruction_hierarchy_is_present_and_scoped() -> None:
    scoped_instruction_paths = (
        "src/AGENTS.md",
        "src/caveviewer/core/AGENTS.md",
        "src/caveviewer/gui/AGENTS.md",
        "tests/AGENTS.md",
    )

    assert (REPOSITORY_ROOT / "AGENTS.md").is_file()
    for relative_path in scoped_instruction_paths:
        assert (REPOSITORY_ROOT / relative_path).is_file()
        assert "Inherits:" in _read(relative_path)


def test_root_instructions_require_tracked_work_and_startup_checks() -> None:
    instructions = _read("AGENTS.md")

    assert "docs/development/work/<work-name>.md" in instructions
    assert "docs/development/.agents/<work-name>.md" not in instructions
    assert "## Session startup" in instructions
    assert "Inspect the active branch and Git status" in instructions
    assert "focused and complete validation commands" in instructions


def test_jetbrains_rule_delegates_to_canonical_instructions() -> None:
    rule = _read(".aiassistant/rules/repository-instructions.md")

    assert "Always follow the root `AGENTS.md`" in rule
    assert "docs/development/work/" in rule
    assert "**Always** project rule" in rule

    gitignore = _read(".gitignore")
    assert "!.aiassistant/rules/" in gitignore
    assert "!.aiassistant/rules/*.md" in gitignore


def test_shared_pycharm_workflows_remain_visible_to_jetbrains_agents() -> None:
    aiignore = _read(".aiignore")
    assert ".run/*" in aiignore
    assert "!.run/GitHub - *.run.xml" in aiignore

    shared_actions = sorted((REPOSITORY_ROOT / ".run").glob("GitHub - *.run.xml"))
    assert shared_actions
    for action in shared_actions:
        action_text = action.read_text(encoding="utf-8")
        assert "$PROJECT_DIR$" in action_text


def test_work_definition_and_discovery_docs_use_tracked_work_directory() -> None:
    template = _read("docs/development/work-definition.md")
    readme = _read("docs/development/README.md")
    required_columns = (
        "Problem",
        "Current implementation",
        "Desired solution",
        "Task details",
        "Branch",
        "Status",
    )

    assert "docs/development/work/<work-name>.md" in template
    assert "vertical-align: top" in template
    assert "failed build or release workflow" in template.lower()
    for column in required_columns:
        assert column in template

    assert "docs/development/work/<work-name>.md" in readme
    assert ".aiassistant/rules/repository-instructions.md" in readme

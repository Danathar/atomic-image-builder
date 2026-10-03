"""Join `docs/ai-ops-runbook.md` to the workflows and messages it tells you to act on.

Script: tests/test_ai_ops_runbook.py
What: Reads the runbook against `.github/workflows/`, the nightly compliance
      job's steps, the messages the agent audit and the snapshot drift script
      print, the `workflow_dispatch` inputs its commands pass, and the headings
      its links point at.
Why: A runbook is read when something is already wrong, which is the worst
     moment to find it describing a step that was renamed, a message that is
     no longer printed, or a `-f since=` input the workflow dropped. Nothing
     else reads this page: no coverage tier measures Markdown and ruff does
     not lint it, so a workflow change that leaves it stale stays green.
Goal: Adding a workflow without saying what to do when it fails, renaming a
      nightly step, rewording a quoted message, changing the audit's cap, or
      renaming a heading the runbook links to fails here, in the change that
      caused it.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_maintainer_workflow_table import job_step_names  # noqa: E402

RUNBOOK = ROOT / "docs/ai-ops-runbook.md"
WORKFLOW_DIR = ROOT / ".github/workflows"
NIGHTLY = WORKFLOW_DIR / "nightly-compliance.yml"
AGENT_AUDIT = WORKFLOW_DIR / "agent-audit.yml"
SNAPSHOT_DRIFT = ROOT / "snapshot_drift_issue.py"

# Steps of the nightly job that set the runner up rather than check anything.
# Listed so the step table cannot silently skip a check: every other step has
# to be a row. Asserted present, so a renamed setup step fails instead of
# quietly becoming a check the table is missing.
NIGHTLY_SETUP_STEPS = {"Checkout", "Set up Python", "Install test tooling", "Summarize"}

# Text the runbook quotes as what a run prints, and the file that prints it.
QUOTED_MESSAGES = {
    "with no `— hive:` signature line": AGENT_AUDIT,
    "no Signed-off-by trailer": AGENT_AUDIT,
    "Tier 4 paths": AGENT_AUDIT,
    "Could not sync the snapshot drift issue:": SNAPSHOT_DRIFT,
    "Bundled template snapshot trails upstream": SNAPSHOT_DRIFT,
}


def section(text: str, title: str) -> str:
    """The body of one `## <title>` section, up to the next `## ` heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"the runbook has no '## {title}' section")
    return match.group(1)


def first_column(body: str) -> list[str]:
    """The first cell of every body row of the first table in a section."""
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [row.split("|")[1].strip() for row in rows[2:]]


def anchor_slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to dashes."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s", "-", slug)


def anchors(path: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", path.read_text(), flags=re.S | re.M)
    return {anchor_slug(title) for title in re.findall(r"^#+ (.+)$", text, re.M)}


def dispatch_inputs(workflow: Path) -> set[str]:
    """The input names under a workflow's `workflow_dispatch: inputs:` block."""
    lines = workflow.read_text().splitlines()
    names: set[str] = set()
    inside = False
    for line in lines:
        if line.strip() == "inputs:" and line.startswith("    "):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith("      "):
                break
            match = re.match(r"^      ([\w-]+):\s*$", line)
            if match:
                names.add(match.group(1))
    return names


class RunbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RUNBOOK.read_text()

    def test_every_workflow_is_named(self) -> None:
        missing = sorted(
            path.name for path in WORKFLOW_DIR.glob("*.yml") if path.name not in self.text
        )
        self.assertEqual(
            missing,
            [],
            "a workflow the runbook never names; say what to do when it fails",
        )

    def test_every_named_workflow_exists(self) -> None:
        named = set(re.findall(r"\b([\w-]+\.yml)\b", self.text))
        on_disk = {path.name for path in WORKFLOW_DIR.glob("*.yml")}
        self.assertEqual(sorted(named - on_disk), [], "the runbook names a workflow that is not there")

    def test_nightly_step_table_is_the_jobs_checks(self) -> None:
        steps = job_step_names(NIGHTLY, "compliance")
        self.assertLessEqual(
            NIGHTLY_SETUP_STEPS,
            set(steps),
            "a setup step this test excludes is no longer in the nightly job",
        )
        checks = [step for step in steps if step not in NIGHTLY_SETUP_STEPS]
        rows = first_column(section(self.text, "Nightly compliance failed"))
        self.assertEqual(
            rows,
            checks,
            "the step table under 'Nightly compliance failed' does not list the "
            "nightly job's checks, in order, by their step names",
        )

    def test_quoted_messages_are_still_printed(self) -> None:
        flat = re.sub(r"\s+", " ", self.text)
        for message, source in QUOTED_MESSAGES.items():
            with self.subTest(message=message):
                self.assertIn(message, flat, "the runbook no longer quotes this; drop it here")
                self.assertIn(message, source.read_text(), f"{source.name} no longer prints this")

    def test_the_audit_cap_is_the_workflows(self) -> None:
        workflow = AGENT_AUDIT.read_text()
        self.assertIn("reached the $limit cap", workflow)
        limit = re.search(r"^\s*limit=(\d+)\s*$", workflow, re.M)
        self.assertIsNotNone(limit, "agent-audit.yml no longer sets limit=N")
        self.assertIn(f"`reached the {limit.group(1)} cap`", self.text)

    def test_dispatch_commands_pass_inputs_the_workflow_takes(self) -> None:
        commands = re.findall(r"gh workflow run ([\w-]+\.yml)((?: -f [\w-]+=\S+)*)", self.text)
        self.assertTrue(commands, "the runbook dispatches nothing, so this checked nothing")
        for workflow, flags in commands:
            with self.subTest(workflow=workflow):
                self.assertIn("workflow_dispatch:", (WORKFLOW_DIR / workflow).read_text())
                passed = set(re.findall(r"-f ([\w-]+)=", flags))
                self.assertLessEqual(passed, dispatch_inputs(WORKFLOW_DIR / workflow))

    def test_links_resolve(self) -> None:
        for target in re.findall(r"\]\(([^)\s]+)\)", self.text):
            if target.startswith("http"):
                continue
            with self.subTest(target=target):
                path_part, _, fragment = target.partition("#")
                path = (RUNBOOK.parent / path_part).resolve() if path_part else RUNBOOK
                self.assertTrue(path.exists(), f"{target} points at nothing")
                if fragment:
                    self.assertIn(fragment, anchors(path), f"{target} names no heading there")


if __name__ == "__main__":
    unittest.main()

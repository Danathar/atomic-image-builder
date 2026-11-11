"""Execute the `run:` body of .github/workflows/agent-signature.yml.

Script: tests/test_agent_signature_workflow.py
What: Holds the step's `def signature` line to agent-audit.yml's, runs the
      step's shell against pull request descriptions, and reads the trigger,
      token and job condition out of the workflow.
Why: The open-pull-request check exists so the monthly audit has nothing to
     fail. That holds only while both ask the same question: a copy that
     drifted would pass a description the audit later fails, or fail one it
     accepts. The trigger, the token and the way the description reaches the
     shell are what make a `pull_request` job that reads untrusted text safe,
     and each is a one-line edit away from not being.
Goal: A drifted signature line, a `pull_request_target` trigger, a write
      token, a description interpolated into the script, or a check that
      passes an unsigned description fails here.

The step's shell is extracted from the workflow rather than copied here, so
editing agent-signature.yml re-runs these assertions against the edit. `jq`
is the real one, since the test is written in it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_run_body

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-signature.yml"
AUDIT = ROOT / ".github/workflows/agent-audit.yml"
RULESET = ROOT / ".github/rulesets/main.json"
STEP = "Look for the signature line in the description"
AUDIT_STEP = "Audit merged agent pull requests"
JOB_NAME = "Check the agent signature line"

SIGNATURE = "— hive: agent=scanner backend=claude model=claude-opus-5-5"


def signature_definitions(body: str) -> list[str]:
    """Every `def signature:` line in a step's shell, stripped of its indent."""
    return [line.strip() for line in body.splitlines() if line.strip().startswith("def signature:")]


class SameTestAsTheAuditTests(unittest.TestCase):
    def test_the_definition_is_the_audits_word_for_word(self) -> None:
        mine = signature_definitions(step_run_body(WORKFLOW, STEP))
        audit = signature_definitions(step_run_body(AUDIT, AUDIT_STEP))
        self.assertEqual(len(mine), 1, "agent-signature.yml should define `signature` once")
        self.assertEqual(len(audit), 1, "agent-audit.yml should define `signature` once")
        self.assertEqual(mine, audit, "the two workflows no longer test the signature line the same way")

    def test_the_definition_still_looks_for_the_signature_line(self) -> None:
        # Guards the comparison above against both copies losing the test together.
        (line,) = signature_definitions(step_run_body(WORKFLOW, STEP))
        self.assertIn('"— hive:"', line)


class WorkflowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_it_runs_on_pull_request_only_including_an_edited_description(self) -> None:
        self.assertNotIn("pull_request_target", self.text)
        on = re.search(r"^on:\n((?:  .*\n)+)", self.text, re.M)
        self.assertIsNotNone(on, "agent-signature.yml has no `on:` block")
        self.assertEqual(
            on.group(1),
            "  pull_request:\n    types: [opened, edited, reopened, synchronize]\n",
        )

    def test_the_token_reads_contents_and_nothing_else(self) -> None:
        block = re.search(r"^permissions:\n((?:  .*\n)+)", self.text, re.M)
        self.assertIsNotNone(block, "agent-signature.yml declares no top-level permissions")
        self.assertEqual(block.group(1), "  contents: read\n")
        self.assertNotRegex(self.text, r"(?m)^ +permissions:")
        self.assertNotRegex(self.text, r"(?m)^ *-? *uses:")

    def test_it_checks_hive_app_and_ai_fix_pull_requests(self) -> None:
        condition = re.search(r"^    if: (.+)$", self.text, re.M)
        self.assertIsNotNone(condition, "the job has no `if:`, so it would check every pull request")
        self.assertEqual(
            condition.group(1),
            "github.event.pull_request.user.login == 'danathar-atomic-hive[bot]'"
            " || startsWith(github.event.pull_request.head.ref, 'ai-fix/')",
        )

    def test_the_description_reaches_the_script_only_through_the_environment(self) -> None:
        self.assertEqual(step_env(WORKFLOW, STEP), {"PR_BODY": "${{ github.event.pull_request.body }}"})
        self.assertNotIn("${{", step_run_body(WORKFLOW, STEP))

    def test_it_is_advisory_not_a_required_check(self) -> None:
        self.assertIn(f"    name: {JOB_NAME}\n", self.text)
        contexts = [
            check["context"]
            for rule in json.loads(RULESET.read_text(encoding="utf-8"))["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        ]
        self.assertNotIn(JOB_NAME, contexts)


@unittest.skipIf(shutil.which("jq") is None, "jq is not installed")
class SignatureStepTests(unittest.TestCase):
    """Runs the step's shell with each description as PR_BODY."""

    def run_step(self, description: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, STEP)],
            env={"PATH": "/usr/bin:/bin", "PR_BODY": description},
            capture_output=True,
            text=True,
        )

    def assertSigned(self, description: str) -> None:
        result = self.run_step(description)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Signed: {SIGNATURE}", result.stdout)

    def assertUnsigned(self, description: str) -> None:
        result = self.run_step(description)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::The description has no line starting", result.stdout)

    def test_a_signed_description_passes(self) -> None:
        self.assertSigned(f"## What changed\n\nThe thing.\n\n{SIGNATURE}\n")

    def test_a_description_written_in_the_browser_passes(self) -> None:
        # GitHub stores a description edited on the web with CRLF line endings.
        self.assertSigned(f"## What changed\r\n\r\n{SIGNATURE}\r\n")

    def test_a_signature_above_hidden_markers_passes(self) -> None:
        # The audit takes the first line that starts with the signature, wherever it is.
        self.assertSigned(f"Closes #1\n\n{SIGNATURE}\n\n<!-- hive:issue-link-fixed -->\n")

    def test_an_empty_description_fails(self) -> None:
        self.assertUnsigned("")

    def test_a_description_with_no_signature_fails(self) -> None:
        self.assertUnsigned("## What changed\n\nFiled by the scanner agent.\n")

    def test_a_signature_that_does_not_start_its_line_fails(self) -> None:
        self.assertUnsigned(f"The line looks like {SIGNATURE}\n")

    def test_shell_syntax_in_the_description_is_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "ran"
            self.assertSigned(f"$(touch {marker}) `touch {marker}` '\"\n\n{SIGNATURE}\n")
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

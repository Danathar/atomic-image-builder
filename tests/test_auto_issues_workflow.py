"""Execute the `run:` body of .github/workflows/auto-issues.yml.

The workflow turns a red scheduled run into an issue and closes it when the
workflow passes again. What the shell decides, and what goes wrong quietly
when it stops deciding it:

* Which runs count. Only a scheduled or dispatched run on the default branch
  says anything about `main`; a cancelled run is neither a pass nor a fail.
  Closing on one of those hides a failure nobody fixed.
* Which open issue is the tracking issue. Matched by exact title *and* by the
  github-actions author, because anyone can open an issue with that title.
  Matching the title alone lets a squatted issue absorb every failure.
* That a failed listing fails the step. Read as "no issue open", it would file
  a duplicate on every red run.
* That `workflows:` names the scheduled workflows. `workflow_run` matches on
  each workflow's `name:`, so a rename silently stops tracking it.

The step's shell is extracted from the workflow rather than copied here, so
editing auto-issues.yml re-runs these assertions against the edit. `gh` is a
stub serving staged JSON and recording its argv and stdin; `jq` is the real
one, since the listing is filtered by the `--jq` expression the step sends.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_run_body

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
WORKFLOW = WORKFLOWS / "auto-issues.yml"
STEP = "Open, update or close the tracking issue"
REPO = "Danathar/atomic-image-builder"
RUN_URL = "https://github.com/Danathar/atomic-image-builder/actions/runs/1"
SHA = "a" * 40

# `issue list` applies the step's own --jq to issues.json, as gh does, and
# prints the result raw. Every call's argv is logged tab-separated; a body
# sent on stdin (`--body-file -`) is appended to stdin.log.
GH_STUB = r"""#!/usr/bin/env bash
{ printf '%s\t' "$@"; printf '\n'; } >> "$STUB_DIR/gh.log"
case "$1 $2" in
  "issue list")
    [ "${STUB_LIST_EXIT:-0}" = 0 ] || { echo "gh stub: listing failed" >&2; exit "$STUB_LIST_EXIT"; }
    filter=""
    while [ $# -gt 0 ]; do
      [ "$1" = "--jq" ] && filter="$2"
      shift
    done
    jq -r "$filter" "$STUB_DIR/issues.json"
    ;;
  "issue create"|"issue comment")
    cat >> "$STUB_DIR/stdin.log"
    ;;
  "issue close") ;;
  *) echo "gh stub: unexpected command: $*" >&2; exit 97 ;;
esac
"""


def issue(number: int, title: str, author: str = "app/github-actions") -> dict:
    return {"number": number, "title": title, "author": {"login": author}}


def scheduled_workflow_names() -> list[str]:
    """The `name:` of every workflow whose `on:` declares a schedule."""
    names = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        if not re.search(r"^  schedule:", text, re.MULTILINE):
            continue
        name = re.search(r"^name: (.+)$", text, re.MULTILINE)
        assert name, f"{path.name} has no top-level name:"
        names.append(name.group(1).strip())
    return sorted(names)


class StepRun:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def run(
        self,
        *,
        issues: list[dict],
        workflow: str = "Nightly compliance",
        conclusion: str = "failure",
        event: str = "schedule",
        branch: str = "main",
        list_exit: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        (self.tmp / "issues.json").write_text(json.dumps(issues))
        env = {
            **os.environ,
            "PATH": self.path,
            "STUB_DIR": str(self.tmp),
            "STUB_LIST_EXIT": str(list_exit),
            "GH_TOKEN": "stub-token",
            "REPO": REPO,
            "DEFAULT_BRANCH": "main",
            "WORKFLOW": workflow,
            "EVENT": event,
            "HEAD_BRANCH": branch,
            "HEAD_SHA": SHA,
            "CONCLUSION": conclusion,
            "RUN_URL": RUN_URL,
        }
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, STEP)],
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
        )

    def calls(self) -> list[list[str]]:
        log = self.tmp / "gh.log"
        if not log.exists():
            return []
        return [line.split("\t")[:-1] for line in log.read_text().splitlines()]

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] != ["issue", "list"]]

    def stdin(self) -> str:
        path = self.tmp / "stdin.log"
        return path.read_text() if path.exists() else ""


class AutoIssuesWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        # jq is on every GitHub runner; skipped the way
        # tests/test_agent_audit_workflow.py skips without it.
        if shutil.which("jq") is None:
            self.skipTest("jq is not installed")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.step = StepRun(Path(self.tmpdir.name))

    def test_the_harness_supplies_exactly_the_variables_the_step_declares(self) -> None:
        env = step_env(WORKFLOW, STEP)
        self.assertEqual(
            sorted(env),
            ["CONCLUSION", "DEFAULT_BRANCH", "EVENT", "GH_TOKEN", "HEAD_BRANCH", "HEAD_SHA", "REPO", "RUN_URL", "WORKFLOW"],
        )
        self.assertEqual(env["DEFAULT_BRANCH"], "${{ github.event.repository.default_branch }}")
        for key, field in (
            ("WORKFLOW", "name"),
            ("EVENT", "event"),
            ("HEAD_BRANCH", "head_branch"),
            ("HEAD_SHA", "head_sha"),
            ("CONCLUSION", "conclusion"),
            ("RUN_URL", "html_url"),
        ):
            self.assertEqual(env[key], f"${{{{ github.event.workflow_run.{field} }}}}")

    def test_the_watched_workflows_are_exactly_the_scheduled_ones(self) -> None:
        # workflow_run matches on `name:`. A renamed workflow, or a new
        # scheduled one, would otherwise go untracked with nothing failing.
        listed = re.search(r"^\s*workflows: \[(?P<names>[^\]]+)\]", WORKFLOW.read_text(), re.MULTILINE)
        self.assertIsNotNone(listed, "auto-issues.yml has no inline workflows: list")
        names = sorted(name.strip() for name in listed.group("names").split(","))
        self.assertEqual(names, scheduled_workflow_names())

    def test_a_first_failure_opens_one_issue_naming_the_run(self) -> None:
        result = self.step.run(issues=[issue(5, "Scheduled run failing: Maintenance Audit")])
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = self.step.writes()
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][:2], ["issue", "create"])
        self.assertIn("Scheduled run failing: Nightly compliance", writes[0])
        body = self.step.stdin()
        self.assertIn(RUN_URL, body)
        self.assertIn(SHA, body)
        self.assertIn("`failure`", body)

    def test_a_further_failure_comments_on_the_open_issue_instead_of_filing_another(self) -> None:
        result = self.step.run(
            issues=[issue(7, "Scheduled run failing: Nightly compliance")],
            conclusion="timed_out",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = self.step.writes()
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][:3], ["issue", "comment", "7"])
        self.assertIn(RUN_URL, self.step.stdin())
        self.assertIn("`timed_out`", self.step.stdin())

    def test_an_issue_with_the_title_but_another_author_is_not_the_tracking_issue(self) -> None:
        # Anyone can open an issue with the title. Commenting on theirs would
        # leave the real signal in an issue someone else controls.
        result = self.step.run(issues=[issue(9, "Scheduled run failing: Nightly compliance", author="someone")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[:2] for call in self.step.writes()], [["issue", "create"]])

    def test_a_pass_closes_the_open_issue(self) -> None:
        result = self.step.run(
            issues=[issue(11, "Scheduled run failing: Nightly compliance", author="github-actions[bot]")],
            conclusion="success",
            event="workflow_dispatch",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = self.step.writes()
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][:3], ["issue", "close", "11"])
        self.assertIn(RUN_URL, " ".join(writes[0]))

    def test_a_pass_with_no_issue_open_writes_nothing(self) -> None:
        result = self.step.run(issues=[], conclusion="success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.step.writes(), [])

    def test_runs_that_say_nothing_about_main_neither_open_nor_close(self) -> None:
        open_issue = [issue(13, "Scheduled run failing: Nightly compliance")]
        cases = {
            "a dispatch on another branch": {"branch": "feature", "event": "workflow_dispatch"},
            "a push": {"event": "push"},
            "a cancelled run": {"conclusion": "cancelled"},
            "a skipped run": {"conclusion": "skipped"},
        }
        for label, overrides in cases.items():
            for conclusion in ("failure", "success"):
                kwargs = {"conclusion": conclusion, **overrides}
                with self.subTest(case=label, conclusion=kwargs["conclusion"]):
                    (Path(self.tmpdir.name) / "gh.log").unlink(missing_ok=True)
                    result = self.step.run(issues=open_issue, **kwargs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.step.writes(), [])

    def test_a_failed_listing_fails_the_step_and_files_nothing(self) -> None:
        result = self.step.run(issues=[], list_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.step.writes(), [])


if __name__ == "__main__":
    unittest.main()

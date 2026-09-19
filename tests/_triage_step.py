"""Run the `Label the issue` step of .github/workflows/triage.yml offline.

The step is extracted from the workflow rather than copied, so editing
triage.yml re-runs it here. ``gh`` is a recording stub on PATH: the step never
reaches GitHub, and a caller asserts on the argv the step would have sent.

Two suites need this. ``tests/test_triage_workflow.py`` drives it with bodies
written by hand, one per labelling rule. ``tests/test_issue_templates.py``
drives it with a body rendered out of `.github/ISSUE_TEMPLATE/bug_report.yml`,
which is what keeps the `grep -vF` needle in the workflow and the checkbox
wording in the form from drifting apart -- a hand-written body cannot see that.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts this
directory on sys.path.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from _workflow_steps import step_run_body

ROOT = Path(__file__).resolve().parents[1]
TRIAGE_WORKFLOW = ROOT / ".github/workflows/triage.yml"
LABEL_STEP = "Label the issue"

# `gh issue view --json title` and `--json body` are the step's only reads;
# everything else it runs is a write worth asserting on. Each invocation is
# appended to a log as tab-separated argv so a case can assert on exact
# arguments rather than on a flattened command line.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
case " $* " in
  *" issue view "*" --json title "*) cat "$STUB_TITLE_FILE"; exit 0 ;;
  *" issue view "*" --json body "*) cat "$STUB_BODY_FILE"; exit 0 ;;
  *" label create "*) exit "${STUB_LABEL_CREATE_EXIT:-0}" ;;
  *" issue edit "*) exit "${STUB_ISSUE_EDIT_EXIT:-0}" ;;
esac
exit 0
"""


@dataclass
class StepRun:
    returncode: int
    stdout: str
    stderr: str
    calls: list[list[str]] = field(default_factory=list)

    def label_creates(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["label", "create"]]

    def issue_edits(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["issue", "edit"]]

    def applied_labels(self) -> list[str]:
        applied = []
        for call in self.issue_edits():
            for i, arg in enumerate(call):
                if arg == "--add-label":
                    applied.append(call[i + 1])
        return applied


def run_label_step(
    *,
    title: str,
    body: str,
    number: str = "42",
    repo: str = "Danathar/atomic-image-builder",
    label_create_exit: int = 0,
    issue_edit_exit: int = 0,
) -> StepRun:
    """Execute the step's shell against ``title``/``body`` and record its calls."""
    script = step_run_body(TRIAGE_WORKFLOW, LABEL_STEP)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        gh_stub = fake_bin / "gh"
        gh_stub.write_text(GH_STUB)
        gh_stub.chmod(0o755)

        title_file = tmp_path / "title"
        title_file.write_text(title)
        body_file = tmp_path / "body"
        body_file.write_text(body)
        log = tmp_path / "gh.log"
        log.touch()

        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        # The three variables triage.yml's `env:` block supplies. The test in
        # test_triage_workflow.py pins that mapping so a rename in the
        # workflow cannot leave this harness silently feeding the old names.
        env["GH_TOKEN"] = "stub-token"
        env["REPO"] = repo
        env["NUMBER"] = number
        env["STUB_LOG"] = str(log)
        env["STUB_TITLE_FILE"] = str(title_file)
        env["STUB_BODY_FILE"] = str(body_file)
        env["STUB_LABEL_CREATE_EXIT"] = str(label_create_exit)
        env["STUB_ISSUE_EDIT_EXIT"] = str(issue_edit_exit)

        proc = subprocess.run(
            ["bash", "-c", script],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        calls = [line.split("\t")[:-1] for line in log.read_text().splitlines() if line]
    return StepRun(proc.returncode, proc.stdout, proc.stderr, calls)

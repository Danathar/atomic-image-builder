"""Execute the `run:` body of .github/workflows/triage.yml.

Nothing in the suite ran a workflow step. `tests/test_workflow_dependencies.py`
reads every workflow, but only to compare pins and install commands across
them -- the shell inside `run:` is never executed, so the issue-labelling rules
in triage.yml were checked by review alone. Two of those rules are there
because they were wrong the first time (the bug form's own boilerplate matched
every report, and `case` matched nothing capitalised); a silent regression to
either state would flood or empty the `security` label with no test failing.

The step's shell is extracted from the workflow rather than copied here, so
editing triage.yml re-runs these assertions against the edit. `gh` is a
recording stub on PATH: the step never reaches GitHub, and each case asserts
on the argv the step would have sent.
"""

import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRIAGE_WORKFLOW = ROOT / ".github/workflows/triage.yml"
LABEL_STEP = "Label the issue"


def _step_lines(workflow_path: Path, step_name: str) -> tuple[list[str], int, int]:
    """The lines of one `- name: <step_name>` step, with its bounds.

    Returns the workflow's lines, the index of the step's `- name:` line, and
    the index one past the step's last line.
    """
    lines = workflow_path.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one {step_name!r} step in {workflow_path}, found {len(starts)}")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and len(lines[i]) - len(lines[i].lstrip()) <= indent:
            end = i
            break
    return lines, start, end


def step_run_body(workflow_path: Path, step_name: str) -> str:
    """The dedented shell of a step's `run: |` block."""
    lines, start, end = _step_lines(workflow_path, step_name)
    for i in range(start + 1, end):
        if lines[i].strip() != "run: |":
            continue
        body_indent = len(lines[i]) - len(lines[i].lstrip()) + 2
        body = []
        for line in lines[i + 1 : end]:
            if line.strip() and len(line) - len(line.lstrip()) < body_indent:
                break
            body.append(line[body_indent:] if len(line) >= body_indent else "")
        return "\n".join(body).rstrip() + "\n"
    raise AssertionError(f"{step_name!r} in {workflow_path} has no `run: |` block")


def step_env(workflow_path: Path, step_name: str) -> dict[str, str]:
    """A step's `env:` mapping, as written (expressions left unevaluated)."""
    lines, start, end = _step_lines(workflow_path, step_name)
    for i in range(start + 1, end):
        if lines[i].strip() != "env:":
            continue
        env_indent = len(lines[i]) - len(lines[i].lstrip())
        env = {}
        for line in lines[i + 1 : end]:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= env_indent:
                break
            key, _, value = line.strip().partition(": ")
            env[key] = value
        return env
    return {}


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


class TriageWorkflowTests(unittest.TestCase):
    def run_label_step(
        self,
        *,
        title: str,
        body: str,
        number: str = "42",
        repo: str = "Danathar/atomic-image-builder",
        label_create_exit: int = 0,
        issue_edit_exit: int = 0,
    ) -> StepRun:
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
            # The three variables triage.yml's `env:` block supplies. The
            # test below pins that mapping so a rename in the workflow cannot
            # leave this harness silently feeding the old names.
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

    def test_the_harness_supplies_exactly_the_variables_the_step_declares(self) -> None:
        env = step_env(TRIAGE_WORKFLOW, LABEL_STEP)
        self.assertEqual(sorted(env), ["GH_TOKEN", "NUMBER", "REPO"])
        # NUMBER has to work for both triggers the workflow declares: the
        # issues event carries the number, workflow_dispatch takes it as an
        # input. Losing either side makes one trigger label issue "".
        self.assertEqual(env["NUMBER"], "${{ github.event.issue.number || inputs.issue }}")

    def test_an_acmm_level_in_the_title_becomes_a_label(self) -> None:
        result = self.run_label_step(title="[ACMM L2] Harden the signing policy", body="Some body.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), ["acmm-l2"])

    def test_every_declared_acmm_level_maps_to_its_own_label(self) -> None:
        for level in range(5):
            with self.subTest(level=level):
                result = self.run_label_step(title=f"[ACMM L{level}] Something", body="Body.")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.applied_labels(), [f"acmm-l{level}"])

    def test_an_unknown_acmm_level_matches_no_rule(self) -> None:
        # The case arms are literal, so a level the table does not list must
        # fall through rather than produce an acmm-l5 label nobody defined.
        result = self.run_label_step(title="[ACMM L5] Something", body="Body.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), [])
        self.assertIn("No label rule matched issue #42.", result.stdout)

    def test_security_keywords_match_regardless_of_case(self) -> None:
        # The rule shipped case-sensitive and let exactly these two titles
        # through. Both are lowercased before matching now.
        for title in ("Cosign verification fails", "Signing key is missing"):
            with self.subTest(title=title):
                result = self.run_label_step(title=title, body="No detail.")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.applied_labels(), ["security"])

    def test_every_security_keyword_is_matched_in_the_body(self) -> None:
        for keyword in ("cosign", "signing key", "gh_token", "credential", "prompt injection"):
            with self.subTest(keyword=keyword):
                result = self.run_label_step(title="Build fails", body=f"Mentions {keyword} here.")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.applied_labels(), ["security"])

    def test_the_bug_forms_own_checkbox_does_not_label_every_report_security(self) -> None:
        # The bug form's pre-submit checkbox names "tokens, cosign keys" in
        # every body it produces. Matching the raw body labelled every bug
        # report `security`; the line is stripped before matching.
        body = "I checked the pasted output for tokens, cosign keys, and other secrets.\n\nThe build fails on step 3."
        result = self.run_label_step(title="Build fails on step 3", body=body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), [])
        self.assertIn("No label rule matched issue #42.", result.stdout)

    def test_stripping_the_checkbox_does_not_hide_a_real_report_beside_it(self) -> None:
        # Only the boilerplate line goes; a genuine mention elsewhere in the
        # same body still has to win.
        body = "I checked the pasted output for tokens, cosign keys, and other secrets.\n\nCosign verification rejects our own image."
        result = self.run_label_step(title="Verification fails", body=body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), ["security"])

    def test_both_rules_can_apply_to_one_issue(self) -> None:
        result = self.run_label_step(title="[ACMM L3] Cosign policy is unsigned", body="Body.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), ["acmm-l3", "security"])

    def test_nothing_is_sent_when_no_rule_matches(self) -> None:
        result = self.run_label_step(title="Typo in README", body="Second paragraph has a typo.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.label_creates(), [])
        self.assertEqual(result.issue_edits(), [])
        self.assertIn("No label rule matched issue #42.", result.stdout)

    def test_labelling_is_additive_and_never_removes_a_human_decision(self) -> None:
        # The workflow's stated contract: a person's label always wins.
        result = self.run_label_step(title="[ACMM L1] Cosign keys rotate", body="Body.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--remove-label", [arg for call in result.calls for arg in call])
        for call in result.issue_edits():
            self.assertIn("--add-label", call)

    def test_the_label_is_created_before_it_is_applied(self) -> None:
        result = self.run_label_step(title="[ACMM L0] Baseline", body="Body.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.calls[2:],
            [
                [
                    "label",
                    "create",
                    "acmm-l0",
                    "--repo",
                    "Danathar/atomic-image-builder",
                    "--color",
                    "ededed",
                    "--description",
                    "Applied by triage.yml",
                ],
                [
                    "issue",
                    "edit",
                    "42",
                    "--repo",
                    "Danathar/atomic-image-builder",
                    "--add-label",
                    "acmm-l0",
                ],
            ],
        )

    def test_an_already_existing_label_still_gets_applied(self) -> None:
        # `gh label create` exits non-zero when the label exists, which is the
        # ordinary case. Under `set -e` that has to stay swallowed, or triage
        # would only ever work on labels nobody has created yet.
        result = self.run_label_step(
            title="[ACMM L4] Something", body="Body.", label_create_exit=1
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), ["acmm-l4"])
        self.assertIn("Applied acmm-l4 to #42.", result.stdout)

    def test_a_failed_edit_fails_the_step(self) -> None:
        # The swallowed failure above is scoped to `label create`; a rejected
        # edit is a real failure and must not be reported as a clean run.
        result = self.run_label_step(
            title="[ACMM L2] Something", body="Body.", issue_edit_exit=1
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Applied acmm-l2 to #42.", result.stdout)

    def test_the_issue_number_and_repo_come_from_the_environment(self) -> None:
        result = self.run_label_step(
            title="[ACMM L1] Something",
            body="Body.",
            number="1234",
            repo="Danathar/other-repo",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in result.calls:
            self.assertIn("Danathar/other-repo", call)
        self.assertEqual(
            result.issue_edits(),
            [["issue", "edit", "1234", "--repo", "Danathar/other-repo", "--add-label", "acmm-l1"]],
        )


if __name__ == "__main__":
    unittest.main()

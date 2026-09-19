"""Execute the `run:` body of .github/workflows/triage.yml.

Nothing in the suite ran a workflow step. `tests/test_workflow_dependencies.py`
reads every workflow, but only to compare pins and install commands across
them -- the shell inside `run:` is never executed, so the issue-labelling rules
in triage.yml were checked by review alone. Two of those rules are there
because they were wrong the first time (the bug form's own boilerplate matched
every report, and `case` matched nothing capitalised); a silent regression to
either state would flood or empty the `security` label with no test failing.

The harness lives in `tests/_triage_step.py` so the issue-form suite can drive
the same step with a body rendered out of the form itself. Bodies here are
written by hand, one per labelling rule.
"""

import unittest

from _triage_step import LABEL_STEP, TRIAGE_WORKFLOW, StepRun, run_label_step
from _workflow_steps import step_env


class TriageWorkflowTests(unittest.TestCase):
    def run_label_step(self, **kwargs: object) -> StepRun:
        return run_label_step(**kwargs)  # type: ignore[arg-type]

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

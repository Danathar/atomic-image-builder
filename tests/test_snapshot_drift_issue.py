import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snapshot_drift_issue import (
    ISSUE_TITLE,
    collect_drift,
    find_tracking_issue,
    main,
    render_body,
    run_gh,
    sync,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def issue_list(*issues: dict) -> str:
    return json.dumps(list(issues))


class RunGhTests(unittest.TestCase):
    def test_run_gh_returns_stdout(self) -> None:
        with patch("snapshot_drift_issue.subprocess.run", return_value=gh_result("[]")) as run:
            self.assertEqual(run_gh(["issue", "list"]), "[]")
        self.assertEqual(run.call_args.args[0][:3], ["gh", "issue", "list"])

    def test_run_gh_raises_with_the_command_output(self) -> None:
        with patch("snapshot_drift_issue.subprocess.run", return_value=gh_result("", 1, "not authenticated")):
            with self.assertRaisesRegex(RuntimeError, "not authenticated"):
                run_gh(["issue", "list"])

    def test_run_gh_raises_when_gh_is_not_installed(self) -> None:
        with patch("snapshot_drift_issue.subprocess.run", side_effect=OSError("No such file")):
            with self.assertRaisesRegex(RuntimeError, "could not run gh"):
                run_gh(["issue", "list"])

    def test_run_gh_raises_even_when_the_command_said_nothing(self) -> None:
        with patch("snapshot_drift_issue.subprocess.run", return_value=gh_result("", 1, "")):
            with self.assertRaisesRegex(RuntimeError, "gh issue list failed"):
                run_gh(["issue", "list"])


class CollectDriftTests(unittest.TestCase):
    def test_collect_drift_reports_both_buckets(self) -> None:
        # An issue tracking only the advisory would close itself at exactly the
        # point the drift got bad enough to fail the audit.
        with patch(
            "snapshot_drift_issue.audit_upstream_drift",
            side_effect=[(["far behind"], []), ([], ["slightly behind"])],
        ):
            messages = collect_drift(REPO_ROOT)
        self.assertEqual(messages, ["far behind", "slightly behind"])

    def test_collect_drift_is_quiet_when_the_snapshots_match(self) -> None:
        with patch("snapshot_drift_issue.audit_upstream_drift", return_value=([], [])):
            self.assertEqual(collect_drift(REPO_ROOT), [])

    def test_collect_drift_skips_an_unloadable_source(self) -> None:
        # A missing .template-source already fails the audit's local checks.
        # This script must not crash on top of that.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("snapshot_drift_issue.audit_upstream_drift") as drift:
                self.assertEqual(collect_drift(Path(tmp)), [])
            drift.assert_not_called()


class RenderBodyTests(unittest.TestCase):
    def test_render_body_lists_every_message(self) -> None:
        body = render_body(["snapshot a behind", "snapshot b behind"])
        self.assertIn("- snapshot a behind", body)
        self.assertIn("- snapshot b behind", body)
        self.assertIn("closes itself", body)
        self.assertNotIn("Last checked", body)

    def test_render_body_records_the_run_url_when_given(self) -> None:
        body = render_body(["drift"], run_url="https://example.test/run/1")
        self.assertIn("Last checked: https://example.test/run/1", body)


class FindTrackingIssueTests(unittest.TestCase):
    def test_find_tracking_issue_matches_on_exact_title(self) -> None:
        payload = issue_list(
            {"number": 3, "title": "something else", "body": "x"},
            {"number": 7, "title": ISSUE_TITLE, "body": "current"},
        )
        with patch("snapshot_drift_issue.run_gh", return_value=payload):
            self.assertEqual(find_tracking_issue(), (7, "current"))

    def test_find_tracking_issue_returns_none_when_absent(self) -> None:
        with patch("snapshot_drift_issue.run_gh", return_value=issue_list({"number": 3, "title": "other"})):
            self.assertIsNone(find_tracking_issue())

    def test_find_tracking_issue_tolerates_junk_entries_and_empty_output(self) -> None:
        payload = issue_list({"number": "not-an-int", "title": ISSUE_TITLE})
        with patch("snapshot_drift_issue.run_gh", return_value=payload):
            self.assertIsNone(find_tracking_issue())
        with patch("snapshot_drift_issue.run_gh", return_value=""):
            self.assertIsNone(find_tracking_issue())

    def test_find_tracking_issue_defaults_a_missing_body(self) -> None:
        with patch("snapshot_drift_issue.run_gh", return_value=issue_list({"number": 7, "title": ISSUE_TITLE})):
            self.assertEqual(find_tracking_issue(), (7, ""))

    def test_find_tracking_issue_rejects_non_json_output(self) -> None:
        # gh can exit 0 and print something that is not JSON -- an auth notice,
        # an upgrade warning. That has to become a RuntimeError, because it is
        # the only thing main() catches; anything else fails the weekly audit.
        with patch("snapshot_drift_issue.run_gh", return_value="gh: upgrade available"):
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON from gh issue list"):
                find_tracking_issue()

    def test_find_tracking_issue_rejects_a_non_list_payload(self) -> None:
        with patch("snapshot_drift_issue.run_gh", return_value='{"number": 1}'):
            with self.assertRaisesRegex(RuntimeError, "Unexpected issue list payload"):
                find_tracking_issue()

    def test_find_tracking_issue_passes_the_repo_through(self) -> None:
        with patch("snapshot_drift_issue.run_gh", return_value="[]") as gh:
            find_tracking_issue("owner/name")
        self.assertIn("--repo", gh.call_args.args[0])
        self.assertIn("owner/name", gh.call_args.args[0])


class SyncTests(unittest.TestCase):
    def test_sync_opens_an_issue_when_drift_appears(self) -> None:
        with patch("snapshot_drift_issue.collect_drift", return_value=["snapshot behind"]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=None):
                with patch("snapshot_drift_issue.run_gh", return_value="https://example.test/issues/9\n") as gh:
                    message = sync(REPO_ROOT)
        self.assertIn("https://example.test/issues/9", message)
        self.assertIn("issue", gh.call_args.args[0])
        self.assertIn("create", gh.call_args.args[0])

    def test_sync_updates_the_existing_issue_when_the_drift_changed(self) -> None:
        with patch("snapshot_drift_issue.collect_drift", return_value=["now 30 commits behind"]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=(9, "stale body")):
                with patch("snapshot_drift_issue.run_gh", return_value="") as gh:
                    message = sync(REPO_ROOT)
        self.assertIn("updated #9", message)
        self.assertIn("edit", gh.call_args.args[0])

    def test_sync_leaves_an_unchanged_issue_alone(self) -> None:
        # A weekly edit with identical content is the same noise this exists to
        # avoid, one inbox over.
        body = render_body(["unchanged drift"], run_url="https://example.test/run/1")
        with patch("snapshot_drift_issue.collect_drift", return_value=["unchanged drift"]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=(9, body)):
                with patch("snapshot_drift_issue.run_gh") as gh:
                    message = sync(REPO_ROOT, run_url="https://example.test/run/2")
        gh.assert_not_called()
        self.assertIn("unchanged", message)

    def test_sync_closes_the_issue_once_the_drift_clears(self) -> None:
        with patch("snapshot_drift_issue.collect_drift", return_value=[]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=(9, "body")):
                with patch("snapshot_drift_issue.run_gh", return_value="") as gh:
                    message = sync(REPO_ROOT)
        self.assertIn("closed #9", message)
        commands = [call.args[0] for call in gh.call_args_list]
        self.assertIn("comment", commands[0])
        self.assertIn("close", commands[1])

    def test_sync_does_nothing_when_clean_and_no_issue_is_open(self) -> None:
        with patch("snapshot_drift_issue.collect_drift", return_value=[]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=None):
                with patch("snapshot_drift_issue.run_gh") as gh:
                    message = sync(REPO_ROOT)
        gh.assert_not_called()
        self.assertIn("nothing to do", message)

    def test_sync_passes_the_repo_through_to_every_write(self) -> None:
        with patch("snapshot_drift_issue.collect_drift", return_value=[]):
            with patch("snapshot_drift_issue.find_tracking_issue", return_value=(9, "body")):
                with patch("snapshot_drift_issue.run_gh", return_value="") as gh:
                    sync(REPO_ROOT, repo="owner/name")
        for call in gh.call_args_list:
            self.assertIn("--repo", call.args[0])


class MainTests(unittest.TestCase):
    def test_main_prints_the_outcome(self) -> None:
        with patch("snapshot_drift_issue.sync", return_value="did a thing") as synced:
            with patch("builtins.print") as printed:
                self.assertEqual(main(["--repo-root", str(REPO_ROOT), "--run-url", "u"]), 0)
        printed.assert_called_once_with("did a thing")
        self.assertEqual(synced.call_args.kwargs["run_url"], "u")

    def test_main_never_fails_the_audit_over_issue_bookkeeping(self) -> None:
        # The audit's own exit code is the signal. A GitHub hiccup here must
        # not turn a green audit red, which would recreate the problem #129
        # was filed about.
        with patch("snapshot_drift_issue.sync", side_effect=RuntimeError("gh exploded")):
            with patch("builtins.print") as printed:
                self.assertEqual(main([]), 0)
        self.assertIn("Could not sync", printed.call_args.args[0])
        self.assertIn("gh exploded", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

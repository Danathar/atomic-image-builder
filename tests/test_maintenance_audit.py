import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from maintenance_audit import (
    TemplateSource,
    audit_action_update_availability,
    audit_local_snapshot,
    audit_upstream_drift,
    github_api_json,
    is_newer_version_available,
    load_template_source,
    main,
    parse_version_tag,
    query_latest_github_semver_tag,
    query_remote_head,
    run_audit,
    version_tag_precision,
)


class FakeResponse:
    """Minimal context-manager stand-in for urllib.request.urlopen."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode()


class MaintenanceAuditTests(unittest.TestCase):
    def test_parse_version_tag_accepts_major_and_semver_forms(self) -> None:
        self.assertEqual(parse_version_tag("v6"), (6, 0, 0))
        self.assertEqual(parse_version_tag("v4.2.1"), (4, 2, 1))
        self.assertIsNone(parse_version_tag("main"))

    def test_is_newer_version_available_respects_current_label_precision(self) -> None:
        self.assertFalse(is_newer_version_available("v6", "v6.0.2"))
        self.assertTrue(is_newer_version_available("v8", "v9"))
        self.assertTrue(is_newer_version_available("v4.0.0", "v4.1.1"))

    def test_is_newer_version_available_at_minor_precision(self) -> None:
        self.assertTrue(is_newer_version_available("v4.2", "v4.3"))
        self.assertFalse(is_newer_version_available("v4.2", "v4.2.5"))

    def test_is_newer_version_available_rejects_unparseable_tags(self) -> None:
        self.assertFalse(is_newer_version_available("main", "v1"))
        self.assertFalse(is_newer_version_available("v1", "main"))

    def test_version_tag_precision_reports_specified_components(self) -> None:
        self.assertEqual(version_tag_precision("v6"), 1)
        self.assertEqual(version_tag_precision("v4.2"), 2)
        self.assertEqual(version_tag_precision("v4.2.1"), 3)
        self.assertIsNone(version_tag_precision("main"))

    def test_load_template_source_rejects_invalid_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / ".template-source"
            source_path.write_text("repo=https://github.com/example/repo.git\nrevision=not-a-sha\n")
            with self.assertRaisesRegex(ValueError, "invalid revision"):
                load_template_source(source_path)

    def test_audit_local_snapshot_passes_for_current_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_local_snapshot(repo_root), [])

    def test_audit_local_snapshot_reports_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Set up containerfile template with an unknown action.
            cf_workflow_dir = repo_root / "template_snapshots/containerfile/.github/workflows"
            cf_workflow_dir.mkdir(parents=True)
            (repo_root / "template_snapshots/containerfile/.template-source").write_text(
                "repo=https://github.com/example/repo.git\nrevision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            )
            (cf_workflow_dir / "build.yml").write_text(
                "jobs:\n  build:\n    steps:\n      - uses: example/custom-action@v1\n"
            )
            # Set up bluebuild template with a valid (empty) workflow so it
            # does not add extra findings.
            bb_workflow_dir = repo_root / "template_snapshots/bluebuild/.github/workflows"
            bb_workflow_dir.mkdir(parents=True)
            (repo_root / "template_snapshots/bluebuild/.template-source").write_text(
                "repo=https://github.com/example/bb.git\nrevision=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            )
            (bb_workflow_dir / "build.yml").write_text("jobs: {}\n")

            findings = audit_local_snapshot(repo_root)

        self.assertEqual(len(findings), 1)
        self.assertIn("not covered by ACTION_PINS", findings[0])

    def test_audit_local_snapshot_discovers_root_and_secondary_yaml_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for template, repo_name, revision in (
                ("containerfile", "container", "a" * 40),
                ("bluebuild", "bluebuild", "b" * 40),
            ):
                workflow_dir = repo_root / f"template_snapshots/{template}/.github/workflows"
                workflow_dir.mkdir(parents=True)
                (repo_root / f"template_snapshots/{template}/.template-source").write_text(
                    f"repo=https://github.com/example/{repo_name}.git\nrevision={revision}\n"
                )
                (workflow_dir / "build.yml").write_text("jobs: {}\n")

            root_workflow_dir = repo_root / ".github/workflows"
            root_workflow_dir.mkdir(parents=True)
            (root_workflow_dir / "secondary.yaml").write_text(
                "jobs:\n  check:\n    steps:\n      - uses: actions/checkout@deadbeef\n"
            )
            secondary_snapshot = repo_root / "template_snapshots/containerfile/.github/workflows/secondary.yml"
            secondary_snapshot.write_text(
                "jobs:\n  check:\n    steps:\n      - uses: example/unknown-action@v1\n"
            )

            findings = audit_local_snapshot(repo_root)

        self.assertEqual(len(findings), 2)
        self.assertTrue(any("does not match the pin table SHA" in finding for finding in findings))
        self.assertTrue(any("not covered by ACTION_PINS" in finding for finding in findings))

    def test_audit_upstream_drift_reports_head_changes(self) -> None:
        source = TemplateSource(
            repo="https://github.com/ublue-os/image-template.git",
            revision="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        completed = subprocess.CompletedProcess(
            ["git", "ls-remote"],
            0,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tHEAD\n",
            "",
        )
        with patch("maintenance_audit.subprocess.run", return_value=completed):
            findings = audit_upstream_drift(source)

        self.assertEqual(len(findings), 1)
        self.assertIn("differs from upstream HEAD", findings[0])

    def test_audit_action_update_availability_reports_newer_tags(self) -> None:
        actions = {"docker/login-action": ("deadbeef", "v4.0.0")}
        with patch("maintenance_audit.query_latest_github_semver_tag", return_value="v4.1.0"):
            findings = audit_action_update_availability(actions)

        self.assertEqual(len(findings), 1)
        self.assertIn("latest upstream tag", findings[0])

    def test_audit_action_update_availability_ignores_non_version_labels(self) -> None:
        actions = {"example/custom-action": ("deadbeef", "main")}
        findings = audit_action_update_availability(actions)
        self.assertEqual(findings, [])

    def test_audit_action_update_availability_reports_query_failures(self) -> None:
        actions = {"docker/login-action": ("deadbeef", "v4.0.0")}
        with patch(
            "maintenance_audit.query_latest_github_semver_tag",
            side_effect=RuntimeError("rate limited"),
        ):
            findings = audit_action_update_availability(actions)
        self.assertEqual(len(findings), 1)
        self.assertIn("Unable to query upstream tags", findings[0])
        self.assertIn("rate limited", findings[0])

    def test_audit_action_update_availability_quiet_when_current_or_untagged(self) -> None:
        actions = {"docker/login-action": ("deadbeef", "v4.0.0")}
        # No semver tag upstream at all, and an upstream that is not newer:
        # both must produce no findings.
        with patch("maintenance_audit.query_latest_github_semver_tag", return_value=None):
            self.assertEqual(audit_action_update_availability(actions), [])
        with patch("maintenance_audit.query_latest_github_semver_tag", return_value="v4.0.0"):
            self.assertEqual(audit_action_update_availability(actions), [])

    def test_query_remote_head_raises_on_git_failure_and_bad_output(self) -> None:
        failed = subprocess.CompletedProcess(["git", "ls-remote"], 128, "", "fatal: not found")
        with patch("maintenance_audit.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                query_remote_head("https://github.com/example/repo.git")

        garbled = subprocess.CompletedProcess(["git", "ls-remote"], 0, "not-a-sha\tHEAD\n", "")
        with patch("maintenance_audit.subprocess.run", return_value=garbled):
            with self.assertRaisesRegex(RuntimeError, "Unexpected ls-remote output"):
                query_remote_head("https://github.com/example/repo.git")

    def test_audit_upstream_drift_reports_query_failure_as_finding(self) -> None:
        source = TemplateSource(
            repo="https://github.com/example/repo.git",
            revision="a" * 40,
        )
        with patch("maintenance_audit.query_remote_head", side_effect=RuntimeError("offline")):
            findings = audit_upstream_drift(source)
        self.assertEqual(len(findings), 1)
        self.assertIn("Unable to query upstream template HEAD", findings[0])
        self.assertIn("offline", findings[0])

    def test_github_api_json_sends_token_header_only_when_set(self) -> None:
        captured: list = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return FakeResponse({"ok": True})

        with patch("maintenance_audit.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch.dict("os.environ", {"GITHUB_TOKEN": "secret-token"}, clear=False):
                self.assertEqual(github_api_json("https://api.github.com/x"), {"ok": True})
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(github_api_json("https://api.github.com/x"), {"ok": True})

        with_token, without_token = captured
        self.assertEqual(with_token.get_header("Authorization"), "Bearer secret-token")
        self.assertIsNone(without_token.get_header("Authorization"))
        self.assertEqual(with_token.get_header("Accept"), "application/vnd.github+json")

    def test_github_api_json_wraps_http_and_url_errors(self) -> None:
        http_error = urllib.error.HTTPError(
            "https://api.github.com/x", 403, "Forbidden", {}, io.BytesIO(b"rate limit exceeded")
        )
        with patch("maintenance_audit.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(RuntimeError, "rate limit exceeded"):
                github_api_json("https://api.github.com/x")

        empty_body = urllib.error.HTTPError(
            "https://api.github.com/x", 500, "Server Error", {}, io.BytesIO(b"")
        )
        with patch("maintenance_audit.urllib.request.urlopen", side_effect=empty_body):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                github_api_json("https://api.github.com/x")

        url_error = urllib.error.URLError("name resolution failed")
        with patch("maintenance_audit.urllib.request.urlopen", side_effect=url_error):
            with self.assertRaisesRegex(RuntimeError, "name resolution failed"):
                github_api_json("https://api.github.com/x")

    def test_query_latest_github_semver_tag_picks_highest_and_skips_noise(self) -> None:
        payload = [
            "not-a-dict",
            {"name": 42},
            {"name": "main"},
            {"name": "v1.9.9"},
            {"name": "v2.0.1"},
            {"name": "v2.0.0"},
        ]
        with patch("maintenance_audit.github_api_json", return_value=payload):
            self.assertEqual(query_latest_github_semver_tag("example/action"), "v2.0.1")

    def test_query_latest_github_semver_tag_handles_empty_and_bad_payloads(self) -> None:
        with patch("maintenance_audit.github_api_json", return_value=[{"name": "main"}]):
            self.assertIsNone(query_latest_github_semver_tag("example/action"))
        with patch("maintenance_audit.github_api_json", return_value={"message": "Not Found"}):
            with self.assertRaisesRegex(RuntimeError, "Unexpected tag payload"):
                query_latest_github_semver_tag("example/action")

    def test_run_audit_skip_upstream_runs_only_local_checks(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch("maintenance_audit.audit_upstream_drift") as drift:
            with patch("maintenance_audit.audit_action_update_availability") as updates:
                findings = run_audit(repo_root, skip_upstream=True)
        drift.assert_not_called()
        updates.assert_not_called()
        self.assertEqual(findings, [])

    def test_run_audit_queries_drift_per_template_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_upstream_drift", return_value=["drift finding"]
        ) as drift:
            findings = run_audit(repo_root, skip_upstream=False)
        self.assertEqual(drift.call_count, 2)
        self.assertEqual(findings, ["drift finding", "drift finding"])

    def test_run_audit_tolerates_unloadable_source_when_querying_upstream(self) -> None:
        # A missing/invalid .template-source is already reported by the local
        # audit; the upstream pass must skip it rather than crash.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("maintenance_audit.audit_upstream_drift") as drift:
                findings = run_audit(repo_root, skip_upstream=False)
        drift.assert_not_called()
        self.assertTrue(any("Missing template metadata file" in f for f in findings))

    def test_run_audit_appends_action_update_findings_when_asked(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_action_update_availability", return_value=["stale pin"]
        ):
            findings = run_audit(repo_root, skip_upstream=True, check_action_updates=True)
        self.assertEqual(findings, ["stale pin"])

    def test_main_passing_audit_prints_success_and_returns_zero(self) -> None:
        stdout = io.StringIO()
        with patch("maintenance_audit.run_audit", return_value=[]) as run:
            with contextlib.redirect_stdout(stdout):
                code = main(["--skip-upstream"])
        self.assertEqual(code, 0)
        self.assertIn("Maintenance audit passed.", stdout.getvalue())
        self.assertEqual(run.call_args.kwargs["skip_upstream"], True)
        self.assertEqual(run.call_args.kwargs["check_action_updates"], False)

    def test_main_failing_audit_lists_findings_and_returns_one(self) -> None:
        stdout = io.StringIO()
        with patch("maintenance_audit.run_audit", return_value=["first", "second"]):
            with contextlib.redirect_stdout(stdout):
                code = main([])
        self.assertEqual(code, 1)
        output = stdout.getvalue()
        self.assertIn("Maintenance audit failed:", output)
        self.assertIn("- first", output)
        self.assertIn("- second", output)

    def test_main_forwards_repo_root_and_update_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("maintenance_audit.run_audit", return_value=[]) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--repo-root", tmp, "--skip-upstream", "--check-action-updates"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], Path(tmp).resolve())
        self.assertEqual(run.call_args.kwargs["check_action_updates"], True)

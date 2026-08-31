import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from _local_http_server import closed_port_url, local_http_server
from maintenance_audit import (
    TemplateSource,
    audit_action_pin_freshness,
    audit_action_update_availability,
    audit_local_snapshot,
    audit_upstream_drift,
    describe_pin_drift,
    describe_snapshot_drift,
    github_api_json,
    github_repo_slug,
    is_newer_version_available,
    iter_pinned_refs,
    load_template_source,
    main,
    parse_version_tag,
    query_github_comparison,
    query_github_ref_sha,
    query_latest_github_semver_tag,
    query_remote_head,
    run_audit,
    version_tag_precision,
)


class FakeResponse:
    """Minimal context-manager stand-in for urllib.request.urlopen."""

    def __init__(self, payload: object, *, raw: bytes | None = None) -> None:
        self._body = raw if raw is not None else json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


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

    def test_load_template_source_rejects_missing_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / ".template-source"
            source_path.write_text("revision=" + "a" * 40 + "\n")
            with self.assertRaisesRegex(ValueError, "missing repo="):
                load_template_source(source_path)

    def test_load_template_source_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / ".template-source"
            source_path.write_text(
                "\n# a comment\nrepo=https://github.com/example/repo.git\n\nrevision=" + "a" * 40 + "\n"
            )
            source = load_template_source(source_path)
        self.assertEqual(source.repo, "https://github.com/example/repo.git")
        self.assertEqual(source.revision, "a" * 40)

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
            with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 3, 0)):
                findings = audit_upstream_drift(source)

        self.assertEqual(len(findings), 1)
        self.assertIn("trails upstream HEAD", findings[0])

    def test_describe_snapshot_drift_states_how_far_behind(self) -> None:
        # The advisory has to be triageable without a checkout: three commits
        # behind is an ordinary week, eighty is a snapshot nobody has looked at.
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 47, 0)) as compare:
            message = describe_snapshot_drift(source, "b" * 40)
        compare.assert_called_once_with("ublue-os/image-template", "a" * 40, "b" * 40)
        self.assertIn("47 commit(s) newer", message)
        self.assertIn("Refresh the snapshot", message)

    def test_describe_snapshot_drift_refuses_to_recommend_a_rollback(self) -> None:
        # A force-pushed upstream branch can move HEAD backwards, and then
        # "refresh to HEAD" would roll the bundled snapshot back.
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("behind", 0, 9)):
            message = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("9 commit(s) OLDER", message)
        self.assertIn("Look before refreshing", message)
        self.assertNotIn("Refresh the snapshot", message)

    def test_describe_snapshot_drift_flags_diverged_history(self) -> None:
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("diverged", 2, 5)):
            message = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("diverged history", message)

    def test_describe_snapshot_drift_falls_back_when_compare_fails(self) -> None:
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", side_effect=RuntimeError("rate limited")):
            message = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("trails upstream HEAD", message)
        self.assertIn("Refresh the snapshot", message)
        self.assertNotIn("commit(s)", message)

    def test_describe_snapshot_drift_skips_compare_for_a_non_github_remote(self) -> None:
        # The compare API is GitHub-only; a snapshot pinned to any other host
        # still has to produce a usable advisory rather than an exception.
        source = TemplateSource(repo="https://gitlab.com/example/template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison") as compare:
            message = describe_snapshot_drift(source, "b" * 40)
        compare.assert_not_called()
        self.assertIn("trails upstream HEAD", message)

    def test_github_repo_slug_parses_clone_urls(self) -> None:
        self.assertEqual(github_repo_slug("https://github.com/ublue-os/image-template.git"), "ublue-os/image-template")
        self.assertEqual(github_repo_slug("https://github.com/blue-build/template"), "blue-build/template")
        self.assertEqual(github_repo_slug("git@github.com:owner/repo.git"), "owner/repo")
        self.assertIsNone(github_repo_slug("https://gitlab.com/owner/repo.git"))

    def test_audit_upstream_drift_quiet_when_head_matches_pinned_revision(self) -> None:
        source = TemplateSource(
            repo="https://github.com/ublue-os/image-template.git",
            revision="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        with patch("maintenance_audit.query_remote_head", return_value=source.revision):
            findings = audit_upstream_drift(source)
        self.assertEqual(findings, [])

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

    def test_github_api_json_wraps_response_errors(self) -> None:
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

        invalid_json = FakeResponse(None, raw=b"not json")
        with patch("maintenance_audit.urllib.request.urlopen", return_value=invalid_json):
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON response from GitHub"):
                github_api_json("https://api.github.com/x")

        invalid_encoding = FakeResponse(None, raw=b"\x80")
        with patch("maintenance_audit.urllib.request.urlopen", return_value=invalid_encoding):
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON response from GitHub"):
                github_api_json("https://api.github.com/x")

    # The three tests below hit a real loopback socket instead of a mocked
    # urlopen, so urllib.request's actual connect/read/error-parsing code runs
    # -- the class of branch the weekly maintenance-audit real-run coverage
    # (see .coveragerc.maintenance-audit) showed the mocked suite above cannot
    # reach on its own (issue #123).
    def test_github_api_json_real_rate_limit_response_from_local_server(self) -> None:
        body = b'{"message": "API rate limit exceeded for 127.0.0.1."}'
        with local_http_server(status=403, body=body) as url:
            with self.assertRaisesRegex(RuntimeError, "rate limit exceeded"):
                github_api_json(url)

    def test_github_api_json_real_malformed_json_response_from_local_server(self) -> None:
        with local_http_server(status=200, body=b"not-json{") as url:
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON response from GitHub"):
                github_api_json(url)

    def test_github_api_json_real_connection_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            github_api_json(closed_port_url())

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
                findings, advisories = run_audit(repo_root, skip_upstream=True)
        drift.assert_not_called()
        updates.assert_not_called()
        self.assertEqual(findings, [])
        self.assertEqual(advisories, [])

    def test_run_audit_queries_drift_per_template_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_upstream_drift", return_value=["drift advisory"]
        ) as drift:
            findings, advisories = run_audit(repo_root, skip_upstream=False)
        self.assertEqual(drift.call_count, 2)
        self.assertEqual(advisories, ["drift advisory", "drift advisory"])

    def test_run_audit_returns_snapshot_drift_as_advisory_not_failure(self) -> None:
        # Both template upstreams are active, so the pin differs from their
        # HEAD most weeks. As a failure this took the weekly job red 5 of its
        # last 6 scheduled runs, which would have buried a real finding from
        # audit_local_snapshot() in an already-red run. See issue #129.
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_upstream_drift", return_value=["snapshot trails upstream"]
        ):
            findings, advisories = run_audit(repo_root, skip_upstream=False)
        self.assertEqual(findings, [])
        self.assertIn("snapshot trails upstream", advisories)

    def test_run_audit_tolerates_unloadable_source_when_querying_upstream(self) -> None:
        # A missing/invalid .template-source is already reported by the local
        # audit; the upstream pass must skip it rather than crash.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("maintenance_audit.audit_upstream_drift") as drift:
                findings, _advisories = run_audit(repo_root, skip_upstream=False)
        drift.assert_not_called()
        # Still a failure: a missing metadata file is the repo contradicting
        # itself, which is a different bucket from upstream having moved.
        self.assertTrue(any("Missing template metadata file" in f for f in findings))

    def test_run_audit_returns_action_updates_as_advisories_not_failures(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_action_update_availability", return_value=["stale pin"]
        ):
            with patch(
                "maintenance_audit.audit_action_pin_freshness", return_value=["moved pin"]
            ):
                findings, advisories = run_audit(
                    repo_root, skip_upstream=True, check_action_updates=True
                )
        # Pin drift must not fail the run -- a branch pin drifts constantly.
        self.assertEqual(findings, [])
        self.assertEqual(advisories, ["stale pin", "moved pin"])

    def test_audit_action_pin_freshness_flags_a_moving_tag_that_left_the_pin_behind(self) -> None:
        # The exact case that shipped actions/checkout v7.0.0 in generated
        # repos: the pin says v7, but upstream's v7 tag has moved on.
        pinned = [("actions/checkout", "v7", "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0")]
        with patch("maintenance_audit.query_github_ref_sha", return_value="3d3c42e5aac5ba805825da76410c181273ba90b1"):
            with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 1, 0)):
                findings = audit_action_pin_freshness(pinned)
        self.assertEqual(len(findings), 1)
        self.assertIn("actions/checkout", findings[0])
        self.assertIn("the tag it names", findings[0])
        self.assertIn("9c091bb21b7c", findings[0])
        self.assertIn("3d3c42e5aac5", findings[0])

    def test_audit_action_pin_freshness_flags_a_branch_pin_and_names_it_a_branch(self) -> None:
        pinned = [("osbuild/bootc-image-builder-action", "main", "8661cd3832544ad68c12dcde8681b13ab0f56a8d")]
        with patch("maintenance_audit.query_github_ref_sha", return_value="56d652d0afb02eb3e4b8fd35e7ca0391dbebab2a"):
            with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 1, 0)):
                findings = audit_action_pin_freshness(pinned)
        self.assertEqual(len(findings), 1)
        self.assertIn("the branch it names", findings[0])

    def test_describe_pin_drift_says_refresh_when_the_ref_is_newer(self) -> None:
        with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 3, 0)):
            message = describe_pin_drift("osbuild/act", "main", "8661cd3832544ad68c12dcde8681b13ab0f56a8d", "56d652d0afb02eb3e4b8fd35e7ca0391dbebab2a")
        self.assertIn("3 commit(s) newer", message)
        self.assertIn("refresh it", message)
        self.assertIn("the branch it names", message)

    def test_describe_pin_drift_warns_against_refreshing_onto_an_older_ref(self) -> None:
        # ublue-os/remove-unwanted-software really is pinned 26 commits ahead of
        # what its v8 tag points at. "Refresh this pin" would be a downgrade.
        with patch("maintenance_audit.query_github_comparison", return_value=("behind", 0, 26)):
            message = describe_pin_drift("ublue-os/remove-unwanted-software", "v8", "695eb75bc387dbcd9685a8e72d23439d8686cba6", "5a8b0374222a6fffddb1be9516b5fece9483bed0")
        self.assertIn("26 commit(s) OLDER than the pin", message)
        self.assertIn("would downgrade the action", message)
        self.assertNotIn("ship the older action", message)

    def test_describe_pin_drift_stays_neutral_when_direction_is_unknown(self) -> None:
        with patch("maintenance_audit.query_github_comparison", return_value=("diverged", 2, 2)):
            self.assertIn("diverged history", describe_pin_drift("o/a", "main", "a" * 40, "b" * 40))
        with patch("maintenance_audit.query_github_comparison", return_value=None):
            self.assertIn("direction of the difference is unknown", describe_pin_drift("o/a", "main", "a" * 40, "b" * 40))
        # A failed compare must not turn into a failed audit.
        with patch("maintenance_audit.query_github_comparison", side_effect=RuntimeError("rate limited")):
            self.assertIn("direction of the difference is unknown", describe_pin_drift("o/a", "main", "a" * 40, "b" * 40))

    def test_query_github_comparison_extracts_status_and_counts(self) -> None:
        with patch("maintenance_audit.github_api_json", return_value={"status": "ahead", "ahead_by": 3, "behind_by": 0}):
            self.assertEqual(query_github_comparison("o/a", "base", "head"), ("ahead", 3, 0))
        # Missing or wrongly typed fields mean "cannot tell", not a crash.
        with patch("maintenance_audit.github_api_json", return_value={"status": "ahead"}):
            self.assertIsNone(query_github_comparison("o/a", "base", "head"))
        with patch("maintenance_audit.github_api_json", return_value=["nope"]):
            with self.assertRaises(RuntimeError):
                query_github_comparison("o/a", "base", "head")

    def test_audit_local_snapshot_reports_an_unreadable_workflow_and_keeps_going(self) -> None:
        # The read failure must be reported and the remaining workflows still
        # audited, rather than aborting the whole local pass.
        repo_root = Path(__file__).resolve().parents[1]
        real_read_text = Path.read_text
        target = repo_root / ".github" / "workflows" / "ci.yml"

        def fake_read_text(self, *args, **kwargs):
            if self == target:
                raise OSError("Permission denied")
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", fake_read_text):
            findings = audit_local_snapshot(repo_root)

        self.assertTrue(any(f"Unable to read {target}" in f for f in findings))
        self.assertTrue(all("is not covered by ACTION_PINS" not in f for f in findings))

    def test_audit_local_snapshot_reports_an_invalid_template_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source = repo_root / "template_snapshots" / "containerfile" / ".template-source"
            source.parent.mkdir(parents=True)
            source.write_text("repo=ublue-os/image-template\nrevision=not-a-sha\n")
            findings = audit_local_snapshot(repo_root)
        # The invalid revision is surfaced by load_template_source, not swallowed.
        self.assertTrue(any("invalid revision" in f for f in findings))
        self.assertTrue(any("Missing template workflow file" in f for f in findings))

    def test_audit_action_pin_freshness_quiet_when_the_ref_still_matches(self) -> None:
        sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        with patch("maintenance_audit.query_github_ref_sha", return_value=sha):
            self.assertEqual(audit_action_pin_freshness([("actions/checkout", "v7", sha)]), [])
        # An unresolvable ref is not evidence of drift either.
        with patch("maintenance_audit.query_github_ref_sha", return_value=None):
            self.assertEqual(audit_action_pin_freshness([("actions/checkout", "v7", sha)]), [])

    def test_audit_action_pin_freshness_reports_query_failures(self) -> None:
        with patch("maintenance_audit.query_github_ref_sha", side_effect=RuntimeError("rate limited")):
            findings = audit_action_pin_freshness([("actions/checkout", "v7", "abc123")])
        self.assertEqual(len(findings), 1)
        self.assertIn("Unable to resolve actions/checkout@v7", findings[0])
        self.assertIn("rate limited", findings[0])

    def test_query_github_ref_sha_extracts_sha_and_rejects_bad_payloads(self) -> None:
        with patch("maintenance_audit.github_api_json", return_value={"sha": "abc"}):
            self.assertEqual(query_github_ref_sha("owner/repo", "main"), "abc")
        with patch("maintenance_audit.github_api_json", return_value={"nope": 1}):
            self.assertIsNone(query_github_ref_sha("owner/repo", "main"))
        with patch("maintenance_audit.github_api_json", return_value=["not a dict"]):
            with self.assertRaises(RuntimeError):
                query_github_ref_sha("owner/repo", "main")

    def test_iter_pinned_refs_dedupes_the_many_spellings_of_one_ref_pin(self) -> None:
        actions = {"owner/act": ("sha1", "v1")}
        ref_pins = {
            "owner/act@v1": ("sha1", "v1"),
            "other/act@main": ("sha2", "main"),
            "other/act@sha2": ("sha2", "main"),
        }
        pinned = iter_pinned_refs(actions, ref_pins)
        self.assertEqual(sorted(pinned), [("other/act", "main", "sha2"), ("owner/act", "v1", "sha1")])

    def test_main_prints_advisories_without_failing(self) -> None:
        stdout = io.StringIO()
        with patch("maintenance_audit.run_audit", return_value=([], ["pin moved"])):
            with contextlib.redirect_stdout(stdout):
                code = main(["--skip-upstream", "--check-action-updates"])
        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Action pin advisories (not failures):", output)
        self.assertIn("- pin moved", output)
        self.assertIn("Maintenance audit passed.", output)

    def test_main_passing_audit_prints_success_and_returns_zero(self) -> None:
        stdout = io.StringIO()
        with patch("maintenance_audit.run_audit", return_value=([], [])) as run:
            with contextlib.redirect_stdout(stdout):
                code = main(["--skip-upstream"])
        self.assertEqual(code, 0)
        self.assertIn("Maintenance audit passed.", stdout.getvalue())
        self.assertEqual(run.call_args.kwargs["skip_upstream"], True)
        self.assertEqual(run.call_args.kwargs["check_action_updates"], False)

    def test_main_failing_audit_lists_findings_and_returns_one(self) -> None:
        stdout = io.StringIO()
        with patch("maintenance_audit.run_audit", return_value=(["first", "second"], [])):
            with contextlib.redirect_stdout(stdout):
                code = main([])
        self.assertEqual(code, 1)
        output = stdout.getvalue()
        self.assertIn("Maintenance audit failed:", output)
        self.assertIn("- first", output)
        self.assertIn("- second", output)

    def test_main_forwards_repo_root_and_update_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("maintenance_audit.run_audit", return_value=([], [])) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--repo-root", tmp, "--skip-upstream", "--check-action-updates"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], Path(tmp).resolve())
        self.assertEqual(run.call_args.kwargs["check_action_updates"], True)

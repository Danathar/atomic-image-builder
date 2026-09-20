import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from _local_http_server import closed_port_url, local_http_server
from atomic_image_builder import (
    BOOTC_IMAGE_BUILDER_IMAGE_DIGEST,
    BOOTC_IMAGE_BUILDER_IMAGE_TAG,
    UNIVERSAL_BLUE_BREW_IMAGE_DIGEST,
    UNIVERSAL_BLUE_BREW_IMAGE_TAG,
)
from maintenance_audit import (
    SNAPSHOT_DRIFT_FAILURE_COMMITS,
    SUBPROCESS_TIMEOUT_SECONDS,
    WRAPPER_ASSET,
    WRAPPER_RELEASE_REPO,
    WRAPPER_SOURCE,
    TemplateSource,
    audit_action_pin_freshness,
    audit_action_update_availability,
    audit_brew_image_pin,
    audit_container_trust_roots,
    audit_disk_builder_image_pin,
    audit_local_snapshot,
    audit_pin_table_shapes,
    audit_upstream_drift,
    audit_wrapper_release,
    describe_pin_drift,
    describe_snapshot_drift,
    fetch_registry_pull_token,
    fetch_sha256,
    github_api_json,
    github_repo_slug,
    is_newer_version_available,
    iter_pinned_downloads,
    iter_pinned_refs,
    load_template_source,
    main,
    parse_version_tag,
    query_github_comparison,
    query_github_ref_sha,
    query_latest_github_semver_tag,
    query_latest_release,
    query_remote_head,
    resolve_registry_tag_digest,
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


class FakeHeadResponse:
    """A HEAD response: headers and no body, the shape a manifest probe reads."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def __enter__(self) -> "FakeHeadResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


GHCR_CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:ublue-os/brew:pull"'


def unauthorized(challenge: str | None = GHCR_CHALLENGE) -> urllib.error.HTTPError:
    """The 401 a registry answers an anonymous manifest read with."""
    headers = {} if challenge is None else {"WWW-Authenticate": challenge}
    return urllib.error.HTTPError("https://ghcr.io/v2/ublue-os/brew/manifests/latest", 401, "Unauthorized", headers, None)


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

    def test_pin_tables_hold_only_commit_shas(self) -> None:
        # The shipped tables, checked directly rather than through a fixture:
        # these are the values that reach other people's repositories.
        self.assertEqual(audit_pin_table_shapes(), [])

    def test_a_tag_valued_pin_fails_the_audit_instead_of_matching_a_workflow(self) -> None:
        # The discriminating case. A table entry holding a tag is consistent
        # with a workflow that names the same tag, so the ref comparison below
        # passes it; without the shape check the audit reports nothing and the
        # floating ref ships.
        self.assertEqual(
            audit_pin_table_shapes(actions={"actions/checkout": ("v7", "v7")}, ref_pins={}),
            [
                "ACTION_PINS entry actions/checkout is pinned to 'v7' (labelled v7), "
                "which is not a 40-character commit SHA."
            ],
        )

    def test_a_branch_valued_ref_pin_fails_the_audit(self) -> None:
        # ACTION_REF_PINS is only read by the patching path, and most of its
        # targets appear in no workflow this audit reads -- so this check is
        # the only offline thing that would ever see a bad value in it.
        self.assertEqual(
            audit_pin_table_shapes(
                actions={},
                ref_pins={"osbuild/bootc-image-builder-action@main": ("main", "main")},
            ),
            [
                "ACTION_REF_PINS entry osbuild/bootc-image-builder-action@main is pinned to "
                "'main' (labelled main), which is not a 40-character commit SHA."
            ],
        )

    def test_a_short_sha_is_not_accepted_as_a_pin(self) -> None:
        # An abbreviated SHA is not immutable the way a full one is -- it is a
        # prefix, and Actions does not accept it -- so it fails here too.
        findings = audit_pin_table_shapes(actions={"actions/checkout": ("3d3c42e", "v7")}, ref_pins={})
        self.assertEqual(len(findings), 1)
        self.assertIn("not a 40-character commit SHA", findings[0])

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
                findings, advisories = audit_upstream_drift(source)

        self.assertEqual(findings, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("trails upstream HEAD", advisories[0])

    def test_audit_upstream_drift_fails_once_the_snapshot_is_badly_stale(self) -> None:
        # Past the threshold the drift is neglect rather than movement, and
        # every generated repo is shipping that much stale template.
        source = TemplateSource(
            repo="https://github.com/ublue-os/image-template.git",
            revision="a" * 40,
        )
        with patch("maintenance_audit.query_remote_head", return_value="b" * 40):
            with patch(
                "maintenance_audit.query_github_comparison",
                return_value=("ahead", SNAPSHOT_DRIFT_FAILURE_COMMITS, 0),
            ):
                findings, advisories = audit_upstream_drift(source)
        self.assertEqual(advisories, [])
        self.assertEqual(len(findings), 1)
        self.assertIn("stops being ordinary upstream movement", findings[0])

    def test_describe_snapshot_drift_states_how_far_behind(self) -> None:
        # The advisory has to be triageable without a checkout: three commits
        # behind is an ordinary week, eighty is a snapshot nobody has looked at.
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("ahead", 7, 0)) as compare:
            drift = describe_snapshot_drift(source, "b" * 40)
        compare.assert_called_once_with("ublue-os/image-template", "a" * 40, "b" * 40)
        self.assertIn("7 commit(s) newer", drift.message)
        self.assertIn("Refresh the snapshot", drift.message)
        self.assertFalse(drift.blocking)

    def test_describe_snapshot_drift_blocks_only_past_the_threshold(self) -> None:
        # The boundary is the whole point of the threshold, so pin both sides
        # of it. All four runs behind issue #129 sat at 1-4 commits.
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        for ahead, blocking in (
            (SNAPSHOT_DRIFT_FAILURE_COMMITS - 1, False),
            (SNAPSHOT_DRIFT_FAILURE_COMMITS, True),
        ):
            with self.subTest(ahead=ahead):
                with patch("maintenance_audit.query_github_comparison", return_value=("ahead", ahead, 0)):
                    drift = describe_snapshot_drift(source, "b" * 40)
                self.assertEqual(drift.blocking, blocking)

    def test_describe_snapshot_drift_refuses_to_recommend_a_rollback(self) -> None:
        # A force-pushed upstream branch can move HEAD backwards, and then
        # "refresh to HEAD" would roll the bundled snapshot back.
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("behind", 0, 9)):
            drift = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("9 commit(s) OLDER", drift.message)
        self.assertIn("Look before refreshing", drift.message)
        self.assertNotIn("Refresh the snapshot", drift.message)
        # Never blocking: failing the job here would demand a refresh that
        # would roll the snapshot backwards.
        self.assertFalse(drift.blocking)

    def test_describe_snapshot_drift_flags_diverged_history(self) -> None:
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", return_value=("diverged", 2, 5)):
            drift = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("diverged history", drift.message)
        self.assertFalse(drift.blocking)

    def test_describe_snapshot_drift_falls_back_when_compare_fails(self) -> None:
        source = TemplateSource(repo="https://github.com/ublue-os/image-template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison", side_effect=RuntimeError("rate limited")):
            drift = describe_snapshot_drift(source, "b" * 40)
        self.assertIn("trails upstream HEAD", drift.message)
        self.assertIn("Refresh the snapshot", drift.message)
        self.assertNotIn("commit(s)", drift.message)
        # Blocking on an unknown count is how a rate limit becomes a red audit.
        self.assertFalse(drift.blocking)

    def test_describe_snapshot_drift_skips_compare_for_a_non_github_remote(self) -> None:
        # The compare API is GitHub-only; a snapshot pinned to any other host
        # still has to produce a usable advisory rather than an exception.
        source = TemplateSource(repo="https://gitlab.com/example/template.git", revision="a" * 40)
        with patch("maintenance_audit.query_github_comparison") as compare:
            drift = describe_snapshot_drift(source, "b" * 40)
        compare.assert_not_called()
        self.assertIn("trails upstream HEAD", drift.message)
        self.assertFalse(drift.blocking)

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
            findings, advisories = audit_upstream_drift(source)
        self.assertEqual(findings, [])
        self.assertEqual(advisories, [])

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

    def test_query_remote_head_passes_a_timeout_and_converts_expiry(self) -> None:
        # A hung git process must not hold the weekly job open until GitHub's
        # own six-hour runner limit kills it. TimeoutExpired is neither an
        # OSError nor a CalledProcessError, so it needs its own conversion or
        # it escapes every caller's RuntimeError handling.
        with patch("maintenance_audit.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=120)
            with self.assertRaisesRegex(RuntimeError, "timed out after 120s"):
                query_remote_head("https://github.com/example/repo.git")
        self.assertEqual(run.call_args.kwargs["timeout"], SUBPROCESS_TIMEOUT_SECONDS)

    def test_audit_upstream_drift_reports_query_failure_as_advisory(self) -> None:
        # Being unable to check is not an inconsistency. An unreachable
        # upstream must not take the weekly job red.
        source = TemplateSource(
            repo="https://github.com/example/repo.git",
            revision="a" * 40,
        )
        with patch("maintenance_audit.query_remote_head", side_effect=RuntimeError("offline")):
            findings, advisories = audit_upstream_drift(source)
        self.assertEqual(findings, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("Unable to query upstream template HEAD", advisories[0])
        self.assertIn("offline", advisories[0])

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
            "maintenance_audit.audit_upstream_drift", return_value=([], ["drift advisory"])
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
            "maintenance_audit.audit_upstream_drift", return_value=([], ["snapshot trails upstream"])
        ):
            findings, advisories = run_audit(repo_root, skip_upstream=False)
        self.assertEqual(findings, [])
        self.assertIn("snapshot trails upstream", advisories)

    def test_run_audit_surfaces_badly_stale_snapshot_drift_as_a_failure(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch(
            "maintenance_audit.audit_upstream_drift", return_value=(["snapshot far behind"], [])
        ):
            findings, advisories = run_audit(repo_root, skip_upstream=False)
        self.assertIn("snapshot far behind", findings)
        self.assertEqual(advisories, [])

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
        # The same gate also runs the trust-root downloads, the two image
        # pin lookups and the release-asset comparison. Stub them: they read
        # the live registry and the live release, and this test went red the
        # day ghcr.io/ublue-os/brew:latest moved past its pin.
        with patch("maintenance_audit.audit_container_trust_roots", return_value=[]), patch(
            "maintenance_audit.audit_brew_image_pin", return_value=[]
        ), patch("maintenance_audit.audit_disk_builder_image_pin", return_value=[]), patch(
            "maintenance_audit.audit_wrapper_release", return_value=([], [])
        ):
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


# A Containerfile shaped like the real one: two keys pinned by digest, one
# download with no digest recorded, and continuations to fold.
_PINNED_CONTAINERFILE = """\
FROM registry.fedoraproject.org/fedora:44

RUN dnf5 -y install curl && \\
    curl -fsSL -o /tmp/KEY-a https://example.invalid/a.key && \\
    curl -fsSL -o /tmp/KEY-b https://example.invalid/b.key && \\
    curl -fsSL -o /tmp/unpinned https://example.invalid/c.tar && \\
    printf '%s\\n' \\
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  /tmp/KEY-a" \\
      "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  /tmp/KEY-b" \\
      > /tmp/keys.sha256 && \\
    sha256sum -c /tmp/keys.sha256
"""


class ContainerTrustRootAuditTests(unittest.TestCase):
    """The image's key pins are checked against upstream once a week.

    A sha256 pin on a file its owner may rotate is a maintenance obligation as
    much as a control: the day the key changes, the build stops. These cover
    the check that reports it as an advisory first, while it is still someone
    noticing rather than a red nightly with a bare checksum mismatch.
    """

    def test_pinned_downloads_are_paired_by_destination(self) -> None:
        # By destination and not by order, because the mistake worth catching
        # is a copied block that kept the previous file's name -- which pairs
        # a real URL with a digest for something else.
        self.assertEqual(
            iter_pinned_downloads(_PINNED_CONTAINERFILE),
            [
                ("https://example.invalid/a.key", "/tmp/KEY-a", "a" * 64),
                ("https://example.invalid/b.key", "/tmp/KEY-b", "b" * 64),
            ],
        )

    def test_a_download_with_no_digest_is_not_reported_here(self) -> None:
        # Whether an unpinned download may exist is a question for the offline
        # guard in tests/test_workflow_dependencies.py. This audit only asks
        # whether what upstream serves still matches what was written down, and
        # it has nothing to compare an unpinned download against.
        destinations = [dest for _url, dest, _digest in iter_pinned_downloads(_PINNED_CONTAINERFILE)]
        self.assertNotIn("/tmp/unpinned", destinations)

    def test_fetch_sha256_digests_what_the_server_actually_sent(self) -> None:
        body = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
        with local_http_server(status=200, body=body) as url:
            self.assertEqual(fetch_sha256(url), hashlib.sha256(body).hexdigest())

    def test_fetch_sha256_reports_an_http_error_rather_than_raising_urllib(self) -> None:
        with local_http_server(status=404, body=b"nope") as url:
            with self.assertRaises(RuntimeError) as caught:
                fetch_sha256(url)
        self.assertIn("404", str(caught.exception))

    def test_fetch_sha256_reports_a_connection_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            fetch_sha256(closed_port_url())

    def test_a_pin_that_still_matches_upstream_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Containerfile").write_text(_PINNED_CONTAINERFILE)
            digests = {
                "https://example.invalid/a.key": "a" * 64,
                "https://example.invalid/b.key": "b" * 64,
            }
            with patch("maintenance_audit.fetch_sha256", side_effect=digests.__getitem__):
                self.assertEqual(audit_container_trust_roots(root), [])

    def test_a_rotated_key_is_reported_with_both_digests(self) -> None:
        # Both, because the advisory has to be actionable without a checkout:
        # what is pinned now, and what upstream serves instead.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Containerfile").write_text(_PINNED_CONTAINERFILE)
            with patch("maintenance_audit.fetch_sha256", return_value="c" * 64):
                advisories = audit_container_trust_roots(root)

        self.assertEqual(len(advisories), 2)
        self.assertIn("https://example.invalid/a.key", advisories[0])
        self.assertIn("/tmp/KEY-a", advisories[0])
        self.assertIn("a" * 64, advisories[0])
        self.assertIn("c" * 64, advisories[0])
        # It must not read as a version bump to apply. Confirming the
        # fingerprint is the whole point of pinning the key in the first place.
        self.assertIn("fingerprint", advisories[0])

    def test_an_upstream_that_cannot_be_reached_is_said_so_not_passed(self) -> None:
        # Silence on an unreachable host would read exactly like a pin that
        # still matches, which is the one thing this must never do.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Containerfile").write_text(_PINNED_CONTAINERFILE)
            with patch("maintenance_audit.fetch_sha256", side_effect=RuntimeError("timed out")):
                advisories = audit_container_trust_roots(root)

        self.assertEqual(len(advisories), 2)
        for advisory in advisories:
            self.assertIn("Unable to check", advisory)

    def test_a_missing_containerfile_is_reported_rather_than_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            advisories = audit_container_trust_roots(Path(tmp))

        self.assertEqual(len(advisories), 1)
        self.assertIn("Unable to read Containerfile", advisories[0])

    def test_this_repositorys_own_containerfile_is_parsed_by_the_audit(self) -> None:
        # The fixture above proves the parser works on a file shaped like the
        # real one. This proves the real one is that shape -- otherwise the
        # weekly check runs, finds nothing to look at, and reports success.
        found = iter_pinned_downloads((Path(__file__).resolve().parents[1] / "Containerfile").read_text())
        self.assertEqual(len(found), 3, "expected two signing keys and the cosign RPM")
        for _url, _dest, digest in found:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_weekly_run_is_the_one_that_checks_the_pins(self) -> None:
        # Offline callers (--skip-upstream, from nightly-compliance and
        # ai-fix) must not reach for the network; the weekly audit must.
        with patch("maintenance_audit.audit_container_trust_roots", return_value=["x"]) as checked:
            with patch("maintenance_audit.audit_action_update_availability", return_value=[]):
                with patch("maintenance_audit.audit_action_pin_freshness", return_value=[]):
                    with patch("maintenance_audit.audit_brew_image_pin", return_value=[]), patch(
                        "maintenance_audit.audit_disk_builder_image_pin", return_value=[]
                    ), patch("maintenance_audit.audit_wrapper_release", return_value=([], [])):
                        repo_root = Path(__file__).resolve().parents[1]
                        run_audit(repo_root, skip_upstream=True, check_action_updates=False)
                        self.assertEqual(checked.call_count, 0)

                        _findings, advisories = run_audit(
                            repo_root, skip_upstream=True, check_action_updates=True
                        )
                        self.assertEqual(checked.call_count, 1)
                        self.assertIn("x", advisories)


class BrewPayloadPinAuditTests(unittest.TestCase):
    """The Homebrew payload pin is checked against its tag once a week.

    The payload is copied whole into `/` of every generated image that enables
    Homebrew, so it is pinned by digest rather than by tag. The obligation that
    comes with the pin is the same one the key pins carry: upstream publishes a
    new payload and the pinned copy ages, with nothing to say so.
    """

    def test_a_tag_that_still_resolves_to_the_pin_says_nothing(self) -> None:
        with patch(
            "maintenance_audit.resolve_registry_tag_digest",
            return_value=UNIVERSAL_BLUE_BREW_IMAGE_DIGEST,
        ):
            self.assertEqual(audit_brew_image_pin(), [])

    def test_a_moved_tag_is_reported_with_both_digests(self) -> None:
        moved = "sha256:" + "e" * 64
        with patch("maintenance_audit.resolve_registry_tag_digest", return_value=moved):
            (advisory,) = audit_brew_image_pin()
        self.assertIn(UNIVERSAL_BLUE_BREW_IMAGE_DIGEST, advisory)
        self.assertIn(moved, advisory)
        # Naming the two payload modules is the point of the advisory: a new
        # payload is a review of what it ships, not a digest to copy across.
        self.assertIn("tests/test_brew_login_fragments.py", advisory)
        self.assertIn("tests/test_brew_setup_staging.py", advisory)

    def test_a_registry_that_cannot_be_reached_is_said_so_not_passed(self) -> None:
        with patch(
            "maintenance_audit.resolve_registry_tag_digest",
            side_effect=RuntimeError("timed out"),
        ):
            (advisory,) = audit_brew_image_pin()
        self.assertIn("Unable to resolve", advisory)
        self.assertIn("timed out", advisory)

    def test_the_registry_host_is_split_off_the_image_reference(self) -> None:
        # ghcr.io/ublue-os/brew is one string in the tool; the registry API
        # needs the host and the repository path apart, and splitting on the
        # wrong slash asks ghcr.io for "brew" in nobody's namespace.
        with patch(
            "maintenance_audit.resolve_registry_tag_digest",
            return_value=UNIVERSAL_BLUE_BREW_IMAGE_DIGEST,
        ) as resolve:
            audit_brew_image_pin()
        resolve.assert_called_once_with("ghcr.io", "ublue-os/brew", UNIVERSAL_BLUE_BREW_IMAGE_TAG)

    def test_resolve_registry_tag_digest_returns_the_header_the_registry_sent(self) -> None:
        # ghcr.io's shape: the anonymous read is refused with a challenge, the
        # token comes from the realm it names, and the retry carries it.
        digest = "sha256:" + "1" * 64
        responses = [unauthorized(), FakeResponse({"token": "t"}), FakeHeadResponse({"Docker-Content-Digest": digest})]
        with patch("urllib.request.urlopen", side_effect=responses):
            self.assertEqual(resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest"), digest)

    def test_a_registry_that_answers_anonymously_is_not_asked_for_a_token(self) -> None:
        # quay.io's shape: a public repository reads without a token, and its
        # token endpoint is not where ghcr.io keeps its own. One request, no
        # guess at an endpoint.
        digest = "sha256:" + "3" * 64
        with patch("urllib.request.urlopen", return_value=FakeHeadResponse({"Docker-Content-Digest": digest})) as opened:
            self.assertEqual(
                resolve_registry_tag_digest("quay.io", "centos-bootc/bootc-image-builder", "latest"), digest
            )
        self.assertEqual(opened.call_count, 1)
        self.assertNotIn("Authorization", opened.call_args[0][0].headers)

    def test_the_manifest_is_requested_as_a_head_that_accepts_an_index(self) -> None:
        # Both halves matter. A GET would download the manifest to read a
        # header; and a request that does not accept an image index is
        # answered with a converted single-platform manifest, whose digest is
        # not what `COPY --from=` resolves -- so the check would report drift
        # every week and the pin would be "refreshed" onto the wrong value.
        requests = []

        def record(request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            requests.append(request)
            if len(requests) == 1:
                raise unauthorized()
            if len(requests) == 2:
                return FakeResponse({"token": "t"})
            return FakeHeadResponse({"Docker-Content-Digest": "sha256:" + "2" * 64})

        with patch("urllib.request.urlopen", side_effect=record):
            resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")
        first, token_request, retry = requests
        for manifest_request in (first, retry):
            self.assertEqual(manifest_request.get_method(), "HEAD")
            self.assertIn("application/vnd.oci.image.index.v1+json", manifest_request.headers["Accept"])
        # The token endpoint, service and scope all come from the challenge,
        # not from anything this module assumed about the registry.
        self.assertEqual(
            token_request.full_url, "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aublue-os%2Fbrew%3Apull"
        )
        self.assertEqual(retry.headers["Authorization"], "Bearer t")

    def test_fetch_registry_pull_token_accepts_the_other_spelling_of_the_answer(self) -> None:
        # The distribution spec allows `access_token` as well as `token`.
        with patch("urllib.request.urlopen", return_value=FakeResponse({"access_token": "a"})):
            self.assertEqual(fetch_registry_pull_token(GHCR_CHALLENGE, "ghcr.io", "ublue-os/brew"), "a")

    def test_fetch_registry_pull_token_sends_only_the_parameters_the_flow_defines(self) -> None:
        # A challenge can carry `error="..."` and other parameters; only the
        # realm is the endpoint and only service and scope go back to it.
        challenge = 'Bearer realm="https://r.example/auth",scope="repository:x:pull",error="insufficient_scope"'
        with patch("urllib.request.urlopen", return_value=FakeResponse({"token": "t"})) as opened:
            fetch_registry_pull_token(challenge, "r.example", "x")
        self.assertEqual(opened.call_args[0][0].full_url, "https://r.example/auth?scope=repository%3Ax%3Apull")
        with patch("urllib.request.urlopen", return_value=FakeResponse({"token": "t"})) as opened:
            fetch_registry_pull_token('Bearer realm="https://r.example/auth"', "r.example", "x")
        self.assertEqual(opened.call_args[0][0].full_url, "https://r.example/auth")

    def test_a_refusal_with_no_token_endpoint_is_an_error_not_a_guess(self) -> None:
        for challenge in (None, "", "Basic realm=\"x\"", 'Bearer service="ghcr.io"'):
            with self.subTest(challenge=challenge):
                with patch("urllib.request.urlopen", side_effect=unauthorized(challenge)):
                    with self.assertRaisesRegex(RuntimeError, "offered no token endpoint"):
                        resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_rejects_a_response_without_the_header(self) -> None:
        with patch("urllib.request.urlopen", return_value=FakeHeadResponse({})):
            with self.assertRaisesRegex(RuntimeError, "no Docker-Content-Digest"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_rejects_a_token_response_with_no_token(self) -> None:
        for payload in ({"errors": []}, ["not", "an", "object"], {"token": ""}):
            with self.subTest(payload=payload):
                with patch("urllib.request.urlopen", side_effect=[unauthorized(), FakeResponse(payload)]):
                    with self.assertRaisesRegex(RuntimeError, "issued no pull token"):
                        resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_reports_a_token_http_error(self) -> None:
        error = urllib.error.HTTPError("https://ghcr.io/token", 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=[unauthorized(), error]):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403 requesting a pull token"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_reports_a_token_that_is_not_json(self) -> None:
        with patch("urllib.request.urlopen", side_effect=[unauthorized(), FakeResponse(None, raw=b"<html>")]):
            with self.assertRaisesRegex(RuntimeError, "Expecting value"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_reports_a_manifest_http_error(self) -> None:
        error = urllib.error.HTTPError("https://ghcr.io/v2/x/manifests/latest", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_a_token_the_registry_then_rejects_is_reported_not_retried(self) -> None:
        # A second 401 is not another invitation; looping on it would be the
        # failure mode of a registry that issues tokens it does not honour.
        with patch("urllib.request.urlopen", side_effect=[unauthorized(), FakeResponse({"token": "t"}), unauthorized()]):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_resolve_registry_tag_digest_reports_a_connection_failure(self) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_a_connection_that_drops_after_the_token_is_reported_too(self) -> None:
        # The retry has its own error handling, and a token that was issued
        # before the connection failed would otherwise leave the failure to
        # escape as a urllib exception rather than the RuntimeError the caller
        # turns into an advisory.
        with patch(
            "urllib.request.urlopen",
            side_effect=[unauthorized(), FakeResponse({"token": "t"}), urllib.error.URLError("reset by peer")],
        ):
            with self.assertRaisesRegex(RuntimeError, "reset by peer"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_a_connection_that_drops_requesting_the_token_is_reported_too(self) -> None:
        with patch("urllib.request.urlopen", side_effect=[unauthorized(), urllib.error.URLError("no route")]):
            with self.assertRaisesRegex(RuntimeError, "no route"):
                resolve_registry_tag_digest("ghcr.io", "ublue-os/brew", "latest")

    def test_the_weekly_run_is_the_one_that_checks_the_payload_pin(self) -> None:
        with patch("maintenance_audit.audit_brew_image_pin", return_value=["moved"]) as checked:
            with patch("maintenance_audit.audit_disk_builder_image_pin", return_value=[]), patch(
                "maintenance_audit.audit_wrapper_release", return_value=([], [])
            ):
                with patch("maintenance_audit.audit_container_trust_roots", return_value=[]):
                    with patch("maintenance_audit.audit_action_update_availability", return_value=[]):
                        with patch("maintenance_audit.audit_action_pin_freshness", return_value=[]):
                            repo_root = Path(__file__).resolve().parents[1]
                            run_audit(repo_root, skip_upstream=True, check_action_updates=False)
                            self.assertEqual(checked.call_count, 0)

                            _findings, advisories = run_audit(
                                repo_root, skip_upstream=True, check_action_updates=True
                            )
                            self.assertEqual(checked.call_count, 1)
                            self.assertIn("moved", advisories)


class DiskBuilderPinAuditTests(unittest.TestCase):
    """The bootc-image-builder pin is checked against its tag once a week.

    The builder produces every disk artifact a generated repository publishes,
    so it is pinned like the payload is. What its pin ages into is different:
    not an old Homebrew but a builder behind the bootc in the base images, so
    the advisory has to say that a re-pin is a compatibility check.
    """

    def test_a_tag_that_still_resolves_to_the_pin_says_nothing(self) -> None:
        with patch(
            "maintenance_audit.resolve_registry_tag_digest",
            return_value=BOOTC_IMAGE_BUILDER_IMAGE_DIGEST,
        ):
            self.assertEqual(audit_disk_builder_image_pin(), [])

    def test_a_moved_tag_is_reported_with_both_digests_and_the_check_to_make(self) -> None:
        moved = "sha256:" + "f" * 64
        with patch("maintenance_audit.resolve_registry_tag_digest", return_value=moved):
            (advisory,) = audit_disk_builder_image_pin()
        self.assertIn("BOOTC_IMAGE_BUILDER_IMAGE_DIGEST", advisory)
        self.assertIn(BOOTC_IMAGE_BUILDER_IMAGE_DIGEST, advisory)
        self.assertIn(moved, advisory)
        self.assertIn("base images", advisory)

    def test_a_registry_that_cannot_be_reached_is_said_so_not_passed(self) -> None:
        with patch("maintenance_audit.resolve_registry_tag_digest", side_effect=RuntimeError("timed out")):
            (advisory,) = audit_disk_builder_image_pin()
        self.assertIn("Unable to resolve quay.io/centos-bootc/bootc-image-builder:latest", advisory)
        self.assertIn("timed out", advisory)

    def test_the_registry_host_is_split_off_the_image_reference(self) -> None:
        with patch(
            "maintenance_audit.resolve_registry_tag_digest",
            return_value=BOOTC_IMAGE_BUILDER_IMAGE_DIGEST,
        ) as resolve:
            audit_disk_builder_image_pin()
        resolve.assert_called_once_with("quay.io", "centos-bootc/bootc-image-builder", BOOTC_IMAGE_BUILDER_IMAGE_TAG)

    def test_the_weekly_run_is_the_one_that_checks_the_builder_pin(self) -> None:
        with patch("maintenance_audit.audit_disk_builder_image_pin", return_value=["moved"]) as checked:
            with patch("maintenance_audit.audit_brew_image_pin", return_value=[]), patch(
                "maintenance_audit.audit_wrapper_release", return_value=([], [])
            ):
                with patch("maintenance_audit.audit_container_trust_roots", return_value=[]):
                    with patch("maintenance_audit.audit_action_update_availability", return_value=[]):
                        with patch("maintenance_audit.audit_action_pin_freshness", return_value=[]):
                            repo_root = Path(__file__).resolve().parents[1]
                            run_audit(repo_root, skip_upstream=True, check_action_updates=False)
                            self.assertEqual(checked.call_count, 0)

                            _findings, advisories = run_audit(
                                repo_root, skip_upstream=True, check_action_updates=True
                            )
                            self.assertEqual(checked.call_count, 1)
                            self.assertIn("moved", advisories)


class WrapperReleaseAuditTests(unittest.TestCase):
    """The wrapper the latest release serves is compared to contrib/aib weekly.

    The recommended install fetches `aib` from the release and checks it
    against the checksum published beside it, which proves the download is
    what the release carries and nothing about whether the release carries
    what `main` describes. For weeks it did not (#356): the docs described a
    wrapper that verifies the image and withholds the token, and every user
    following them installed the v0.9.5 one, which does neither. These cover
    the check that reports that, and the bucket it reports it in.
    """

    def _checkout(self, tmp: str, wrapper: bytes) -> Path:
        root = Path(tmp)
        (root / WRAPPER_SOURCE).parent.mkdir(parents=True)
        (root / WRAPPER_SOURCE).write_bytes(wrapper)
        return root

    def _release(self, tag: str = "v0.9.5", *, assets: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
        if assets is None:
            assets = {WRAPPER_ASSET: f"https://github.com/{WRAPPER_RELEASE_REPO}/releases/download/{tag}/aib"}
        return tag, assets

    def test_a_release_that_carries_this_checkouts_wrapper_says_nothing(self) -> None:
        wrapper = b"#!/usr/bin/env bash\ncosign verify\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, wrapper)
            with patch("maintenance_audit.query_latest_release", return_value=self._release()):
                with patch("maintenance_audit.fetch_sha256", return_value=hashlib.sha256(wrapper).hexdigest()):
                    self.assertEqual(audit_wrapper_release(root), ([], []))

    def test_a_release_serving_an_older_wrapper_is_a_failure_carrying_both_digests(self) -> None:
        # A failure, not an advisory: nothing outside the repository moved.
        # This is its own release trailing its own main, every new install
        # is affected for as long as it lasts, and only a release cut here
        # clears it. Both digests and the tag, so it can be acted on
        # without a checkout -- and the action is named.
        wrapper = b"#!/usr/bin/env bash\ncosign verify\n"
        released = "b9e97913e1be620c85f75e1c599e8e4fd67ab4e6118af3d6a487515a4fa47cc8"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, wrapper)
            with patch("maintenance_audit.query_latest_release", return_value=self._release("v0.9.5")):
                with patch("maintenance_audit.fetch_sha256", return_value=released):
                    findings, advisories = audit_wrapper_release(root)

        self.assertEqual(advisories, [])
        (finding,) = findings
        self.assertIn("v0.9.5", finding)
        self.assertIn(released, finding)
        self.assertIn(hashlib.sha256(wrapper).hexdigest(), finding)
        self.assertIn(str(WRAPPER_SOURCE), finding)
        self.assertIn("Cutting a release", finding)

    def test_the_digest_compared_is_of_the_asset_the_docs_download(self) -> None:
        # The asset's own URL, not a guess at it: what is hashed has to be
        # the bytes `curl -fsSLO .../releases/latest/download/aib` lands.
        url = f"https://github.com/{WRAPPER_RELEASE_REPO}/releases/download/v0.9.5/aib"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, b"x")
            with patch("maintenance_audit.query_latest_release", return_value=self._release(assets={WRAPPER_ASSET: url})):
                with patch("maintenance_audit.fetch_sha256", return_value=hashlib.sha256(b"x").hexdigest()) as fetched:
                    audit_wrapper_release(root)
        fetched.assert_called_once_with(url)

    def test_a_release_with_no_wrapper_asset_is_a_failure_naming_the_dispatch(self) -> None:
        # The docs fetch the asset by name, so a release without it is a
        # recommended install that 404s. The fix is publish-wrapper.yml's
        # dispatch fallback, not a new release, and the finding says which.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, b"x")
            with patch(
                "maintenance_audit.query_latest_release",
                return_value=self._release("v0.9.6", assets={"aib.sha256": "https://example.invalid/aib.sha256"}),
            ):
                with patch("maintenance_audit.fetch_sha256") as fetched:
                    findings, advisories = audit_wrapper_release(root)
        fetched.assert_not_called()
        self.assertEqual(advisories, [])
        (finding,) = findings
        self.assertIn("v0.9.6", finding)
        self.assertIn("no `aib` asset", finding)
        self.assertIn("gh workflow run publish-wrapper.yml -f tag=v0.9.6", finding)

    def test_an_api_that_cannot_be_reached_is_an_advisory_not_a_pass_or_a_failure(self) -> None:
        # Silence would read like a release that matches; a failure would be
        # blocking on an unknown, which is how a rate limit becomes a red
        # weekly audit. Same bucket as every other unreachable upstream here.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, b"x")
            with patch("maintenance_audit.query_latest_release", side_effect=RuntimeError("API rate limit exceeded")):
                findings, advisories = audit_wrapper_release(root)
        self.assertEqual(findings, [])
        (advisory,) = advisories
        self.assertIn("Unable to query the latest release", advisory)
        self.assertIn(WRAPPER_RELEASE_REPO, advisory)
        self.assertIn("API rate limit exceeded", advisory)

    def test_an_asset_that_cannot_be_downloaded_is_an_advisory(self) -> None:
        url = f"https://github.com/{WRAPPER_RELEASE_REPO}/releases/download/v0.9.5/aib"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp, b"x")
            with patch("maintenance_audit.query_latest_release", return_value=self._release(assets={WRAPPER_ASSET: url})):
                with patch("maintenance_audit.fetch_sha256", side_effect=RuntimeError("HTTP 503")):
                    findings, advisories = audit_wrapper_release(root)
        self.assertEqual(findings, [])
        (advisory,) = advisories
        self.assertIn(f"Unable to download {url}", advisory)
        self.assertIn("HTTP 503", advisory)

    def test_a_checkout_without_the_wrapper_is_said_so_before_any_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("maintenance_audit.query_latest_release") as queried:
                findings, advisories = audit_wrapper_release(Path(tmp))
        queried.assert_not_called()
        self.assertEqual(findings, [])
        (advisory,) = advisories
        self.assertIn(f"Unable to read {WRAPPER_SOURCE}", advisory)

    def test_query_latest_release_extracts_the_tag_and_the_asset_urls(self) -> None:
        payload = {
            "tag_name": "v0.9.5",
            "assets": [
                {"name": "aib", "browser_download_url": "https://example.invalid/aib"},
                {"name": "aib.sha256", "browser_download_url": "https://example.invalid/aib.sha256"},
                "not-a-dict",
                {"name": 7, "browser_download_url": "https://example.invalid/seven"},
            ],
        }
        with patch("maintenance_audit.github_api_json", return_value=payload) as api:
            tag, assets = query_latest_release("Danathar/atomic-image-builder")
        api.assert_called_once_with("https://api.github.com/repos/Danathar/atomic-image-builder/releases/latest")
        self.assertEqual(tag, "v0.9.5")
        self.assertEqual(
            assets,
            {"aib": "https://example.invalid/aib", "aib.sha256": "https://example.invalid/aib.sha256"},
        )

    def test_query_latest_release_rejects_payloads_without_a_tag_or_assets(self) -> None:
        payloads = (["not", "a", "dict"], {"assets": []}, {"tag_name": "", "assets": []}, {"tag_name": "v1", "assets": "x"})
        for payload in payloads:
            with self.subTest(payload=payload):
                with patch("maintenance_audit.github_api_json", return_value=payload):
                    with self.assertRaisesRegex(RuntimeError, "Unexpected release payload"):
                        query_latest_release("Danathar/atomic-image-builder")

    def test_the_release_checked_is_the_one_the_install_docs_download_from(self) -> None:
        # The constants are only right while they name what the docs fetch.
        # A moved install URL, or a renamed asset, must fail here rather than
        # leave the audit comparing against a release nobody installs from.
        url = f"https://github.com/{WRAPPER_RELEASE_REPO}/releases/latest/download/{WRAPPER_ASSET}"
        root = Path(__file__).resolve().parents[1]
        for doc in ("README.md", "docs/installing.md"):
            with self.subTest(doc=doc):
                self.assertIn(url, (root / doc).read_text())
        self.assertTrue((root / WRAPPER_SOURCE).is_file())

    def test_the_weekly_run_is_the_one_that_checks_the_release(self) -> None:
        # And its failures land in the failure bucket: run_audit() must not
        # flatten the pair into advisories the way the other network checks
        # are collected.
        with patch("maintenance_audit.audit_wrapper_release", return_value=(["trailing"], ["unsure"])) as checked:
            with patch("maintenance_audit.audit_disk_builder_image_pin", return_value=[]):
                with patch("maintenance_audit.audit_brew_image_pin", return_value=[]):
                    with patch("maintenance_audit.audit_container_trust_roots", return_value=[]):
                        with patch("maintenance_audit.audit_action_update_availability", return_value=[]):
                            with patch("maintenance_audit.audit_action_pin_freshness", return_value=[]):
                                repo_root = Path(__file__).resolve().parents[1]
                                run_audit(repo_root, skip_upstream=True, check_action_updates=False)
                                self.assertEqual(checked.call_count, 0)

                                findings, advisories = run_audit(
                                    repo_root, skip_upstream=True, check_action_updates=True
                                )
        checked.assert_called_once_with(repo_root)
        self.assertIn("trailing", findings)
        self.assertIn("unsure", advisories)

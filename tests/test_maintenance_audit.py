import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance_audit import (
    TemplateSource,
    audit_action_update_availability,
    audit_local_snapshot,
    audit_upstream_drift,
    is_newer_version_available,
    load_template_source,
    parse_version_tag,
)


class MaintenanceAuditTests(unittest.TestCase):
    def test_parse_version_tag_accepts_major_and_semver_forms(self) -> None:
        self.assertEqual(parse_version_tag("v6"), (6, 0, 0))
        self.assertEqual(parse_version_tag("v4.2.1"), (4, 2, 1))
        self.assertIsNone(parse_version_tag("main"))

    def test_is_newer_version_available_respects_current_label_precision(self) -> None:
        self.assertFalse(is_newer_version_available("v6", "v6.0.2"))
        self.assertTrue(is_newer_version_available("v8", "v9"))
        self.assertTrue(is_newer_version_available("v4.0.0", "v4.1.1"))

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

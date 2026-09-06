import contextlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import atomic_image_builder
from _block_yaml import parse as parse_block_yaml
from atomic_image_builder import (
    ACCENT_COLOR,
    ACTION_PINS,
    ACTION_REF_PINS,
    BASE_IMAGES,
    BLUEBUILD_RECIPE_SCHEMA,
    BLUEBUILD_TEMPLATE_DIR,
    COMMON_SERVICES,
    CONTAINERFILE_TEMPLATE_DIR,
    CONTROLS_COLOR,
    DEFAULT_GITHUB_BUILD_CRON,
    DEFAULT_REPO_NAME,
    FEDORA_ATOMIC_DEFAULT_TAG,
    FEDORA_ATOMIC_FALLBACK_TAG,
    MANAGED_REPO_HINT_BLUEBUILD,
    MANAGED_REPO_WARNING,
    METHOD_DISPLAY,
    PACKAGE_SEARCH_LIMIT,
    SCAN_CANCELLED,
    SCAN_OK,
    SCAN_UNAVAILABLE,
    SCAN_UNSUPPORTED_BASE,
    STATE_FILE,
    TOOL_NAME,
    TOOL_SLUG,
    UNIVERSAL_BLUE_BREW_IMAGE,
    VERSION,
    App,
    CommandError,
    Config,
    Gum,
    ScreenBack,
    config_from_state_payload,
    determine_fedora_atomic_default_tag,
    ensure_trailing_newline,
    ensure_workflow_job_env_entries,
    format_daily_rebuild_note,
    is_valid_repo_name,
    normalize_container_image_reference,
    patch_cosign_compatibility,
    patch_workflow_steps,
    pin_action_uses_line,
    read_os_release_fields,
    string_list,
)


class GumStub:
    """Shared test double for Gum — override only what you need per test."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.clear_calls = 0

    def header(self, *_args, **_kwargs) -> None:
        pass

    def clear(self) -> None:
        self.clear_calls += 1

    def hint(self, message: str = "", *_args, **_kwargs) -> None:
        self.messages.append(("hint", message))

    def instruction(self, *_args, **_kwargs) -> None:
        pass

    def controls(self, *_parts: str) -> None:
        pass

    def success(self, message: str) -> None:
        self.messages.append(("success", message))

    def warn(self, message: str) -> None:
        self.messages.append(("warn", message))

    def error(self, message: str) -> None:
        self.messages.append(("error", message))

    def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
        self.prompts.append(placeholder)

    def style(self, *lines: str, **_kwargs) -> str:
        return "\n".join(lines)

    def content_width(self, reserve: int = 0, **_kwargs) -> int:
        return 100 - reserve

    def confirm(self, _prompt: str, default: bool = False) -> bool:
        return default

    def ensure_available(self) -> None:
        pass

    def pager(self, _text: str) -> None:
        pass

    def table(self, *_args, **_kwargs) -> None:
        pass

    def table_widths(self, *_args, **_kwargs) -> str:
        return "20,40"

    def form_width(self, **_kwargs) -> int:
        return 80

    def spinner(self, _title: str, _command, *, cwd=None) -> None:
        pass


# Captured before setUp() patches the module attribute, so the tests that
# exercise the probe itself run the real implementation rather than the
# no-conflict stand-in every other test gets.
REAL_GHCR_PACKAGE_EXISTS = atomic_image_builder.ghcr_package_exists


MIRROR_START = "<!-- mirror:copilot-instructions start -->"
MIRROR_END = "<!-- mirror:copilot-instructions end -->"
# The paragraphs CLAUDE.md must carry, named by their opening phrase so the
# selection survives rewording of the body. These four are the ones whose
# consequences leave this repository: two ship to every generated repo, one
# orphans every repo the tool has created, and one is the defect most often
# reintroduced here.
MIRRORED_TRAPS = (
    "**Every `uses:` in a workflow must be covered by `ACTION_PINS` or",
    "**`template_snapshots/` is vendored.**",
    "**Never rename `TOOL_SLUG`.**",
    "**The coverage threshold is not a literal.**",
)


def _markdown_prose(text: str) -> set[str]:
    """Sentence-length prose lines from a Markdown document.

    Fenced code blocks are skipped. A skill that tells someone which commands
    to run has to contain those commands, and this repo already repeats its
    check commands across CONTRIBUTING.md, MAINTAINER.md and the PR template
    deliberately -- keeping the numbers in them consistent is
    test_coverage_gate_threshold_has_one_source_of_truth's job, not this one.
    Headings, list items and indented lines are skipped as too short or too
    incidental to be evidence of copying.
    """
    prose = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if len(stripped) > 60 and not line.startswith(("#", "-", " ", "`")):
            prose.add(stripped)
    return prose


class BuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        # The tool consults a couple of env vars the container distribution
        # sets (AIB_RPM_OSTREE_STATUS_FILE, AIB_DISABLE_LOCAL_BUILD). Keep the
        # tests that exercise the normal paths hermetic by ensuring ambient
        # values from the runner environment (e.g. running the suite inside
        # the container) cannot silently divert them.
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        os.environ.pop("AIB_RPM_OSTREE_STATUS_FILE", None)
        os.environ.pop("AIB_DISABLE_LOCAL_BUILD", None)
        self.addCleanup(patcher.stop)

        # do_build() probes GHCR before creating a repo. No unit test may reach
        # the network, so default the probe to "no conflict" for everyone; the
        # tests that care about the conflict patch it themselves.
        ghcr_patcher = patch("atomic_image_builder.ghcr_package_exists", return_value=False)
        ghcr_patcher.start()
        self.addCleanup(ghcr_patcher.stop)

    def make_app(self) -> App:
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="ghcr.io/ublue-os/bazzite:stable",
            base_image_name="Bazzite (KDE)",
            repo_name="test-image",
            image_desc="Test image",
            github_user="example",
        )
        return app

    def test_state_file_uses_current_tool_slug(self) -> None:
        self.assertEqual(STATE_FILE, f".{TOOL_SLUG}.json")

    def init_signing_repo(self, repo_dir: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
        (repo_dir / "cosign.pub").write_text("OLD PUBLIC KEY\n")
        subprocess.run(["git", "add", "cosign.pub"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "Initial key"], cwd=repo_dir, check=True)

    def test_config_from_state_payload_rejects_string_list_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "packages must be a list of strings"):
            config_from_state_payload({"packages": "tmux"})

    def test_config_from_state_payload_rejects_non_string_list_item(self) -> None:
        with self.assertRaisesRegex(ValueError, "packages must contain only strings"):
            config_from_state_payload({"packages": ["tmux", 42]})

    def test_format_daily_rebuild_note_formats_local_time(self) -> None:
        note = format_daily_rebuild_note(
            "05 10 * * *",
            now_utc=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
            local_tz=timezone(timedelta(hours=-4), name="EDT"),
        )
        self.assertEqual(
            note,
            "Scheduled rebuilds also run daily at about 6:05 AM EDT on this system (10:05 UTC).",
        )

    def test_format_daily_rebuild_note_formats_utc_when_local_timezone_is_utc(self) -> None:
        note = format_daily_rebuild_note(
            "05 10 * * *",
            now_utc=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
            local_tz=timezone.utc,
        )
        self.assertEqual(note, "Scheduled rebuilds also run daily at about 10:05 AM UTC.")

    def test_format_daily_rebuild_note_falls_back_for_non_standard_field_count(self) -> None:
        note = format_daily_rebuild_note("05 10 * *")
        self.assertEqual(note, "Scheduled rebuilds also run automatically on GitHub.")

    def test_format_daily_rebuild_note_falls_back_for_non_daily_schedule(self) -> None:
        note = format_daily_rebuild_note("05 10 1 * *")
        self.assertEqual(
            note,
            "Scheduled rebuilds also run automatically on GitHub using the configured schedule (05 10 1 * * UTC).",
        )

    def test_format_daily_rebuild_note_falls_back_for_out_of_range_time(self) -> None:
        note = format_daily_rebuild_note("99 10 * * *")
        self.assertEqual(
            note,
            "Scheduled rebuilds also run automatically on GitHub using the configured schedule (99 10 * * * UTC).",
        )

    def test_is_valid_repo_name_accepts_a_normal_slug(self) -> None:
        self.assertTrue(is_valid_repo_name("my-custom-image"))

    def test_is_valid_repo_name_rejects_empty_and_overlong_values(self) -> None:
        self.assertFalse(is_valid_repo_name(""))
        self.assertFalse(is_valid_repo_name("a" * 101))

    def test_is_valid_repo_name_rejects_dot_git_suffix(self) -> None:
        self.assertFalse(is_valid_repo_name("my-image.git"))

    def test_is_valid_repo_name_rejects_disallowed_characters(self) -> None:
        self.assertFalse(is_valid_repo_name("My Image!"))

    def test_is_valid_repo_name_rejects_separator_runs_no_reference_can_parse(self) -> None:
        # GitHub accepts all three. ghcr.io/<owner>/<name> does not parse with
        # any of them, so the wizard would create the repo and the signing key
        # for an image nothing could push or pull. Verified against podman's
        # own reference parser, which reports "invalid reference format" for
        # each before it makes a request.
        self.assertFalse(is_valid_repo_name("test..image"))
        self.assertFalse(is_valid_repo_name("test.-image"))
        self.assertFalse(is_valid_repo_name("test___image"))
        self.assertFalse(is_valid_repo_name("test-.image"))
        self.assertFalse(is_valid_repo_name("test._image"))

    def test_is_valid_repo_name_keeps_the_separators_the_grammar_allows(self) -> None:
        # The controls that stop the rule above from being written as "one
        # separator character only": a double underscore and a run of hyphens
        # are both valid path components, and rejecting them would refuse
        # names people really use.
        self.assertTrue(is_valid_repo_name("test.image"))
        self.assertTrue(is_valid_repo_name("test_image"))
        self.assertTrue(is_valid_repo_name("test__image"))
        self.assertTrue(is_valid_repo_name("test--image"))
        self.assertTrue(is_valid_repo_name("test---image"))

    def test_repository_status_omits_description_separator_when_unset(self) -> None:
        app = self.make_app()
        app.config.image_desc = ""
        self.assertEqual(app.repository_status(), "test-image")

    def test_normalize_container_image_reference_handles_remote_registry_prefix(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ostree-remote-registry:fedora:quay.io/fedora-ostree-desktops/kinoite:43"),
            "quay.io/fedora-ostree-desktops/kinoite:43",
        )

    def test_normalize_container_image_reference_keeps_a_truncated_remote_prefix(self) -> None:
        # ostree-remote-registry:<remote>:<ref> needs all three fields. A ref
        # missing the third is malformed, and the tool must hand it back
        # untouched rather than index into a field that is not there. Both
        # prefixes share the one startswith() tuple, so both get a case here:
        # a future split of that tuple should not quietly lose the coverage
        # on whichever half moves.
        for malformed in ("ostree-remote-registry:fedora", "ostree-remote-image:fedora"):
            with self.subTest(reference=malformed):
                self.assertEqual(normalize_container_image_reference(malformed), malformed)

    def test_normalize_container_image_reference_handles_image_signed_docker_prefix(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ostree-image-signed:docker://ghcr.io/ublue-os/bazzite:stable"),
            "ghcr.io/ublue-os/bazzite:stable",
        )

    def test_normalize_container_image_reference_handles_unverified_registry_prefix(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ostree-unverified-registry:ghcr.io/ublue-os/bluefin:stable"),
            "ghcr.io/ublue-os/bluefin:stable",
        )

    def test_normalize_container_image_reference_handles_unverified_image_docker_prefix(self) -> None:
        # A host rebased with signature verification disabled reports this form.
        self.assertEqual(
            normalize_container_image_reference("ostree-unverified-image:docker://ghcr.io/ublue-os/aurora:stable"),
            "ghcr.io/ublue-os/aurora:stable",
        )

    def test_normalize_container_image_reference_handles_unverified_image_registry_transport(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ostree-unverified-image:registry:ghcr.io/ublue-os/aurora:stable"),
            "ghcr.io/ublue-os/aurora:stable",
        )

    def test_normalize_container_image_reference_preserves_digest_pins(self) -> None:
        digest = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        self.assertEqual(
            normalize_container_image_reference(f"ostree-unverified-image:docker://{digest}"),
            digest,
        )

    def test_normalize_container_image_reference_handles_docker_scheme(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("docker://ghcr.io/ublue-os/aurora:stable"),
            "ghcr.io/ublue-os/aurora:stable",
        )

    def test_normalize_container_image_reference_handles_remote_image_prefix(self) -> None:
        # ostree-remote-image uses the same split logic as ostree-remote-registry;
        # the inner docker:// prefix is then stripped by the docker:// handler.
        self.assertEqual(
            normalize_container_image_reference("ostree-remote-image:fedora:docker://quay.io/fedora-ostree-desktops/silverblue:43"),
            "quay.io/fedora-ostree-desktops/silverblue:43",
        )

    def test_normalize_container_image_reference_returns_bare_ref_unchanged(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ghcr.io/ublue-os/bazzite:stable"),
            "ghcr.io/ublue-os/bazzite:stable",
        )

    def test_normalize_container_image_reference_strips_whitespace(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("  ghcr.io/ublue-os/bazzite:stable  "),
            "ghcr.io/ublue-os/bazzite:stable",
        )

    def test_load_repo_config_rejects_repo_without_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "Containerfile").write_text("FROM ghcr.io/ublue-os/bazzite:stable\n")
            (repo_dir / "build_files").mkdir()
            (repo_dir / "build_files/build.sh").write_text("#!/bin/bash\n")
            with self.assertRaisesRegex(CommandError, "Only repos created by this tool are supported"):
                self.make_app().load_repo_config(repo_dir)

    def test_load_repo_config_wraps_state_file_read_errors(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / STATE_FILE).write_text("{}")
            with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
                with self.assertRaisesRegex(CommandError, "saved settings file"):
                    app.load_repo_config(repo_dir)

    def test_load_repo_config_wraps_state_payload_value_errors(self) -> None:
        # Valid JSON, but not an object: config_from_state_payload rejects it
        # with a ValueError, which load_repo_config must also wrap.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / STATE_FILE).write_text(json.dumps(["not", "an", "object"]))
            with self.assertRaisesRegex(CommandError, "saved settings file"):
                app.load_repo_config(repo_dir)

    def test_patch_container_workflow_pins_actions_and_ignores_state_file(self) -> None:
        app = self.make_app()
        app.config.signing_enabled = True
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              schedule:
                - cron: '00 00 * * *'
              push:
                paths-ignore:
                  - '**/README.md'
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
                  - name: Maximize build space
                    uses: ublue-os/remove-unwanted-software@v8
                  - name: Install Cosign
                    if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
                    uses: sigstore/cosign-installer@v3
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn(".atomic-image-builder.json", patched)
        self.assertIn(ACTION_PINS["actions/checkout"][0], patched)
        # The fixture uses @v8, which maps to the legacy v8 SHA via ACTION_REF_PINS
        self.assertIn(ACTION_REF_PINS["ublue-os/remove-unwanted-software@v8"][0], patched)
        self.assertIn(ACTION_PINS["sigstore/cosign-installer"][0], patched)

    def test_patch_container_workflow_matches_signing_steps_by_behavior(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            jobs:
              build_push:
                steps:
                  - name: Setup signer toolchain
                    if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
                    uses: sigstore/cosign-installer@v3
                  - name: Publish signature with custom label
                    run: cosign sign --yes ghcr.io/example/test-image:latest
            """
        )

        patched = app.patch_container_workflow(workflow)

        self.assertEqual(patched.count("env.COSIGN_PRIVATE_KEY != ''"), 2)
        self.assertIn(ACTION_PINS["sigstore/cosign-installer"][0], patched)

    def test_patch_container_workflow_injects_job_env_even_when_step_env_matches(self) -> None:
        """Step-level COSIGN_PRIVATE_KEY must not prevent job-level injection."""
        app = self.make_app()
        app.config.signing_enabled = True
        # This mirrors the template workflow: COSIGN_PRIVATE_KEY exists at the
        # step level but NOT at the job level.
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              push:
                paths-ignore:
                  - '**/README.md'
            jobs:
              build_push:
                env:
                  COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
                  - name: Install Cosign
                    uses: sigstore/cosign-installer@v3
                    if: github.event_name != 'pull_request' && env.COSIGN_PRIVATE_KEY != ''
                  - name: Sign container image
                    if: github.event_name != 'pull_request' && env.COSIGN_PRIVATE_KEY != ''
                    run: cosign sign -y --key env://COSIGN_PRIVATE_KEY ghcr.io/example/test:latest
                    env:
                      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}
            """
        )
        patched = app.patch_container_workflow(workflow)
        # The job-level env block must now contain COSIGN_PRIVATE_KEY
        job_env_lines = []
        in_job_env = False
        for line in patched.splitlines():
            if line == "    env:":
                in_job_env = True
                continue
            if in_job_env and line.startswith("      ") and ":" in line:
                job_env_lines.append(line.strip())
            elif in_job_env:
                break
        self.assertIn("COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}", job_env_lines)
        self.assertIn("COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}", job_env_lines)

    def test_patch_container_workflow_handles_inline_paths_ignore(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              push:
                paths-ignore: ['**/README.md']
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn("paths-ignore: ['**/README.md', '.atomic-image-builder.json']", patched)

    def test_patch_container_workflow_handles_empty_inline_paths_ignore(self) -> None:
        # An empty inline list must not produce "[, '<state file>']".
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              push:
                paths-ignore: []
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn(f"paths-ignore: ['{STATE_FILE}']", patched)
        self.assertNotIn("paths-ignore: []", patched)
        self.assertEqual(app.patch_container_workflow(patched), patched)

    def test_patch_container_workflow_anchors_state_ignore_to_readme_entry(self) -> None:
        # Fallback for a workflow whose paths-ignore key is written in a form
        # the key match does not see (YAML permits a quoted key). Without it
        # the state file would never be added and every state-only commit
        # would trigger a rebuild.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              push:
                "paths-ignore":
                  - '**/README.md'
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn(f"      - '{STATE_FILE}'", patched)
        # The anchor entry itself must survive, indented as it was.
        self.assertIn("      - '**/README.md'", patched)
        self.assertEqual(app.patch_container_workflow(patched), patched)

    def test_patch_container_workflow_rewrites_image_desc_env_key(self) -> None:
        # The bundled snapshot carries IMAGE_DESC in image-template.env, but a
        # workflow that declares it as a YAML env key must still be rewritten
        # to the configured description rather than keeping the placeholder.
        app = self.make_app()
        app.config.image_desc = 'Doug: my "daily" image'
        workflow = textwrap.dedent(
            """\
            name: Build container image
            env:
              IMAGE_DESC: My Customized Bootc Image
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertNotIn("My Customized Bootc Image", patched)
        # A description carrying a colon and quotes must be emitted as a
        # quoted, escaped scalar or the workflow stops parsing.
        self.assertIn('  IMAGE_DESC: "Doug: my \\"daily\\" image"', patched)
        self.assertEqual(app.patch_container_workflow(patched), patched)

    def test_patch_container_workflow_adds_state_ignore_only_once(self) -> None:
        # Both the key branch and the README anchor can match the same
        # workflow; only one entry may be inserted.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              push:
                paths-ignore:
                  - '**/README.md'
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertEqual(patched.count(f"- '{STATE_FILE}'"), 1)

    def test_patch_container_workflow_updates_branch_filters_for_default_branch(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              pull_request:
                branches:
                  - main
              push:
                branches:
                  - main
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
            """
        )
        patched = app.patch_container_workflow(workflow, default_branch="master")
        self.assertIn("  pull_request:\n    branches:\n      - master", patched)
        self.assertIn("  push:\n    branches:\n      - master", patched)

    def test_patch_container_workflow_updates_legacy_cosign_compatibility(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build:
                steps:
                  - name: Install Cosign
                    with:
                      cosign-release: 'v2.6.3'
                  - name: Sign
                    run: cosign sign -y --key env://COSIGN_PRIVATE_KEY image:latest
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn("cosign-release: 'v3.1.2'", patched)
        self.assertIn(
            "cosign sign --new-bundle-format=false --use-signing-config=false -y --key", patched
        )
        # The stale, unpatched form must be gone entirely.
        self.assertNotIn("cosign sign -y --key", patched)

    # ── patch_cosign_compatibility shapes ───────────────────────────────
    # This patcher is a migration path for managed repos generated before the
    # Cosign 3.x flags existed. A silent no-op here publishes a workflow whose
    # signing step cosign 3.x rejects, so every shape below asserts the
    # replacement is present, the stale form absent, and the result idempotent.

    def assert_cosign_patched(self, text: str) -> str:
        patched = patch_cosign_compatibility(text)
        self.assertIn("--new-bundle-format=false", patched)
        self.assertIn("--use-signing-config=false", patched)
        self.assertEqual(patch_cosign_compatibility(patched), patched, "not idempotent")
        return patched

    def test_patch_cosign_compatibility_bundled_snapshot_shape(self) -> None:
        snapshot = (
            CONTAINERFILE_TEMPLATE_DIR / ".github" / "workflows" / "build.yml"
        ).read_text()
        self.assert_cosign_patched(snapshot)

    def test_patch_cosign_compatibility_handles_line_continuations(self) -> None:
        text = (
            "        run: |\n"
            "          cosign sign -y \\\n"
            "            --key env://COSIGN_PRIVATE_KEY \\\n"
            "            ${IMAGE}\n"
        )
        patched = self.assert_cosign_patched(text)
        # The continuation structure must survive the rewrite.
        self.assertIn("--key env://COSIGN_PRIVATE_KEY \\", patched)
        self.assertEqual(len(patched.splitlines()), len(text.splitlines()))

    def test_patch_cosign_compatibility_handles_long_yes_flag(self) -> None:
        text = "          cosign sign --yes --key env://COSIGN_PRIVATE_KEY ${IMAGE}"
        patched = self.assert_cosign_patched(text)
        self.assertNotIn("cosign sign --yes --key", patched)

    def test_patch_cosign_compatibility_handles_absent_confirm_flag(self) -> None:
        text = "          cosign sign --key env://COSIGN_PRIVATE_KEY ${IMAGE}"
        self.assert_cosign_patched(text)

    def test_patch_cosign_compatibility_leaves_keyless_signing_alone(self) -> None:
        text = "          cosign sign -y ghcr.io/example/test-image:latest"
        self.assertEqual(patch_cosign_compatibility(text), text)

    def test_patch_cosign_compatibility_fails_closed_on_split_verb(self) -> None:
        # `cosign` and `sign` on separate physical lines: no single line can be
        # rewritten, and silently emitting the incompatible command is worse
        # than stopping with an explanation.
        text = (
            "          cosign \\\n"
            "            sign -y --key env://COSIGN_PRIVATE_KEY ${IMAGE}\n"
        )
        with self.assertRaisesRegex(CommandError, "split across line continuations"):
            patch_cosign_compatibility(text)

    def test_patch_container_workflow_patches_continuation_signing_end_to_end(self) -> None:
        # Exercise it through the real generated-repo path, not just the helper.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build:
                steps:
                  - name: Install Cosign
                    with:
                      cosign-release: 'v2.6.3'
                  - name: Sign
                    run: |
                      cosign sign -y \\
                        --key env://COSIGN_PRIVATE_KEY \\
                        image:latest
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn("cosign-release: 'v3.1.2'", patched)
        self.assertIn("--new-bundle-format=false", patched)
        self.assertNotIn("cosign sign -y \\", patched)

    def test_patch_container_workflow_golden(self) -> None:
        expected_path = Path(__file__).parent / "fixtures/workflows/container_expected.yml"
        input_path = Path(__file__).parent / "fixtures/workflows/container_input.yml"
        app = self.make_app()
        result = app.patch_container_workflow(input_path.read_text(), default_branch="main")
        self.assertEqual(result, expected_path.read_text())

    def test_patch_container_workflow_matches_current_upstream_snapshot_shape(self) -> None:
        # Coverage for the "rewrite in just" upstream refresh (image-template @
        # ac6ef404): the current build.yml has no job-level env: block and no
        # IMAGE_DESC: line (description now flows through
        # patch_image_template_env instead), so this exercises the from-scratch
        # env-block insertion and behavior-based signing-step detection against
        # the real, current bundled snapshot rather than a synthetic fixture.
        app = self.make_app()
        snapshot_path = CONTAINERFILE_TEMPLATE_DIR / ".github/workflows/build.yml"
        result = app.patch_container_workflow(snapshot_path.read_text(), default_branch="master")

        self.assertIn(f"- cron: '{DEFAULT_GITHUB_BUILD_CRON}'", result)
        self.assertIn(
            "    paths-ignore:\n      - '.atomic-image-builder.json'\n      - '**/README.md'\n",
            result,
        )
        self.assertIn("  pull_request:\n    branches:\n      - master", result)
        self.assertIn("  push:\n    branches:\n      - master", result)

        self.assertEqual(
            result.count("&& env.COSIGN_PRIVATE_KEY != ''"),
            2,
            "expected both the cosign-installer and cosign-sign steps to gain the guard",
        )
        self.assertIn(
            "    env:\n      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}\n"
            "      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}\n    steps:\n",
            result,
        )

        self.assertIn(f"actions/checkout@{ACTION_PINS['actions/checkout'][0]}", result)
        self.assertIn(f"docker/login-action@{ACTION_PINS['docker/login-action'][0]}", result)
        self.assertIn(f"sigstore/cosign-installer@{ACTION_PINS['sigstore/cosign-installer'][0]}", result)
        self.assertIn(f"extractions/setup-just@{ACTION_PINS['extractions/setup-just'][0]}", result)

        # No IMAGE_DESC: env line exists in this shape; the legacy line-rewrite
        # correctly finds nothing to match, since the description is instead
        # wired in via patch_image_template_env.
        self.assertNotIn("IMAGE_DESC:", result)

        # Chunkah is enabled by default over the rpm-ostree rechunker, and the
        # now-stale commented-out Chunkah alternative block is stripped.
        # Asserted on the recipe name rather than the full invocation so this
        # keeps testing something real across upstream's sudo/rootless spelling
        # changes: no ostree-rechunk call may survive anywhere in the output.
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("just rechunk", result)
        self.assertNotIn("- name: Rechunk with rpm-ostree", result)
        self.assertNotIn("ostree-rechunk", result)
        self.assertNotIn("#- name: Rechunk with Chunkah", result)
        self.assertNotIn("feeling adventurous", result)

    def test_patch_container_rechunk_step_is_idempotent(self) -> None:
        app = self.make_app()
        snapshot_text = (CONTAINERFILE_TEMPLATE_DIR / ".github/workflows/build.yml").read_text()
        once = app.patch_container_rechunk_step(snapshot_text)
        twice = app.patch_container_rechunk_step(once)
        self.assertEqual(once, twice)

    def test_patch_container_rechunk_step_no_ops_without_rechunk_step(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v7
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertEqual(result, ensure_trailing_newline(workflow))

    def test_patch_container_rechunk_step_strips_stale_comment_block(self) -> None:
        # Once the active step is switched to Chunkah, upstream's leftover
        # "if you are feeling adventurous" comment block (which itself
        # contains a now-redundant commented copy of the same step) should
        # be removed rather than left dangling below the active step, and
        # exactly one blank line should separate the rechunk step from the
        # next one.
        app = self.make_app()
        snapshot_text = (CONTAINERFILE_TEMPLATE_DIR / ".github/workflows/build.yml").read_text()
        result = app.patch_container_rechunk_step(snapshot_text)
        self.assertNotIn("feeling adventurous", result)
        self.assertNotIn("#- name: Rechunk with Chunkah", result)
        self.assertIn(
            '            ${DEFAULT_TAG}\n'
            '\n'
            '      - name: Generate Build Tags\n',
            result,
        )

    def test_patch_container_rechunk_step_no_ops_stale_comment_strip_on_unmatched_text(self) -> None:
        # If the active step is present but the trailing comment block's
        # text doesn't match upstream's exact current wording, the strip
        # silently no-ops rather than mangling unrelated content.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Rechunk with rpm-ostree
                    id: rechunk
                    run: |
                      sudo -E $(command -v just) ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}

                  # A completely different trailing comment.
                  - name: Generate Build Tags
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("A completely different trailing comment.", result)

    def test_patch_container_rechunk_step_handles_legacy_sudo_invocation(self) -> None:
        # Existing managed repositories still carry the pre-rootless upstream
        # shape (ublue-os/image-template before b9783f6), and they get patched
        # in place on update rather than replaced from the bundled snapshot.
        # Both spellings must keep working, or an update would rename the step
        # to Chunkah while leaving it running rpm-ostree.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Rechunk with rpm-ostree
                    id: rechunk
                    run: |
                      sudo -E $(command -v just) ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}

                  - name: Generate Build Tags
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("sudo -E $(command -v just) rechunk", result)
        self.assertNotIn("ostree-rechunk", result)

    def test_patch_container_rechunk_step_handles_rootless_invocation(self) -> None:
        # Upstream's rootless shape (b9783f6 onward) drops the sudo wrapper.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Rechunk with rpm-ostree
                    id: rechunk
                    run: |
                      just ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}

                  - name: Generate Build Tags
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("just rechunk", result)
        self.assertNotIn("ostree-rechunk", result)

    def test_patch_container_rechunk_step_leaves_other_steps_alone(self) -> None:
        # A managed repository may call ostree-rechunk from a step of its own.
        # Existing repositories are patched in place on update rather than
        # replaced from the snapshot, so rewriting a user's own step would
        # silently change a build they wrote deliberately.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Rechunk with rpm-ostree
                    id: rechunk
                    run: |
                      just ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}

                  - name: Compare against the classical rechunker
                    run: |
                      just ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}-classic
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("just rechunk \\\n            ${IMAGE_NAME}", result)
        # The user's own step keeps its recipe.
        self.assertIn("just ostree-rechunk \\\n            ${IMAGE_NAME} \\\n            ${DEFAULT_TAG}-classic", result)
        self.assertEqual(result.count("ostree-rechunk"), 1)

    def test_patch_container_rechunk_step_ignores_ostree_rechunk_outside_any_step(self) -> None:
        # No rechunk step at all means nothing in the file is ours to rewrite.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Build Image
                    run: |
                      just build \\
                        ${IMAGE_NAME}

                  - name: Custom rechunk
                    run: |
                      just ostree-rechunk \\
                        ${IMAGE_NAME}
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertEqual(result, ensure_trailing_newline(workflow))

    def test_patch_container_rechunk_step_repairs_half_switched_workflow(self) -> None:
        # A workflow renamed to Chunkah but still calling ostree-rechunk is what
        # a single-shape matcher would have produced. Updating must heal it.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            jobs:
              build_push:
                steps:
                  - name: Rechunk with Chunkah
                    id: rechunk
                    run: |
                      just ostree-rechunk \\
                        ${IMAGE_NAME} \\
                        ${DEFAULT_TAG}

                  - name: Generate Build Tags
            """
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertNotIn("ostree-rechunk", result)

    def test_patch_container_rechunk_step_strips_legacy_stale_comment_block(self) -> None:
        # The stale-comment strip is literal-matched, and upstream shipped the
        # commented alternative with the sudo wrapper before b9783f6. A repo
        # created from that template must still get the block removed.
        app = self.make_app()
        workflow = (
            "jobs:\n"
            "  build_push:\n"
            "    steps:\n"
            "      - name: Rechunk with rpm-ostree\n"
            "        id: rechunk\n"
            "        run: |\n"
            "          sudo -E $(command -v just) ostree-rechunk \\\n"
            "            ${IMAGE_NAME} \\\n"
            "            ${DEFAULT_TAG}\n"
            "\n"
            "      # If you are feeling adventurous, use the new distro agnostic rechunker\n"
            "      # https://github.com/coreos/chunkah\n"
            "      # You can delete the Rechunk with rpm-ostree portion then if you use this\n"
            "      #- name: Rechunk with Chunkah\n"
            "      #  id: rechunk\n"
            "      #  run: |\n"
            "      #    sudo -E $(command -v just) rechunk \\\n"
            "      #      ${IMAGE_NAME} \\\n"
            "      #      ${DEFAULT_TAG}\n"
            "\n"
            "      - name: Generate Build Tags\n"
        )
        result = app.patch_container_rechunk_step(workflow)
        self.assertNotIn("feeling adventurous", result)
        self.assertNotIn("#- name: Rechunk with Chunkah", result)
        self.assertIn(
            "            ${DEFAULT_TAG}\n"
            "\n"
            "      - name: Generate Build Tags\n",
            result,
        )

    def test_patch_workflow_branch_filters_adds_missing_branches_blocks(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              push:
                paths-ignore:
                  - '**.md'
              pull_request:
              workflow_dispatch:
            jobs:
              build:
                steps:
                  - run: true
            """
        )
        patched = app.patch_workflow_branch_filters(workflow, "master")
        self.assertIn("  push:\n    branches:\n      - master\n    paths-ignore:", patched)
        self.assertIn("  pull_request:\n    branches:\n      - master\n  workflow_dispatch:", patched)

    def test_patch_workflow_branch_filters_ends_block_at_commented_sibling(self) -> None:
        # The bundled BlueBuild snapshot ships
        #   workflow_dispatch: # allow manually triggering builds
        # which does not end in a colon and so was absorbed into the preceding
        # trigger's block.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              pull_request:
              workflow_dispatch: # allow manually triggering builds
            jobs:
              build:
                steps:
                  - run: true
            """
        )
        patched = app.patch_workflow_branch_filters(workflow, "master")
        self.assertIn(
            "  pull_request:\n    branches:\n      - master\n  workflow_dispatch: # allow",
            patched,
        )
        self.assertEqual(app.patch_workflow_branch_filters(patched, "master"), patched)

    def test_patch_workflow_branch_filters_does_not_skip_commented_push_trigger(self) -> None:
        # The real breakage: when the swallowed sibling owns a branches: key,
        # branches_found flipped on the wrong trigger, so pull_request never got
        # a filter and PR builds fired from every branch.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              pull_request:
              push: # only the default branch
                branches:
                  - main
            jobs:
              build:
                steps:
                  - run: true
            """
        )
        patched = app.patch_workflow_branch_filters(workflow, "release")
        self.assertIn("  pull_request:\n    branches:\n      - release", patched)
        self.assertIn("  push: # only the default branch\n    branches:\n      - release", patched)
        self.assertNotIn("- main", patched)
        self.assertEqual(patched.count("branches:"), 2)

    def test_patch_workflow_branch_filters_patches_a_trigger_that_ends_the_file(self) -> None:
        # The block scan normally stops at the next sibling trigger. When the
        # trigger it is patching is the last thing in the file there is no
        # sibling to stop at, so the scan has to end at the file boundary
        # instead of running past it.
        app = self.make_app()
        workflow = textwrap.dedent("""\
            name: build
            on:
              workflow_dispatch:
              push:
                paths:
                  - Containerfile
        """)
        result = app.patch_workflow_branch_filters(workflow, default_branch="develop")
        self.assertIn("  push:\n    branches:\n      - develop", result)
        self.assertIn("      - Containerfile", result)

    def test_patch_workflow_branch_filters_bundled_bluebuild_snapshot(self) -> None:
        app = self.make_bluebuild_app()
        snapshot = (
            BLUEBUILD_TEMPLATE_DIR / ".github" / "workflows" / "build.yml"
        ).read_text()
        patched = app.patch_workflow_branch_filters(snapshot, "main")
        # workflow_dispatch must stay a sibling trigger, not part of pull_request.
        self.assertIn("  pull_request:\n    branches:\n      - main", patched)
        self.assertIn("  workflow_dispatch: # allow manually triggering builds", patched)
        self.assertIn("  push:\n    branches:\n      - main\n    paths-ignore:", patched)
        self.assertEqual(app.patch_workflow_branch_filters(patched, "main"), patched)

    def test_patch_workflow_branch_filters_leaves_inline_trigger_mapping_untouched(self) -> None:
        # "push: { branches: [main] }" carries its filter inline. Appending a
        # nested branches: block beneath a valued key is a YAML parse error, so
        # the trigger must be left exactly as written - while its block-style
        # siblings still get patched.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              push: { branches: [main] }
              pull_request:
              workflow_dispatch:
            jobs:
              build:
                steps:
                  - run: true
            """
        )
        patched = app.patch_workflow_branch_filters(workflow, "master")
        self.assertIn("  push: { branches: [main] }\n  pull_request:", patched)
        self.assertIn("  pull_request:\n    branches:\n      - master\n  workflow_dispatch:", patched)
        self.assertEqual(patched.count("branches:"), 2)
        self.assertEqual(app.patch_workflow_branch_filters(patched, "master"), patched)

    def test_patch_workflow_branch_filters_rewrites_inline_branches_list(self) -> None:
        # An inline flow list under a block trigger - "branches: [main]" - took
        # the block path, and "- master" was nested beneath the already-valued
        # key: a parse error. It is rewritten to the block form, replacing the
        # existing entries with the default branch exactly as the block path
        # does.
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              push:
                branches: [main, dev]
              workflow_dispatch:
            jobs:
              build:
                steps:
                  - run: true
            """
        )
        patched = app.patch_workflow_branch_filters(workflow, "master")
        self.assertIn("  push:\n    branches:\n      - master\n  workflow_dispatch:", patched)
        self.assertNotIn("[main, dev]", patched)
        self.assertEqual(app.patch_workflow_branch_filters(patched, "master"), patched)

    def test_validate_config_rejects_unsafe_package_token(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "bad;rm"]
        with self.assertRaisesRegex(CommandError, "Invalid package value"):
            app.validate_config()

    def test_base_image_picker_includes_supported_universal_blue_and_fedora_atomic_images(self) -> None:
        self.assertEqual(
            [image.key for image in BASE_IMAGES],
            [
                "bazzite",
                "bazzite-gnome",
                "bazzite-dx",
                "bazzite-dx-gnome",
                "aurora",
                "aurora-dx",
                "bluefin",
                "bluefin-dx",
                "silverblue",
                "kinoite",
                "sway-atomic",
                "budgie-atomic",
                "cosmic-atomic",
            ],
        )

    def test_fedora_atomic_images_use_the_curated_stable_tag(self) -> None:
        image_map = {image.key: image.image_uri for image in BASE_IMAGES}
        self.assertEqual(image_map["silverblue"], f"quay.io/fedora-ostree-desktops/silverblue:{FEDORA_ATOMIC_DEFAULT_TAG}")
        self.assertEqual(image_map["kinoite"], f"quay.io/fedora-ostree-desktops/kinoite:{FEDORA_ATOMIC_DEFAULT_TAG}")

    def test_determine_fedora_atomic_default_tag_prefers_newer_fedora_host_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text('ID="fedora"\nVERSION_ID="44"\n')
            self.assertEqual(determine_fedora_atomic_default_tag(os_release_path=os_release), "44")

    def test_determine_fedora_atomic_default_tag_caps_one_release_ahead(self) -> None:
        # A pre-release host (Rawhide, or Branched well before GA) reports a
        # VERSION_ID whose fedora-ostree-desktops tag does not exist yet. The
        # repo would be created and pushed fine and the build would then fail on
        # FROM with manifest-unknown, which the user cannot diagnose.
        ahead = str(int(FEDORA_ATOMIC_FALLBACK_TAG) + 1)
        way_ahead = str(int(FEDORA_ATOMIC_FALLBACK_TAG) + 5)
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text(f"ID=fedora\nVERSION_ID={ahead}\n")
            self.assertEqual(determine_fedora_atomic_default_tag(os_release_path=os_release), ahead)
            os_release.write_text(f"ID=fedora\nVERSION_ID={way_ahead}\n")
            self.assertEqual(determine_fedora_atomic_default_tag(os_release_path=os_release), ahead)

    def test_determine_fedora_atomic_default_tag_ignores_older_fedora_host(self) -> None:
        older = str(int(FEDORA_ATOMIC_FALLBACK_TAG) - 1)
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text(f"ID=fedora\nVERSION_ID={older}\n")
            self.assertEqual(
                determine_fedora_atomic_default_tag(os_release_path=os_release),
                FEDORA_ATOMIC_FALLBACK_TAG,
            )

    def test_determine_fedora_atomic_default_tag_ignores_non_numeric_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text("ID=fedora\nVERSION_ID=rawhide\n")
            self.assertEqual(
                determine_fedora_atomic_default_tag(os_release_path=os_release),
                FEDORA_ATOMIC_FALLBACK_TAG,
            )

    def test_determine_fedora_atomic_default_tag_keeps_fallback_for_non_fedora_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text('ID="ubuntu"\nVERSION_ID="24.04"\n')
            self.assertEqual(determine_fedora_atomic_default_tag(os_release_path=os_release), FEDORA_ATOMIC_FALLBACK_TAG)

    def test_read_os_release_fields_returns_empty_dict_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            self.assertEqual(read_os_release_fields(missing), {})

    def test_read_os_release_fields_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text('\n# a comment\nID=fedora\n\nVERSION_ID="44"\n')
            self.assertEqual(
                read_os_release_fields(os_release),
                {"ID": "fedora", "VERSION_ID": "44"},
            )

    def test_pin_action_uses_line_leaves_unrecognized_action_unchanged(self) -> None:
        # Matches the "uses: action@ref" shape but names an action this tool
        # has no pin recorded for -- must pass through untouched rather than
        # dropping or mangling the line.
        line = "      uses: some-org/unpinned-action@v9"
        self.assertEqual(pin_action_uses_line(line), line)

    def test_ensure_workflow_job_env_entries_returns_unchanged_without_env_or_steps_anchor(self) -> None:
        workflow_text = "name: Build\njobs:\n  build:\n    name: build\n"
        result = ensure_workflow_job_env_entries(workflow_text, [("FOO", "bar")])
        self.assertEqual(result, workflow_text)

    def test_validate_config_rejects_unsupported_base_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite-deck:stable"
        app.config.base_image_name = "Bazzite Deck"
        with self.assertRaisesRegex(CommandError, "supported base images"):
            app.validate_config()

    def test_validate_config_rejects_empty_base_image_uri(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = ""
        with self.assertRaisesRegex(CommandError, "Base image URI is missing or invalid"):
            app.validate_config()

    def test_validate_config_rejects_base_image_uri_containing_whitespace(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable extra"
        with self.assertRaisesRegex(CommandError, "Base image URI is missing or invalid"):
            app.validate_config()

    def test_validate_config_rejects_invalid_repo_name(self) -> None:
        app = self.make_app()
        app.config.repo_name = ".git"
        with self.assertRaisesRegex(CommandError, "Repository name is invalid"):
            app.validate_config()

    def test_validate_config_rejects_unparseable_image_names_for_both_methods(self) -> None:
        # validate_config() is the gate a config loaded from a state file goes
        # through as well as one the wizard just built, and it runs before
        # repository creation and signing setup. Both build methods put the
        # repo name in the published reference, so neither may pass one that
        # cannot be parsed.
        for method in ("containerfile", "bluebuild"):
            for name in ("test..image", "test.-image", "test___image"):
                with self.subTest(method=method, repo_name=name):
                    app = self.make_app()
                    app.config.method = method
                    app.config.repo_name = name
                    with self.assertRaisesRegex(CommandError, "Repository name is invalid"):
                        app.validate_config()

    def test_validate_config_accepts_the_separators_a_reference_allows(self) -> None:
        for method in ("containerfile", "bluebuild"):
            for name in ("test.image", "test_image", "test__image", "test--image"):
                with self.subTest(method=method, repo_name=name):
                    app = self.make_app()
                    app.config.method = method
                    app.config.repo_name = name
                    app.validate_config()
                    self.assertEqual(
                        app.published_image_ref(),
                        f"ghcr.io/example/{name}:latest",
                    )

    def test_match_base_image_accepts_fedora_atomic_refs_with_other_tags(self) -> None:
        app = self.make_app()
        matched = app.match_base_image("quay.io/fedora-ostree-desktops/kinoite:44")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.name, "Fedora Kinoite")

    def test_ensure_signing_ready_requires_cosign(self) -> None:
        app = self.make_app()
        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("atomic_image_builder.command_exists", side_effect=lambda name: False if name == "cosign" else True):
                with self.assertRaisesRegex(CommandError, "brew install cosign"):
                    app.ensure_signing_ready("example", "test-image")

    def test_repo_secret_exists_returns_true_for_exact_secret_name(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(
            ["gh", "secret", "list"], 0, '[{"name":"OTHER"},{"name":"SIGNING_SECRET"}]', ""
        )
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", return_value=completed) as run_mock:
                self.assertTrue(app.repo_secret_exists("example", "test-image", "SIGNING_SECRET"))
        run_mock.assert_called_once_with(
            ["gh", "secret", "list", "-R", "example/test-image", "--json", "name"], check=False
        )

    def test_repo_secret_exists_returns_false_for_valid_empty_list(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(["gh", "secret", "list"], 0, "[]", "")
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", return_value=completed):
                self.assertFalse(app.repo_secret_exists("example", "test-image", "SIGNING_SECRET"))

    def test_repo_secret_exists_fails_closed_for_probe_errors(self) -> None:
        app = self.make_app()
        cases = [
            subprocess.CompletedProcess(["gh"], 1, "", "network failure"),
            subprocess.CompletedProcess(["gh"], 0, "not json", ""),
            subprocess.CompletedProcess(["gh"], 0, '{"name":"SIGNING_SECRET"}', ""),
            subprocess.CompletedProcess(["gh"], 0, '[{"name":42}]', ""),
        ]
        with patch("atomic_image_builder.command_exists", return_value=True):
            for completed in cases:
                with self.subTest(stdout=completed.stdout, returncode=completed.returncode):
                    with patch("atomic_image_builder.run", return_value=completed):
                        with self.assertRaisesRegex(CommandError, "status could not be verified"):
                            app.repo_secret_exists("example", "test-image", "SIGNING_SECRET")

    def test_repo_secret_exists_raises_when_gh_not_installed(self) -> None:
        app = self.make_app()
        with patch("atomic_image_builder.command_exists", return_value=False):
            with patch("atomic_image_builder.run") as run_mock:
                with self.assertRaisesRegex(CommandError, "gh is not installed"):
                    app.repo_secret_exists("example", "test-image", "SIGNING_SECRET")
        run_mock.assert_not_called()

    def test_command_exists_reflects_shutil_which(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            self.assertTrue(atomic_image_builder.command_exists("gh"))
        with patch("shutil.which", return_value=None):
            self.assertFalse(atomic_image_builder.command_exists("gh"))

    def test_repo_file_exists_returns_true_on_success(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(["gh", "api"], 0, "{}", "")
        with patch("atomic_image_builder.run", return_value=completed) as run_mock:
            self.assertTrue(app.repo_file_exists("example", "test-image", "cosign.pub"))
        run_mock.assert_called_once_with(
            ["gh", "api", "/repos/example/test-image/contents/cosign.pub"], check=False
        )

    def test_repo_file_exists_returns_false_on_nonzero_exit(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(["gh", "api"], 1, "", "not found")
        with patch("atomic_image_builder.run", return_value=completed):
            self.assertFalse(app.repo_file_exists("example", "test-image", "cosign.pub"))

    def test_repo_has_state_file_delegates_to_repo_file_exists(self) -> None:
        app = self.make_app()
        with patch.object(app, "repo_file_exists", return_value=True) as file_exists_mock:
            self.assertTrue(app.repo_has_state_file("example", "test-image"))
        file_exists_mock.assert_called_once_with("example", "test-image", STATE_FILE)

    def test_gh_json_parses_command_output(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(["gh"], 0, '{"login":"example"}', "")
        with patch("atomic_image_builder.run", return_value=completed) as run_mock:
            self.assertEqual(app.gh_json(["api", "user"]), {"login": "example"})
        run_mock.assert_called_once_with(["gh", "api", "user"])

    def test_gh_json_treats_blank_stdout_as_null(self) -> None:
        app = self.make_app()
        completed = subprocess.CompletedProcess(["gh"], 0, "", "")
        with patch("atomic_image_builder.run", return_value=completed):
            self.assertIsNone(app.gh_json(["api", "user"]))

    def test_gh_json_with_spinner_parses_captured_output(self) -> None:
        app = self.make_app()
        with patch.object(app.gum, "spinner_capture", return_value='{"login":"example"}') as spinner_mock:
            self.assertEqual(app.gh_json_with_spinner("Loading...", ["api", "user"]), {"login": "example"})
        spinner_mock.assert_called_once_with("Loading...", ["gh", "api", "user"])

    def test_gh_json_with_spinner_treats_blank_output_as_null(self) -> None:
        app = self.make_app()
        with patch.object(app.gum, "spinner_capture", return_value=""):
            self.assertIsNone(app.gh_json_with_spinner("Loading...", ["api", "user"]))

    def test_clone_repo_invokes_gum_spinner_with_gh_repo_clone(self) -> None:
        app = self.make_app()
        target = Path("/tmp/example-target")
        with patch.object(app.gum, "spinner") as spinner_mock:
            app.clone_repo("example", "test-image", target)
        spinner_mock.assert_called_once_with(
            "Cloning example/test-image...",
            ["gh", "repo", "clone", "example/test-image", str(target)],
        )

    def test_ensure_signing_ready_fails_closed_when_cosign_password_missing(self) -> None:
        # SIGNING_SECRET present but COSIGN_PASSWORD absent: the Containerfile
        # workflow cannot decrypt the key, so this must not report ready.
        app = self.make_app()
        app.config.method = "containerfile"
        secrets_present = ["SIGNING_SECRET"]
        completed = subprocess.CompletedProcess(
            ["gh", "secret", "list"], 0, json.dumps([{"name": n} for n in secrets_present]), ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "cosign.pub").write_text("PUBLIC KEY\n")
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", return_value=completed):
                    with self.assertRaisesRegex(CommandError, "COSIGN_PASSWORD is missing"):
                        app.ensure_signing_ready("example", "test-image", repo_dir=repo_dir)

    def test_ensure_signing_ready_accepts_both_secrets_present(self) -> None:
        app = self.make_app()
        app.config.method = "containerfile"
        completed = subprocess.CompletedProcess(
            ["gh", "secret", "list"], 0,
            '[{"name":"SIGNING_SECRET"},{"name":"COSIGN_PASSWORD"}]', ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "cosign.pub").write_text("PUBLIC KEY\n")
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", return_value=completed):
                    self.assertTrue(
                        app.ensure_signing_ready("example", "test-image", repo_dir=repo_dir)
                    )

    def test_ensure_signing_ready_bluebuild_does_not_require_cosign_password(self) -> None:
        # BlueBuild generates its key with an empty password and never uploads
        # COSIGN_PASSWORD, so requiring it there would be a false failure.
        app = self.make_app()
        app.config.method = "bluebuild"
        completed = subprocess.CompletedProcess(
            ["gh", "secret", "list"], 0, '[{"name":"SIGNING_SECRET"}]', ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "cosign.pub").write_text("PUBLIC KEY\n")
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", return_value=completed):
                    self.assertTrue(
                        app.ensure_signing_ready("example", "test-image", repo_dir=repo_dir)
                    )

    def test_ensure_signing_ready_does_not_change_keys_when_secret_probe_fails(self) -> None:
        app = self.make_app()
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 1, "", "probe failed")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with self.assertRaisesRegex(CommandError, "status could not be verified"):
                    app.ensure_signing_ready("example", "test-image")

        self.assertFalse(any(call[:2] == ["cosign", "generate-key-pair"] for call in calls))
        self.assertFalse(any(call[:3] == ["gh", "secret", "set"] for call in calls))

    def test_ensure_signing_ready_uploads_password_and_private_key_secrets(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        seen_calls: list[tuple[list[str], Path | None, str | None, dict[str, str] | None]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            seen_calls.append((list(args), cwd, stdin, env))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("PUBLIC KEY")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("atomic_image_builder.command_exists", side_effect=lambda name: True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    self.assertTrue(app.ensure_signing_ready("example", "test-image"))

        self.assertEqual(app.generated_cosign_pub, "PUBLIC KEY")
        self.assertTrue(any(level == "success" and "COSIGN_PASSWORD" in message for level, message in app.gum.messages))
        cosign_call = next(call for call in seen_calls if call[0][:2] == ["cosign", "generate-key-pair"])
        self.assertTrue(cosign_call[3] is not None and cosign_call[3].get("COSIGN_PASSWORD"))
        password_call = next(call for call in seen_calls if call[0][:4] == ["gh", "secret", "set", "COSIGN_PASSWORD"])
        self.assertTrue(password_call[2])
        key_call = next(call for call in seen_calls if call[0][:4] == ["gh", "secret", "set", "SIGNING_SECRET"])
        self.assertEqual(key_call[2], "PRIVATE KEY")

    def test_ensure_signing_ready_bluebuild_uses_empty_password_and_skips_cosign_password_secret(self) -> None:
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        seen_calls: list[tuple[list[str], Path | None, str | None, dict[str, str] | None]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            seen_calls.append((list(args), cwd, stdin, env))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("PUBLIC KEY")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("atomic_image_builder.command_exists", side_effect=lambda name: True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    self.assertTrue(app.ensure_signing_ready("example", "test-image"))

        self.assertEqual(app.generated_cosign_pub, "PUBLIC KEY")
        self.assertTrue(any(level == "success" and "BlueBuild" in message for level, message in app.gum.messages))
        cosign_call = next(call for call in seen_calls if call[0][:2] == ["cosign", "generate-key-pair"])
        self.assertTrue(cosign_call[3] is not None)
        self.assertEqual(cosign_call[3].get("COSIGN_PASSWORD"), "")
        self.assertFalse(any(call[0][:4] == ["gh", "secret", "set", "COSIGN_PASSWORD"] for call in seen_calls))
        key_call = next(call for call in seen_calls if call[0][:4] == ["gh", "secret", "set", "SIGNING_SECRET"])
        self.assertEqual(key_call[2], "PRIVATE KEY")

    def test_ensure_signing_ready_aborts_if_cosign_password_upload_fails(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("PUBLIC KEY")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:4] == ["gh", "secret", "set", "COSIGN_PASSWORD"]:
                return subprocess.CompletedProcess(list(args), 1, "", "nope")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with self.assertRaisesRegex(CommandError, "COSIGN_PASSWORD"):
                        app.ensure_signing_ready("example", "test-image")

        self.assertFalse(any(call[:4] == ["gh", "secret", "set", "SIGNING_SECRET"] for call in calls))

    def test_ensure_signing_ready_warns_when_signing_secret_retry_declined(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: False

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("PUBLIC KEY")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:4] == ["gh", "secret", "set", "SIGNING_SECRET"]:
                return subprocess.CompletedProcess(list(args), 1, "", "nope")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with self.assertRaises(CommandError):
                        app.ensure_signing_ready("example", "test-image")

        self.assertTrue(any(level == "error" and "half-" in message for level, message in app.gum.messages))

    def test_ensure_signing_ready_raises_when_cosign_pub_missing_locally(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch.object(app, "repo_secret_exists", return_value=True):
                with self.assertRaisesRegex(CommandError, "cosign.pub is missing"):
                    app.ensure_signing_ready("example", "test-image", repo_dir=repo_dir)

    def test_rotate_signing_key_aborts_if_secret_upload_fails(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:4] == ["gh", "secret", "set", "COSIGN_PASSWORD"]:
                return subprocess.CompletedProcess(list(args), 1, "", "nope")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

            subject = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=repo_dir, check=True, text=True, capture_output=True)
            self.assertEqual(subject.stdout.strip(), "Initial key")
            self.assertEqual((repo_dir / "cosign.pub").read_text(), "OLD PUBLIC KEY\n")

        self.assertTrue(any(level == "error" and "COSIGN_PASSWORD" in message for level, message in app.gum.messages))
        self.assertFalse(any(call[:2] == ["git", "commit"] for call in calls))

    def test_rotate_signing_key_warns_when_signing_secret_retry_declined(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        confirm_answers = iter([True, False])
        app.gum.confirm = lambda _prompt, default=False: next(confirm_answers)
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:4] == ["gh", "secret", "set", "SIGNING_SECRET"]:
                return subprocess.CompletedProcess(list(args), 1, "", "nope")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

            self.assertEqual((repo_dir / "cosign.pub").read_text(), "OLD PUBLIC KEY\n")

        self.assertTrue(any(level == "warn" and "half-complete" in message for level, message in app.gum.messages))
        self.assertFalse(any(call[:2] == ["git", "commit"] for call in calls))

    def test_rotate_signing_key_bluebuild_abort_is_not_half_complete(self) -> None:
        # The half-complete warning is specific to the containerfile method,
        # where COSIGN_PASSWORD has already been replaced by the time
        # SIGNING_SECRET fails. bluebuild never uploads COSIGN_PASSWORD, so
        # declining the retry leaves the repo exactly as it was -- warning
        # about a broken signing setup there would send the user chasing a
        # problem that does not exist.
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        confirm_answers = iter([True, False])
        app.gum.confirm = lambda _prompt, default=False: next(confirm_answers)

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:4] == ["gh", "secret", "set", "SIGNING_SECRET"]:
                return subprocess.CompletedProcess(list(args), 1, "", "nope")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)
            self.assertEqual((repo_dir / "cosign.pub").read_text(), "OLD PUBLIC KEY\n")

        self.assertFalse(any(level == "warn" and "half-complete" in message for level, message in app.gum.messages))

    def test_rotate_signing_key_warns_when_post_upload_commit_fails(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:2] == ["git", "commit"]:
                # Simulate a pre-commit hook failure after secrets were uploaded.
                raise CommandError("pre-commit hook failed")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

        self.assertTrue(any(level == "warn" and "half-complete" in message for level, message in app.gum.messages))
        self.assertFalse(any(call[:2] == ["git", "push"] for call in calls))

    def test_rotate_signing_key_warns_when_cosign_pub_copy_fails(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        # The new public key is written straight into the repo,
                        # so simulate the failure on that write.
                        real_write_text = Path.write_text

                        def failing_write_text(self, *args, **kwargs):
                            if self.name == "cosign.pub" and self.parent == repo_dir:
                                raise OSError("disk full")
                            return real_write_text(self, *args, **kwargs)

                        with patch.object(Path, "write_text", failing_write_text):
                            app.rotate_signing_key(repo_dir)

        self.assertTrue(any(level == "warn" and "half-complete" in message for level, message in app.gum.messages))
        self.assertFalse(any(call[:2] == ["git", "push"] for call in calls))

    # ── shared signing-key provisioning ─────────────────────────────────
    # ensure_signing_ready and rotate_signing_key run the same generate-and-
    # upload sequence through generate_and_upload_signing_key. These pin the
    # two callers' differing failure contracts so the shared helper cannot
    # drift one of them.

    def signing_run_stub(self, *, signing_secret_rc: int = 0, password_rc: int = 0):
        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                name = args[3]
                rc = password_rc if name == "COSIGN_PASSWORD" else signing_secret_rc
                return subprocess.CompletedProcess(list(args), rc, "", "")
            if args[:3] == ["gh", "secret", "list"]:
                return subprocess.CompletedProcess(list(args), 0, "[]", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        return fake_run

    def test_ensure_signing_ready_raises_when_user_declines_retry(self) -> None:
        app = self.make_app()
        app.config.method = "containerfile"
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: False
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=self.signing_run_stub(signing_secret_rc=1)):
                with self.assertRaisesRegex(CommandError, "half-complete"):
                    app.ensure_signing_ready("example", "test-image")

    def test_ensure_signing_ready_bluebuild_raises_distinct_decline_error(self) -> None:
        app = self.make_bluebuild_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: False
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=self.signing_run_stub(signing_secret_rc=1)):
                with self.assertRaisesRegex(CommandError, "SIGNING_SECRET upload was not completed"):
                    app.ensure_signing_ready("example", "test-image")

    def test_rotate_signing_key_warns_and_returns_when_user_declines_retry(self) -> None:
        # Rotation reports and returns rather than raising, so the update menu
        # survives.
        app = self.make_app()
        app.config.method = "containerfile"
        stub = GumStub()
        confirms = iter([True, False])
        stub.confirm = lambda _prompt, default=False: next(confirms, False)
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=self.signing_run_stub(signing_secret_rc=1)):
                    app.rotate_signing_key(repo_dir)
        self.assertTrue(
            any(level == "warn" and "half-complete" in message for level, message in stub.messages)
        )

    def test_rotate_signing_key_reports_keypair_failure_without_raising(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        app.gum = stub

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                return subprocess.CompletedProcess(list(args), 1, "", "cosign exploded")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    app.rotate_signing_key(repo_dir)
        self.assertTrue(
            any(level == "error" and "cosign keypair" in message for level, message in stub.messages)
        )

    def test_rotate_signing_key_warns_when_repo_cannot_be_identified(self) -> None:
        # Without an owner/repo there is nothing safe to rotate against.
        app = self.make_app()
        app.config.github_user = ""
        app.config.repo_name = ""
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "generate_and_upload_signing_key") as generate_mock:
            app.rotate_signing_key(Path("/nonexistent"))

        generate_mock.assert_not_called()
        self.assertTrue(
            any(level == "warn" and "configured image repo" in message for level, message in stub.messages)
        )

    def test_rotate_signing_key_returns_when_confirm_is_declined(self) -> None:
        # Rotation overwrites real key material; declining the confirm must
        # short-circuit before any tool check or key generation happens.
        app = self.make_app()
        stub = GumStub()
        prompts: list[str] = []

        def fake_confirm(prompt, default=False):
            prompts.append(prompt)
            self.assertFalse(default)
            return False

        stub.confirm = fake_confirm
        app.gum = stub
        with patch("atomic_image_builder.command_exists") as exists_mock:
            with patch.object(app, "generate_and_upload_signing_key") as generate_mock:
                app.rotate_signing_key(Path("/nonexistent"))

        generate_mock.assert_not_called()
        exists_mock.assert_not_called()
        self.assertEqual(len(prompts), 1)
        self.assertIn("Rotate the cosign signing key?", prompts[0])

    def test_rotate_signing_key_warns_when_one_tool_is_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name != "cosign"):
            with patch.object(app, "generate_and_upload_signing_key") as generate_mock:
                app.rotate_signing_key(Path("/nonexistent"))

        generate_mock.assert_not_called()
        self.assertIn(("warn", "cosign is required to rotate the signing key."), stub.messages)

    def test_rotate_signing_key_warns_when_both_tools_are_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=False):
            with patch.object(app, "generate_and_upload_signing_key") as generate_mock:
                app.rotate_signing_key(Path("/nonexistent"))

        generate_mock.assert_not_called()
        self.assertIn(("warn", "cosign, gh are required to rotate the signing key."), stub.messages)

    def test_generate_and_upload_signing_key_skips_password_for_bluebuild(self) -> None:
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            return self.signing_run_stub()(args, cwd=cwd, env=env, check=check, capture=capture, stdin=stdin)

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                pub = app.generate_and_upload_signing_key(
                    "example", "test-image", upload_failed_note="x", half_complete_note="y"
                )
        self.assertEqual(pub, "NEW PUBLIC KEY\n")
        secret_names = [call[3] for call in calls if call[:3] == ["gh", "secret", "set"]]
        self.assertEqual(secret_names, ["SIGNING_SECRET"])

    def test_generate_and_upload_signing_key_uploads_both_for_containerfile(self) -> None:
        app = self.make_app()
        app.config.method = "containerfile"
        app.gum = GumStub()
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            calls.append(list(args))
            return self.signing_run_stub()(args, cwd=cwd, env=env, check=check, capture=capture, stdin=stdin)

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                app.generate_and_upload_signing_key(
                    "example", "test-image", upload_failed_note="x", half_complete_note="y"
                )
        secret_names = [call[3] for call in calls if call[:3] == ["gh", "secret", "set"]]
        self.assertEqual(secret_names, ["COSIGN_PASSWORD", "SIGNING_SECRET"])

    def test_generate_and_upload_signing_key_retries_after_bluebuild_signing_secret_failure(self) -> None:
        # bluebuild never uploads COSIGN_PASSWORD, so a failed SIGNING_SECRET
        # upload there is a plain retry loop rather than the "half-complete"
        # state that applies to the containerfile method.
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        attempts = {"count": 0}

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                attempts["count"] += 1
                rc = 1 if attempts["count"] == 1 else 0
                return subprocess.CompletedProcess(list(args), rc, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                pub = app.generate_and_upload_signing_key(
                    "example", "test-bb-image", upload_failed_note="upload failed", half_complete_note="half complete"
                )

        self.assertEqual(pub, "NEW PUBLIC KEY\n")
        self.assertEqual(attempts["count"], 2)
        errors = [msg for level, msg in app.gum.messages if level == "error"]
        self.assertIn("upload failed", errors)

    def test_generate_and_upload_signing_key_retries_after_half_complete_upload(self) -> None:
        # The containerfile method uploads COSIGN_PASSWORD first, so a failed
        # SIGNING_SECRET leaves GitHub half-configured. Agreeing to retry has
        # to loop and finish the job -- the decline path was covered, the
        # accept path was not, and this is the one that repairs the repo.
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        attempts = {"signing": 0}

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if args[:3] == ["gh", "secret", "set"] and "SIGNING_SECRET" in args:
                attempts["signing"] += 1
                return subprocess.CompletedProcess(list(args), 1 if attempts["signing"] == 1 else 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                pub = app.generate_and_upload_signing_key(
                    "example", "test-image", upload_failed_note="upload failed", half_complete_note="half complete"
                )

        self.assertEqual(pub, "NEW PUBLIC KEY\n")
        self.assertEqual(attempts["signing"], 2)
        errors = [msg for level, msg in app.gum.messages if level == "error"]
        self.assertIn("half complete", errors)

    def test_rotate_signing_key_happy_path(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[tuple[list[str], str | None]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            args = list(args)
            calls.append((args, stdin))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "git":
                proc = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True, check=False)
                return subprocess.CompletedProcess(args, proc.returncode, proc.stdout, proc.stderr)
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

            subject = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=repo_dir, check=True, text=True, capture_output=True)
            self.assertEqual(subject.stdout.strip(), "Rotate cosign signing key")
            self.assertEqual((repo_dir / "cosign.pub").read_text(), "NEW PUBLIC KEY\n")

        self.assertTrue(any(call[0] == ["git", "add", "cosign.pub"] for call in calls))
        secret_names = [call[0][3] for call in calls if call[0][:3] == ["gh", "secret", "set"]]
        self.assertIn("SIGNING_SECRET", secret_names)
        self.assertIn("COSIGN_PASSWORD", secret_names)
        self.assertTrue(any(level == "success" and "Rotated" in message for level, message in app.gum.messages))

    def test_rotate_signing_key_bluebuild_uses_empty_password_and_skips_cosign_password_secret(self) -> None:
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            args = list(args)
            calls.append((args, stdin, env))
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "git":
                proc = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True, check=False)
                return subprocess.CompletedProcess(args, proc.returncode, proc.stdout, proc.stderr)
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

        cosign_call = next(call for call in calls if call[0][:2] == ["cosign", "generate-key-pair"])
        self.assertTrue(cosign_call[2] is not None)
        self.assertEqual(cosign_call[2].get("COSIGN_PASSWORD"), "")
        secret_names = [call[0][3] for call in calls if call[0][:3] == ["gh", "secret", "set"]]
        self.assertIn("SIGNING_SECRET", secret_names)
        self.assertNotIn("COSIGN_PASSWORD", secret_names)

    def test_rotate_signing_key_never_puts_password_in_argv(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.gum.confirm = lambda _prompt, default=False: True
        calls: list[list[str]] = []

        def fake_run(args, *, cwd=None, env=None, check=True, capture=True, stdin=None):
            args = list(args)
            calls.append(args)
            if args[:2] == ["cosign", "generate-key-pair"]:
                assert cwd is not None
                (cwd / "cosign.key").write_text("PRIVATE KEY")
                (cwd / "cosign.pub").write_text("NEW PUBLIC KEY\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["gh", "secret", "set"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "git":
                proc = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True, check=False)
                return subprocess.CompletedProcess(args, proc.returncode, proc.stdout, proc.stderr)
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            self.init_signing_repo(repo_dir)
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch("atomic_image_builder.secrets.token_urlsafe", return_value="ROTATE_PASSWORD"):
                        app.rotate_signing_key(repo_dir)

        self.assertFalse(any("ROTATE_PASSWORD" in arg for call in calls for arg in call))

    def test_preflight_requires_cosign(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub

        def fake_exists(name: str) -> bool:
            if name == "cosign":
                return False
            return True

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch("atomic_image_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "auth", "status"], 0, "", "")
                with patch.object(app, "gh_json", return_value={"login": "example"}):
                    with self.assertRaises(SystemExit):
                        app.preflight()
        self.assertTrue(any("startup checks" in message for level, message in app.gum.messages if level == "warn"))
        self.assertTrue(any("brew install cosign" in message for level, message in app.gum.messages if level == "hint"))
        self.assertEqual(stub.prompts, ["Press Enter to exit to the terminal..."])

    def test_preflight_requires_github_cli(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub

        def fake_exists(name: str) -> bool:
            return name in {"gum", "git", "cosign", "dnf5", "rpm-ostree"}

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with self.assertRaises(SystemExit):
                app.preflight()
        self.assertTrue(any("gh" in message for level, message in app.gum.messages if level == "hint"))
        self.assertTrue(any("brew install gh" in message for level, message in app.gum.messages if level == "hint"))
        self.assertEqual(stub.prompts, ["Press Enter to exit to the terminal..."])

    def test_preflight_runs_github_setup_guide_when_login_missing(self) -> None:
        # When a GitHub login is the only thing missing, preflight walks the
        # user through it (via github_setup_guide) and continues instead of
        # exiting — this is what makes the container's first run work.
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub

        def fake_exists(name: str) -> bool:
            return name in {"gum", "git", "gh", "cosign", "dnf5", "rpm-ostree"}

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", "")):
                with patch.object(app, "github_setup_guide") as guide:
                    with patch.object(app, "github_login_name", return_value="example"):
                        app.preflight()

        guide.assert_called_once()
        self.assertEqual(app.github_user, "example")
        self.assertTrue(app.github_available)
        self.assertTrue(
            any(level == "success" and "example" in message for level, message in stub.messages)
        )

    def test_preflight_skips_setup_guide_when_other_prerequisites_are_missing(self) -> None:
        # If anything besides the login is missing, preflight must not launch
        # the login guide — it hard-fails so the user fixes the tools first.
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub

        def fake_exists(name: str) -> bool:
            # cosign missing, and (below) not logged in either
            return name in {"gum", "git", "gh", "dnf5", "rpm-ostree"}

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", "")):
                with patch.object(app, "github_setup_guide") as guide:
                    with self.assertRaises(SystemExit):
                        app.preflight()

        guide.assert_not_called()
        self.assertTrue(any("gh auth login" in message for level, message in app.gum.messages if level == "hint"))
        self.assertEqual(stub.prompts, ["Press Enter to exit to the terminal..."])

    def test_github_setup_guide_configures_git_credential_helper_after_login(self) -> None:
        # After a successful `gh auth login`, the guide must run
        # `gh auth setup-git` so the raw `git push` in build/update flows can
        # authenticate — the user may have declined gh's own Git prompt.
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda *_a, **_k: ["I already have a GitHub account - log me in"]
        stub.confirm = lambda *_a, **_k: True
        app.gum = stub

        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.run", side_effect=fake_run):
            app.github_setup_guide()

        self.assertIn(["gh", "auth", "login"], calls)
        self.assertIn(["gh", "auth", "setup-git"], calls)
        # setup-git must come after a successful login, not before.
        self.assertLess(calls.index(["gh", "auth", "login"]), calls.index(["gh", "auth", "setup-git"]))

    def test_run_includes_args_on_error(self) -> None:
        proc = subprocess.CompletedProcess(["git", "fake-verb"], returncode=1, stdout="", stderr="fatal: boom")
        with patch("atomic_image_builder.subprocess.run", return_value=proc):
            with self.assertRaises(CommandError) as raised:
                atomic_image_builder.run(["git", "fake-verb"])
        self.assertIn("fatal: boom", str(raised.exception))
        self.assertIn("git fake-verb", str(raised.exception))

    def test_run_main_checks_preflight_before_rendering_when_gum_is_missing(self) -> None:
        app = self.make_app()

        def fake_exists(name: str) -> bool:
            return False if name == "gum" else True

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch.object(app, "preflight", side_effect=SystemExit(1)) as preflight_mock:
                with patch.object(app, "clear") as clear_mock:
                    with patch.object(app, "banner") as banner_mock:
                        with patch.object(app, "startup_requirements") as startup_mock:
                            with patch.object(app, "main_menu") as menu_mock:
                                with self.assertRaises(SystemExit):
                                    app.run_main()

        preflight_mock.assert_called_once_with()
        clear_mock.assert_not_called()
        banner_mock.assert_not_called()
        startup_mock.assert_not_called()
        menu_mock.assert_not_called()

    def test_run_main_renders_landing_flow_when_gum_is_present(self) -> None:
        app = self.make_app()
        calls: list[str] = []

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch.object(app, "clear", side_effect=lambda: calls.append("clear")) as clear_mock:
                with patch.object(app, "banner", side_effect=lambda: calls.append("banner")) as banner_mock:
                    with patch.object(
                        app, "startup_requirements", side_effect=lambda: calls.append("startup_requirements")
                    ) as startup_mock:
                        with patch.object(
                            app, "preflight", side_effect=lambda: calls.append("preflight")
                        ) as preflight_mock:
                            with patch.object(
                                app, "main_menu", side_effect=lambda: calls.append("main_menu")
                            ) as menu_mock:
                                app.run_main()

        clear_mock.assert_called_once_with()
        banner_mock.assert_called_once_with()
        startup_mock.assert_called_once_with()
        preflight_mock.assert_called_once_with()
        menu_mock.assert_called_once_with()
        self.assertEqual(calls, ["clear", "banner", "startup_requirements", "preflight", "main_menu"])

    def test_banner_prints_tool_name_and_version(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            app.banner()
        output = buffer.getvalue()
        self.assertIn(TOOL_NAME, output)
        self.assertIn(VERSION, output)

    def test_startup_requirements_shows_both_landing_cards_then_prompts(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        titles: list[str] = []
        with patch.object(app, "landing_card", side_effect=lambda title, *a, **kw: titles.append(title)):
            app.startup_requirements()
        self.assertEqual(titles, ["Before You Start", "Important"])
        self.assertIn("Press Enter to start the preflight checks...", app.gum.prompts)

    def test_landing_card_passes_through_custom_style_colors(self) -> None:
        # Every current call site uses the default colors; this pins the path
        # a caller takes when it overrides foreground/background instead.
        app = self.make_app()
        stub = GumStub()
        seen_kwargs: dict[str, object] = {}

        def fake_style(*lines, **kwargs):
            seen_kwargs.update(kwargs)
            return "\n".join(lines)

        stub.style = fake_style
        app.gum = stub

        app.landing_card("Title", ["a line"], width=40, border_foreground=5, foreground=1, background=2)

        self.assertEqual(seen_kwargs["foreground"], 1)
        self.assertEqual(seen_kwargs["background"], 2)

    def test_config_from_state_payload_rejects_a_non_object_payload(self) -> None:
        # The state file is fetched from a remote repo, so it is the one place
        # a malformed payload arrives from outside this process.
        for payload in ([], "state", 7, None):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "must contain a JSON object"):
                    atomic_image_builder.config_from_state_payload(payload)

    def test_config_from_state_payload_validates_state_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "state_version must be an integer"):
            atomic_image_builder.config_from_state_payload({"state_version": "1"})
        with self.assertRaisesRegex(ValueError, "unsupported state_version: 2"):
            atomic_image_builder.config_from_state_payload({"state_version": 2})
        # Absent and in-range versions both load.
        self.assertEqual(atomic_image_builder.config_from_state_payload({}).method, Config().method)
        self.assertEqual(atomic_image_builder.config_from_state_payload({"state_version": 1}).method, Config().method)

    def test_config_from_state_payload_rejects_a_non_string_string_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_name must be a string"):
            atomic_image_builder.config_from_state_payload({"repo_name": 42})

    def test_preflight_reports_account_error_when_the_username_cannot_be_read(self) -> None:
        # gh is installed and authenticated, but `gh api user` fails. The tool
        # must not carry on with a blank owner: every downstream gh repo call
        # would be built from it.
        app = self.make_app()
        app.gum = GumStub()
        captured: dict[str, object] = {}

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh"], 0, "", "")):
                with patch.object(app, "github_login_name", side_effect=CommandError("gh api user failed")):
                    with patch.object(app, "render_preflight_failure", side_effect=lambda **kw: captured.update(kw)):
                        with self.assertRaises(SystemExit) as raised:
                            app.preflight()

        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(app.github_available)
        self.assertTrue(captured["github_account_error"])
        self.assertFalse(captured["github_login_missing"])

    def test_preflight_reports_account_error_when_login_succeeds_but_account_still_unreadable(self) -> None:
        # The only-login-missing path runs the guided `gh auth login`, and the
        # account read can still fail afterwards.
        app = self.make_app()
        app.gum = GumStub()
        captured: dict[str, object] = {}

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh"], 1, "", "")):
                with patch.object(app, "github_setup_guide") as guide:
                    with patch.object(app, "github_login_name", side_effect=CommandError("still no account")):
                        with patch.object(app, "render_preflight_failure", side_effect=lambda **kw: captured.update(kw)):
                            with self.assertRaises(SystemExit) as raised:
                                app.preflight()

        guide.assert_called_once_with()
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(app.github_available)
        self.assertTrue(captured["github_account_error"])

    def test_main_prints_version_and_exits_without_running_app(self) -> None:
        buffer = io.StringIO()
        with patch("sys.argv", ["atomic-image-builder", "--version"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                with redirect_stdout(buffer):
                    with self.assertRaises(SystemExit) as raised:
                        atomic_image_builder.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(f"{atomic_image_builder.TOOL_COMMAND} {VERSION}", buffer.getvalue())
        app_cls.assert_not_called()

    def test_main_prints_version_for_short_flag(self) -> None:
        buffer = io.StringIO()
        with patch("sys.argv", ["atomic-image-builder", "-V"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                with redirect_stdout(buffer):
                    with self.assertRaises(SystemExit) as raised:
                        atomic_image_builder.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(f"{atomic_image_builder.TOOL_COMMAND} {VERSION}", buffer.getvalue())
        app_cls.assert_not_called()

    def test_state_file_name_is_not_tied_to_the_command_name(self) -> None:
        # STATE_FILE lands in every managed repo and is how the tool recognises
        # repos it created. Renaming the command must never move it.
        self.assertEqual(atomic_image_builder.STATE_FILE, ".atomic-image-builder.json")
        self.assertNotIn(atomic_image_builder.TOOL_COMMAND, atomic_image_builder.STATE_FILE)

    def test_command_name_does_not_collide_with_the_container_wrapper(self) -> None:
        # contrib/aib installs `aib` to ~/.local/bin, which normally precedes
        # Homebrew's bin on PATH; sharing the name would shadow one silently.
        self.assertEqual(atomic_image_builder.TOOL_COMMAND, "aib-tool")
        self.assertNotEqual(atomic_image_builder.TOOL_COMMAND, "aib")

    def test_usage_text_names_the_tool_and_both_flags(self) -> None:
        usage = atomic_image_builder.usage_text()
        self.assertIn(f"{TOOL_NAME} {VERSION}", usage)
        self.assertIn(f"Usage: {atomic_image_builder.TOOL_COMMAND}", usage)
        self.assertIn("--help", usage)
        self.assertIn("--version", usage)
        # preflight() exits when either tuple has a missing tool, so the help
        # text has to name both or it sends someone down a dead end.
        for tool in (*atomic_image_builder.PRECHECK_REQUIRED_TOOLS, *atomic_image_builder.HOST_REQUIRED_TOOLS):
            self.assertIn(tool, usage)

    def test_main_prints_help_and_exits_without_running_app(self) -> None:
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                buffer = io.StringIO()
                with patch("sys.argv", ["atomic-image-builder", flag]):
                    with patch.object(atomic_image_builder, "App") as app_cls:
                        with redirect_stdout(buffer):
                            with self.assertRaises(SystemExit) as raised:
                                atomic_image_builder.main()
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(atomic_image_builder.usage_text(), buffer.getvalue())
                app_cls.assert_not_called()

    def test_main_runs_app_and_exits_zero_on_success(self) -> None:
        with patch("sys.argv", ["atomic-image-builder"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                app_instance = app_cls.return_value
                atomic_image_builder.main()
        app_instance.run_main.assert_called_once_with()

    def test_main_converts_screen_back_to_clean_exit(self) -> None:
        with patch("sys.argv", ["atomic-image-builder"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                app_cls.return_value.run_main.side_effect = atomic_image_builder.ScreenBack()
                with self.assertRaises(SystemExit) as raised:
                    atomic_image_builder.main()
        self.assertEqual(raised.exception.code, 0)

    def test_main_converts_command_error_to_exit_one_and_reports_it(self) -> None:
        with patch("sys.argv", ["atomic-image-builder"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                app_instance = app_cls.return_value
                app_instance.run_main.side_effect = CommandError("boom")
                with self.assertRaises(SystemExit) as raised:
                    atomic_image_builder.main()
        self.assertEqual(raised.exception.code, 1)
        app_instance.gum.error.assert_called_once_with("boom")

    def test_main_converts_keyboard_interrupt_to_exit_130(self) -> None:
        with patch("sys.argv", ["atomic-image-builder"]):
            with patch.object(atomic_image_builder, "App") as app_cls:
                app_cls.return_value.run_main.side_effect = KeyboardInterrupt()
                with self.assertRaises(SystemExit) as raised:
                    atomic_image_builder.main()
        self.assertEqual(raised.exception.code, 130)

    def test_add_packages_to_config_accepts_valid_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            added = app.add_packages_to_config(["tmux", "ripgrep"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux", "ripgrep"])
        self.assertTrue(any(level == "success" for level, _message in app.gum.messages))

    def test_add_packages_to_config_rejects_unsafe_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        added = app.add_packages_to_config(["tmux", "bad;rm"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.packages, [])
        self.assertTrue(any(level == "error" and "Invalid package value" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_rejects_missing_manual_packages(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: False for p in pkgs}):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.packages, [])
        self.assertTrue(any(level == "error" and "not found" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_keeps_checked_manual_packages_only(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", return_value={"tmux": True, "nethock": False}):
            added = app.add_packages_to_config(["tmux", "nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "error" and "nethock" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_warns_when_manual_check_is_unavailable(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: None for p in pkgs}):
            added = app.add_packages_to_config(["tmux"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "warn" for level, _message in app.gum.messages))

    def test_add_packages_to_config_keeps_missing_manual_packages_when_copr_is_configured(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: False for p in pkgs}):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["nethock"])
        self.assertTrue(any(level == "warn" and "host repos" in message for level, message in app.gum.messages))

    def test_add_removed_packages_to_config_accepts_valid_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            added = app.add_removed_packages_to_config(["vim-enhanced", "nano"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.removed_packages, ["vim-enhanced", "nano"])
        self.assertTrue(any(level == "success" for level, _message in app.gum.messages))

    def test_add_removed_packages_to_config_trusts_a_scanned_source(self) -> None:
        # The availability filter exists to catch typos in hand-typed names.
        # Names that came from the scan were read off the running system, so
        # re-checking them would be pointless work -- and would drop a real
        # removal if the host lookup happened to fail.
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "filter_available_manual_removed_packages") as filter_mock:
            added = app.add_removed_packages_to_config(["firefox"], source_label="system scan")
        filter_mock.assert_not_called()
        self.assertTrue(added)
        self.assertEqual(app.config.removed_packages, ["firefox"])

    def test_add_removed_packages_to_config_rejects_unsafe_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        added = app.add_removed_packages_to_config(["vim-enhanced", "bad;rm"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.removed_packages, [])
        self.assertTrue(any(level == "error" and "Invalid removed package value" in message for level, message in app.gum.messages))

    def test_add_removed_packages_to_config_rejects_missing_manual_packages(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: False for p in pkgs}):
            added = app.add_removed_packages_to_config(["nethock"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.removed_packages, [])
        self.assertTrue(any(level == "error" and "not found" in message for level, message in app.gum.messages))

    def test_add_services_manually_accepts_valid_tokens(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "sshd.service\ntailscaled.service\n"
        app.gum = stub
        app.add_services_manually()
        self.assertEqual(app.config.services, ["sshd.service", "tailscaled.service"])
        self.assertTrue(any(level == "success" for level, _message in stub.messages))

    def test_add_services_manually_rejects_unsafe_tokens_immediately(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "sshd.service\nfoo bar.service\n"
        app.gum = stub
        app.add_services_manually()
        self.assertEqual(app.config.services, [])
        self.assertTrue(
            any(
                level == "error" and "Invalid systemd service" in message
                for level, message in stub.messages
            )
        )

    def test_add_services_manually_returns_quietly_on_empty_input(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "\n  \n"
        app.gum = stub
        app.add_services_manually()
        self.assertEqual(app.config.services, [])
        self.assertFalse(any(level == "success" for level, _message in stub.messages))

    def test_lookup_host_packages_checks_multiple_packages_in_one_dnf5_call(self) -> None:
        # A single, real demo recording showed the "Checking package name"
        # spinner staying on the first package for 46 real seconds (dnf5's
        # cache warm-up) while every package after it flashed by in a
        # fraction of a second -- because each one was its own dnf5 call.
        # This asserts the fix directly: one dnf5 invocation covers every
        # requested package.
        app = self.make_app()
        stub = GumStub()
        calls: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            calls.append(list(command))
            return subprocess.CompletedProcess(list(command), 0, "tmux\nhtop\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["tmux", "htop", "nethock"])

        self.assertEqual(len(calls), 1)
        self.assertIn("tmux", calls[0])
        self.assertIn("htop", calls[0])
        self.assertIn("nethock", calls[0])
        self.assertIn("%{name}\n", calls[0])
        self.assertEqual(results, {"tmux": True, "htop": True, "nethock": False})

    def test_lookup_host_packages_asks_about_each_name_once(self) -> None:
        # A duplicate in the requested list must not become a duplicate
        # argument to dnf5. The one-call-for-everything batching is what keeps
        # this screen from stalling, and it only holds if the batch is a set.
        app = self.make_app()
        stub = GumStub()
        calls: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            calls.append(list(command))
            return subprocess.CompletedProcess(list(command), 0, "tmux\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["tmux", "tmux", "htop"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].count("tmux"), 1)
        self.assertEqual(results, {"tmux": True, "htop": False})

    def test_lookup_host_packages_skips_already_cached_packages(self) -> None:
        app = self.make_app()
        app.package_lookup_cache["tmux"] = True
        stub = GumStub()
        calls: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            calls.append(list(command))
            return subprocess.CompletedProcess(list(command), 0, "htop\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["tmux", "htop"])

        self.assertEqual(len(calls), 1)
        self.assertNotIn("tmux", calls[0])
        self.assertIn("htop", calls[0])
        self.assertEqual(results, {"tmux": True, "htop": True})

    def test_lookup_host_packages_does_not_call_dnf5_when_everything_is_cached(self) -> None:
        app = self.make_app()
        app.package_lookup_cache["tmux"] = True
        app.package_lookup_cache["htop"] = False
        stub = GumStub()
        stub.spinner_result = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run dnf5"))
        app.gum = stub
        results = app.lookup_host_packages(["tmux", "htop"])
        self.assertEqual(results, {"tmux": True, "htop": False})

    def test_lookup_host_packages_returns_none_for_all_when_dnf5_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run dnf5"))
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=False):
            results = app.lookup_host_packages(["tmux", "htop"])
        self.assertEqual(results, {"tmux": None, "htop": None})
        self.assertEqual(app.package_lookup_cache["tmux"], None)

    def test_lookup_host_packages_treats_returncode_zero_absence_as_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            _command, 0, "", ""
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["fake-pkg-one", "fake-pkg-two"])
        self.assertEqual(results, {"fake-pkg-one": False, "fake-pkg-two": False})

    def test_lookup_host_packages_treats_missing_marker_as_absent_regardless_of_returncode(self) -> None:
        # dnf5 repoquery can exit nonzero on a genuine "not found" result; the
        # missing-marker text in stdout/stderr is the authoritative signal for
        # absence, checked before the returncode==0 fallback.
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            _command, 1, "", "Error: no matches found"
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["fake-pkg"])
        self.assertEqual(results, {"fake-pkg": False})

    def test_lookup_host_packages_returns_none_when_uncheckable(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            _command, 1, "", "some unrelated dnf5 error"
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            results = app.lookup_host_packages(["tmux"])
        self.assertEqual(results, {"tmux": None})

    def test_lookup_host_packages_uses_singular_title_for_one_package(self) -> None:
        app = self.make_app()
        stub = GumStub()
        titles: list[str] = []

        def fake_spinner_result(title, command, *, cwd=None):
            titles.append(title)
            return subprocess.CompletedProcess(list(command), 0, "tmux\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            app.lookup_host_packages(["tmux"])
        self.assertEqual(titles, ["Checking package name: tmux"])

    def test_lookup_host_packages_uses_plural_title_for_multiple_packages(self) -> None:
        app = self.make_app()
        stub = GumStub()
        titles: list[str] = []

        def fake_spinner_result(title, command, *, cwd=None):
            titles.append(title)
            return subprocess.CompletedProcess(list(command), 0, "tmux\nhtop\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            app.lookup_host_packages(["tmux", "htop"])
        self.assertEqual(titles, ["Checking package names: tmux, htop"])

    def test_lookup_host_package_delegates_to_batched_lookup(self) -> None:
        app = self.make_app()
        with patch.object(app, "lookup_host_packages", return_value={"tmux": True}) as mock:
            result = app.lookup_host_package("tmux")
        mock.assert_called_once_with(["tmux"])
        self.assertTrue(result)

    def test_search_host_packages_parses_results_and_limits_output(self) -> None:
        app = self.make_app()
        seen_commands: list[list[str]] = []
        stub = GumStub()

        def fake_spinner_result(_title, _command, *, cwd=None):
            seen_commands.append(list(_command))
            output = "\n".join(
                [f"pkg{i}\tSummary {i}" for i in range(PACKAGE_SEARCH_LIMIT + 2)]
            )
            return subprocess.CompletedProcess(["dnf5", "repoquery"], 0, output, "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("pkg")

        self.assertIsNone(message)
        self.assertTrue(truncated)
        self.assertEqual(len(results), PACKAGE_SEARCH_LIMIT)
        self.assertEqual(results[0], ("pkg0", "Summary 0"))
        self.assertTrue(any("%{name}\t%{summary}\n" in command for command in seen_commands))

    def test_search_host_packages_reuses_the_cached_result_for_a_repeated_term(self) -> None:
        # dnf5 repoquery is the slow part of this screen. Searching the same
        # term twice -- which is exactly what paging back and forth does --
        # must not pay for it again.
        app = self.make_app()
        stub = GumStub()
        calls: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            calls.append(list(command))
            return subprocess.CompletedProcess(list(command), 0, "tmux\tTerminal multiplexer\n", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            first = app.search_host_packages("tmux")
            second = app.search_host_packages("TMUX")

        self.assertEqual(len(calls), 1)
        self.assertEqual(first[0], second[0])

    def test_search_host_packages_keeps_the_first_summary_for_a_repeated_name(self) -> None:
        # repoquery lists a package once per matching repo, so the same name
        # can arrive several times. The later rows are usually the same
        # package from another repo; taking the first keeps the result stable
        # rather than letting repo order decide the summary.
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, command, *, cwd=None: subprocess.CompletedProcess(
            list(command), 0, "tmux\tTerminal multiplexer\ntmux\tFrom another repo\n", ""
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, _truncated, message = app.search_host_packages("tmux")

        self.assertIsNone(message)
        self.assertEqual(results, [("tmux", "Terminal multiplexer")])

    def test_search_host_packages_reports_missing_cache_when_refresh_is_declined(self) -> None:
        app = self.make_app()
        stub = GumStub()
        commands: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            commands.append(list(command))
            return subprocess.CompletedProcess(
                ["dnf5", "repoquery"],
                1,
                "",
                'Cache-only enabled but no cache for repository "fedora"',
            )

        stub.spinner_result = fake_spinner_result
        stub.confirm = lambda _prompt, **_kwargs: False
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertEqual(message, atomic_image_builder.PACKAGE_SEARCH_NEEDS_METADATA)
        # Declining must not download anything.
        self.assertTrue(all("makecache" not in command for command in commands))

    def test_search_host_packages_refreshes_metadata_then_retries_the_search(self) -> None:
        app = self.make_app()
        stub = GumStub()
        commands: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            commands.append(list(command))
            if "makecache" in command:
                return subprocess.CompletedProcess(list(command), 0, "Metadata cache created.", "")
            if len([c for c in commands if "repoquery" in c]) == 1:
                return subprocess.CompletedProcess(
                    list(command),
                    1,
                    "",
                    'Cache-only enabled but no cache for repository "fedora"',
                )
            return subprocess.CompletedProcess(list(command), 0, "tmux\tTerminal multiplexer\n", "")

        stub.spinner_result = fake_spinner_result
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertIsNone(message)
        self.assertFalse(truncated)
        self.assertEqual(results, [("tmux", "Terminal multiplexer")])
        self.assertEqual([c for c in commands if "makecache" in c][0][-1], "makecache")
        # The failed query, the refresh, then the query again.
        self.assertEqual(len(commands), 3)
        self.assertTrue(any(level == "success" for level, _message in stub.messages))

    def test_search_host_packages_does_not_loop_when_refresh_leaves_cache_empty(self) -> None:
        app = self.make_app()
        stub = GumStub()
        commands: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            commands.append(list(command))
            if "makecache" in command:
                return subprocess.CompletedProcess(list(command), 0, "", "")
            return subprocess.CompletedProcess(
                list(command),
                1,
                "",
                'Cache-only enabled but no cache for repository "fedora"',
            )

        stub.spinner_result = fake_spinner_result
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertEqual(message, atomic_image_builder.PACKAGE_SEARCH_NEEDS_METADATA)
        # Query, refresh, query -- and then it stops rather than offering again.
        self.assertEqual(len(commands), 3)
        self.assertEqual(len([c for c in commands if "makecache" in c]), 1)

    def test_module_refuses_to_run_on_python_older_than_3_10(self) -> None:
        # The guard runs at import, so importing normally can never reach it --
        # the interpreter running these tests already satisfies it. Executing
        # the real source under a faked version is the only way to prove the
        # check fires rather than trusting that it would.
        module_path = Path(__file__).resolve().parents[1] / "atomic_image_builder.py"
        # Compile against the real absolute path, not a bare filename: coverage
        # attributes executed lines by file, so a relative name would run the
        # guard without ever crediting the line it lives on.
        code = compile(module_path.read_text(), str(module_path), "exec")
        with patch.object(sys, "version_info", (3, 9, 0)):
            with self.assertRaises(SystemExit) as raised:
                exec(code, {"__name__": "not_main_version_probe", "__file__": str(module_path)})
        self.assertIn("Python 3.10 or newer is required", str(raised.exception))

    def test_dnf5_state_dir_refuses_a_symlinked_path(self) -> None:
        # tempfile.gettempdir() is shared and world-writable, so another local
        # user can pre-create this name as a symlink and have dnf5 read and
        # write through it as us. The guard only helps if it actually fires.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "elsewhere"
            target.mkdir()
            attack = Path(tmp) / f"{atomic_image_builder.TOOL_SLUG}-dnf5-{os.getuid()}"
            attack.symlink_to(target)
            with patch("tempfile.gettempdir", return_value=tmp):
                with self.assertRaises(CommandError) as raised:
                    app.dnf5_state_dir()
        self.assertIn("not a plain directory", str(raised.exception))
        self.assertIn("symlink attack", str(raised.exception))

    def test_dnf5_state_dir_refuses_a_path_that_is_not_a_directory(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / f"{atomic_image_builder.TOOL_SLUG}-dnf5-{os.getuid()}"
            blocker.write_text("not a directory")
            with patch("tempfile.gettempdir", return_value=tmp):
                with self.assertRaises(CommandError) as raised:
                    app.dnf5_state_dir()
        self.assertIn("not a plain directory", str(raised.exception))

    def test_dnf5_state_dir_refuses_a_directory_owned_by_someone_else(self) -> None:
        # Chowning needs privileges, so instead make the tool believe it is a
        # different uid than the one that owns the directory it just made --
        # which is exactly the state this check exists to detect.
        app = self.make_app()
        real_uid = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tempfile.gettempdir", return_value=tmp):
                with patch("atomic_image_builder.os.getuid", return_value=real_uid + 1):
                    with self.assertRaises(CommandError) as raised:
                        app.dnf5_state_dir()
        self.assertIn("owned by uid", str(raised.exception))
        self.assertIn("not us", str(raised.exception))

    def test_dnf5_state_dir_creates_a_private_directory_when_the_path_is_clean(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tempfile.gettempdir", return_value=tmp):
                first = app.dnf5_state_dir()
                # Called on every use, so it has to stay happy with its own work.
                second = app.dnf5_state_dir()
        self.assertEqual(first, second)
        self.assertEqual(first.name, f"{atomic_image_builder.TOOL_SLUG}-dnf5-{os.getuid()}")

    def test_dnf5_state_dir_is_scoped_per_uid(self) -> None:
        # An unscoped name would let one user's directory block another's.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tempfile.gettempdir", return_value=tmp):
                mine = app.dnf5_state_dir()
        self.assertTrue(mine.name.endswith(f"-{os.getuid()}"))

    def test_refresh_package_metadata_runs_makecache_with_scoped_state_dir(self) -> None:
        app = self.make_app()
        stub = GumStub()
        commands: list[list[str]] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            commands.append(list(command))
            return subprocess.CompletedProcess(list(command), 0, "", "")

        stub.spinner_result = fake_spinner_result
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            self.assertTrue(app.refresh_package_metadata())

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[0], "env")
        self.assertTrue(command[1].startswith("XDG_STATE_HOME="))
        self.assertEqual(command[2:], ["dnf5", "makecache"])
        self.assertTrue(any(level == "success" for level, _message in stub.messages))

    def test_refresh_package_metadata_reports_a_bare_failure_without_detail(self) -> None:
        # dnf5 can fail with nothing on either stream. The error still has to
        # be reported; there is just no last line to quote under it.
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, command, *, cwd=None: subprocess.CompletedProcess(list(command), 1, "", "")
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            self.assertFalse(app.refresh_package_metadata())
        # The screen's own preamble hint still runs; what must not appear is a
        # follow-up hint quoting a detail line that does not exist.
        self.assertEqual(stub.messages[-1][0], "error")

    def test_refresh_package_metadata_reports_failure_detail(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, command, *, cwd=None: subprocess.CompletedProcess(
            list(command), 1, "", "first line\nCurl error: Could not resolve host"
        )
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            self.assertFalse(app.refresh_package_metadata())

        self.assertTrue(any(level == "error" for level, _message in stub.messages))
        self.assertTrue(
            any(level == "hint" and "Could not resolve host" in message for level, message in stub.messages)
        )

    def test_search_host_packages_treats_no_matches_as_empty_not_error(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            ["dnf5", "repoquery"],
            1,
            "",
            "Error: No matches found for the specified package name or provided globs.",
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("doesnotexist")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertIsNone(message)

    def test_search_host_packages_defaults_summary_when_no_tab_present(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            ["dnf5", "repoquery"], 0, "tmux\n", ""
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertIsNone(message)
        self.assertFalse(truncated)
        self.assertEqual(results, [("tmux", "")])

    def test_gum_spinner_result_treats_missing_status_file_contents_as_failure(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gum", "spin"], 0, "", "")):
            proc = gum.spinner_result("Checking package name", ["true"])

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_gum_spinner_result_cleans_tempfile_when_setup_fails(self) -> None:
        gum = Gum()
        original_named_temporary_file = tempfile.NamedTemporaryFile
        created_paths: list[str] = []

        def fake_named_temporary_file(*args, **kwargs):
            if not created_paths:
                tmp = original_named_temporary_file(*args, **kwargs)
                created_paths.append(tmp.name)
                return tmp
            raise OSError("tempfile setup failed")

        with patch("atomic_image_builder.tempfile.NamedTemporaryFile", side_effect=fake_named_temporary_file):
            with self.assertRaisesRegex(OSError, "tempfile setup failed"):
                gum.spinner_result("Checking package name", ["true"])

        self.assertEqual(len(created_paths), 1)
        try:
            self.assertFalse(Path(created_paths[0]).exists())
        finally:
            for path in created_paths:
                Path(path).unlink(missing_ok=True)

    def test_gum_require_spinner_success_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        proc = subprocess.CompletedProcess(["gum", "spin"], 130, "", "")
        with self.assertRaises(KeyboardInterrupt):
            gum.require_spinner_success(proc, ["gum", "spin"])

    def test_gum_require_spinner_success_raises_command_error_on_other_failure(self) -> None:
        gum = Gum()
        proc = subprocess.CompletedProcess(["gum", "spin"], 1, "", "")
        with self.assertRaisesRegex(CommandError, "gum spin"):
            gum.require_spinner_success(proc, ["gum", "spin", "--title", "x"])

    def test_gum_spinner_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gum", "spin"], 130, "", "")):
            with self.assertRaises(KeyboardInterrupt):
                gum.spinner("Working...", ["true"])

    def test_gum_spinner_capture_raises_command_error_when_gum_fails(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gum", "spin"], 1, "", "")):
            with self.assertRaises(CommandError):
                gum.spinner_capture("Working...", ["true"])

    def test_gum_spinner_capture_returns_captured_output_on_success(self) -> None:
        gum = Gum()
        original_named_temporary_file = tempfile.NamedTemporaryFile
        created_paths: list[str] = []

        def fake_named_temporary_file(*args, **kwargs):
            tmp = original_named_temporary_file(*args, **kwargs)
            created_paths.append(tmp.name)
            return tmp

        def fake_run(_args, **_kwargs):
            Path(created_paths[-1]).write_text("captured output\n")
            return subprocess.CompletedProcess(["gum", "spin"], 0, "", "")

        with patch("atomic_image_builder.tempfile.NamedTemporaryFile", side_effect=fake_named_temporary_file):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                output = gum.spinner_capture("Working...", ["true"])

        self.assertEqual(output, "captured output\n")
        self.assertFalse(Path(created_paths[-1]).exists())

    def test_gum_spinner_result_raises_keyboard_interrupt_when_gum_itself_is_interrupted(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gum", "spin"], 130, "", "")):
            with self.assertRaises(KeyboardInterrupt):
                gum.spinner_result("Working...", ["true"])

    def test_search_packages_can_remove_previously_selected_match(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([("fish", "Friendly interactive shell")], False, None)):
            app.search_packages()

        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, ["Removed 1 package(s). Press Enter to return to the package menu..."])

    def test_select_packages_allows_remove_path_in_create_flow(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]
        choices = ["Remove selected packages", "Continue to review"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "choose_to_remove", return_value=[]) as remove_mock:
            app.select_packages()

        remove_mock.assert_called_once_with(["fish"], "Remove Packages")
        self.assertEqual(app.config.packages, [])

    def test_select_packages_allows_remove_copr_and_service_paths_in_create_flow(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        app.config.services = ["sshd.service"]
        choices = ["Remove COPR repositories", "Remove enabled services", "Continue to review"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "choose_to_remove", side_effect=[[], []]) as remove_mock:
            app.select_packages()

        self.assertEqual(remove_mock.call_args_list[0].args, (["foo/bar"], "Remove COPR Repositories"))
        self.assertEqual(remove_mock.call_args_list[1].args, (["sshd.service"], "Remove Services"))
        self.assertEqual(app.config.copr_repos, [])
        self.assertEqual(app.config.services, [])

    def test_select_packages_shows_requested_package_note(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Continue to review"]
        app.gum = stub
        app.select_packages()

        hints = [msg for level, msg in app.gum.messages if level == "hint"]
        self.assertIn(app.requested_packages_note(), hints)

    def test_manual_packages_pauses_after_successful_add(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "tmux"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(app.gum.prompts, ["Added 1 package(s). Press Enter to return to the package menu..."])

    def test_manual_packages_pauses_after_failed_add(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "nethock"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: False for p in pkgs}):
            app.manual_packages()
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, ["No packages were added. Press Enter to return to the package menu..."])

    def test_manual_packages_accepts_comma_separated_entry(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "tmux,htop, vim"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux", "htop", "vim"])

    def test_manual_packages_returns_silently_when_input_is_empty(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "   "
        app.gum = stub
        app.manual_packages()
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, [])

    def test_manual_packages_pauses_with_missing_hint_when_some_packages_are_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "tmux nethock"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {"tmux": True, "nethock": False}):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(
            app.gum.prompts,
            ["Finished checking package names. Press Enter to return to the package menu..."],
        )

    def test_select_common_services_replaces_curated_selection_only(self) -> None:
        app = self.make_app()
        app.config.services = ["custom.service", COMMON_SERVICES[0][1]]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [f"{COMMON_SERVICES[1][0]} ({COMMON_SERVICES[1][1]})"]
        app.gum = stub
        app.select_common_services()
        self.assertEqual(app.config.services, ["custom.service", COMMON_SERVICES[1][1]])

    def test_select_common_services_back_is_noop(self) -> None:
        app = self.make_app()
        app.config.services = ["custom.service", COMMON_SERVICES[0][1]]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        app.select_common_services()
        self.assertEqual(app.config.services, ["custom.service", COMMON_SERVICES[0][1]])

    def test_do_build_validates_before_creating_repo(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.config.packages = ["bad;rm"]
        with patch("atomic_image_builder.run") as run_mock:
            with self.assertRaisesRegex(CommandError, "Invalid package value"):
                app.do_build()
        run_mock.assert_not_called()

    def test_do_build_requires_cosign_before_creating_repo(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        with patch("atomic_image_builder.command_exists", side_effect=lambda name: False if name == "cosign" else True):
            with patch("atomic_image_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "repo", "view"], 1, "", "")
                with self.assertRaisesRegex(CommandError, "SIGNING_SECRET"):
                    app.do_build()
        self.assertTrue(all(call.args[0][:3] != ["gh", "repo", "create"] for call in run_mock.call_args_list))

    def test_do_build_deletes_repo_if_setup_fails_after_creation(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                    with self.assertRaisesRegex(CommandError, "signing failed"):
                        app.do_build()
        self.assertIn(["gh", "repo", "delete", "example/test-image", "--yes"], run_calls)

    def test_do_build_never_deletes_a_repo_it_already_pushed_to(self) -> None:
        # The cleanup exists to remove an *empty* repo after a failed setup.
        # Once the push lands, that repo holds the user's configuration, and
        # deleting it on the way out of a late failure would destroy work the
        # tool had already completed. The pushed flag is the only thing
        # standing between those two outcomes.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        created: list[str] = []

        class TempDirThatFailsToClean:
            # Cleanup runs after the push has already succeeded, so anything
            # it raises arrives with pushed=True.
            def __init__(self, prefix: str = "") -> None:
                self._path = tempfile.mkdtemp(prefix=prefix)
                created.append(self._path)

            def __enter__(self) -> str:
                return self._path

            def __exit__(self, *_exc: object) -> bool:
                raise OSError("could not remove the temporary directory")

        try:
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch("atomic_image_builder.tempfile.TemporaryDirectory", TempDirThatFailsToClean):
                            with self.assertRaises(OSError):
                                with redirect_stdout(io.StringIO()):
                                    app.do_build()
        finally:
            for path in created:
                shutil.rmtree(path, ignore_errors=True)

        deletes = [call for call in run_calls if call[:3] == ["gh", "repo", "delete"]]
        self.assertEqual(deletes, [])
        self.assertFalse(any("Removing the empty repo" in msg for _level, msg in app.gum.messages))

    def test_do_build_hints_manual_delete_when_auto_cleanup_fails_for_unrelated_reason(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(list(args), 1, "", "HTTP 500: something went wrong")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                    with self.assertRaisesRegex(CommandError, "signing failed"):
                        app.do_build()

        hints = [message for level, message in app.gum.messages if level == "hint"]
        self.assertIn("Delete the repo manually on GitHub before trying again.", hints)
        self.assertFalse(any("delete_repo" in hint for hint in hints))

    def test_do_build_sets_local_git_identity_before_initial_commit(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", return_value=True):
                    with patch.object(app, "repo_default_branch", return_value="main"):
                        with patch.object(app, "seed_project_template", return_value=None):
                            with patch.object(app, "write_project_files", return_value=None):
                                self.assertTrue(app.do_build())

        self.assertIn(["git", "config", "user.name", "example"], run_calls)
        self.assertIn(["git", "config", "user.email", "example@users.noreply.github.com"], run_calls)
        self.assertIn(["git", "commit", "-m", f"Initial image configuration via {TOOL_SLUG}"], run_calls)

    def test_do_build_warns_about_hand_edited_managed_repos(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", return_value=True):
                    with patch.object(app, "repo_default_branch", return_value="main"):
                        with patch.object(app, "seed_project_template", return_value=None):
                            with patch.object(app, "write_project_files", return_value=None):
                                self.assertTrue(app.do_build())

        self.assertIn(("warn", MANAGED_REPO_WARNING), app.gum.messages)

    def test_do_build_shows_reset_hint_after_scanned_import(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.config.scanned_packages = ["tmux"]
        app.config.packages = ["tmux"]
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        output = io.StringIO()
        with redirect_stdout(output):
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "repo_default_branch", return_value="main"):
                            with patch.object(app, "seed_project_template", return_value=None):
                                with patch.object(app, "write_project_files", return_value=None):
                                    self.assertTrue(app.do_build())

        self.assertIn("Scheduled rebuilds also run daily at about", output.getvalue())
        self.assertIn("sudo rpm-ostree reset", output.getvalue())
        # The panel is the first place the command is seen, so it carries the
        # same qualification the README does rather than deferring to it.
        self.assertIn("not only the ones this image reproduces", output.getvalue())

    def test_do_build_omits_reset_hint_for_normal_build(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        output = io.StringIO()
        with redirect_stdout(output):
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "repo_default_branch", return_value="main"):
                            with patch.object(app, "seed_project_template", return_value=None):
                                with patch.object(app, "write_project_files", return_value=None):
                                    self.assertTrue(app.do_build())

        self.assertIn("Scheduled rebuilds also run daily at about", output.getvalue())
        self.assertNotIn("sudo rpm-ostree reset", output.getvalue())
        # The completion panel is where someone is told the build has started,
        # so it is where the step between a green build and a working switch
        # belongs -- there is no package to check yet at this point.
        self.assertIn("Make it public before switching", output.getvalue())
        self.assertIn("pkgs/container/test-image", output.getvalue())

    def test_do_build_summary_uses_lowercase_ghcr_owner(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "ExampleUser"
        app.config.github_user = "ExampleUser"
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        output = io.StringIO()
        with redirect_stdout(output):
            with patch("atomic_image_builder.command_exists", return_value=True):
                with patch("atomic_image_builder.run", side_effect=fake_run):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "repo_default_branch", return_value="main"):
                            with patch.object(app, "seed_project_template", return_value=None):
                                with patch.object(app, "write_project_files", return_value=None):
                                    self.assertTrue(app.do_build())

        rendered = output.getvalue()
        self.assertIn("ghcr.io/exampleuser/test-image:latest", rendered)
        self.assertNotIn("ghcr.io/ExampleUser/test-image:latest", rendered)

    def test_do_build_explains_manual_cleanup_when_delete_scope_is_missing(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(
                    list(args),
                    1,
                    "",
                    'HTTP 403: Must have admin rights to Repository.\nThis API operation needs the "delete_repo" scope.',
                )
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                    with self.assertRaisesRegex(CommandError, "signing failed"):
                        app.do_build()

        self.assertTrue(any("delete_repo scope" in message for level, message in app.gum.messages if level == "hint"))

    def test_do_build_deletes_new_repo_when_interrupted_before_first_push(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", side_effect=KeyboardInterrupt()):
                    with self.assertRaises(KeyboardInterrupt):
                        app.do_build()

        self.assertIn(["gh", "repo", "delete", "example/test-image", "--yes"], run_calls)
        self.assertTrue(any("Removing the empty repo" in message for level, message in app.gum.messages if level == "warn"))

    def test_do_build_returns_false_without_completing_github_setup(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "require_github", return_value=False):
            with patch("atomic_image_builder.run") as run_mock:
                self.assertFalse(app.do_build())
        run_mock.assert_not_called()

    def test_ghcr_package_exists_true_only_for_a_package_it_could_read(self) -> None:
        class FakeResponse:
            def __init__(self, status: int, payload: str = "") -> None:
                self.status = status
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        responses = [FakeResponse(200, '{"token": "t"}'), FakeResponse(200, "{}")]
        with patch("urllib.request.urlopen", side_effect=responses):
            self.assertTrue(REAL_GHCR_PACKAGE_EXISTS("Danathar", "Bazzite-DX-Test"))

        # A package the anonymous pull cannot read is reported as absent: a
        # private one is indistinguishable from a missing one this way. urlopen
        # raises for a non-2xx status rather than returning one, so the denial
        # has to arrive as HTTPError to exercise the real path.
        denied = urllib.error.HTTPError("https://ghcr.io/v2/owner/name/tags/list", 403, "denied", {}, None)
        with patch("urllib.request.urlopen", side_effect=[FakeResponse(200, '{"token": "t"}'), denied]):
            self.assertFalse(REAL_GHCR_PACKAGE_EXISTS("owner", "name"))

        # http.client's exceptions descend from HTTPException, not OSError or
        # ValueError. An advisory probe must not let one stop a repo build.
        for failure in (
            http.client.IncompleteRead(b"partial"),
            http.client.BadStatusLine("garbage"),
            http.client.LineTooLong("header line"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch("urllib.request.urlopen", side_effect=[FakeResponse(200, '{"token": "t"}'), failure]):
                    self.assertFalse(REAL_GHCR_PACKAGE_EXISTS("owner", "name"))

        # No usable token, and any transport failure, both mean "do not warn".
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, "{}")):
            self.assertFalse(REAL_GHCR_PACKAGE_EXISTS("owner", "name"))
        with patch("urllib.request.urlopen", side_effect=OSError("no route to host")):
            self.assertFalse(REAL_GHCR_PACKAGE_EXISTS("owner", "name"))

    def test_ghcr_package_exists_lowercases_the_path_for_the_registry(self) -> None:
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise OSError("stop after the first call")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            REAL_GHCR_PACKAGE_EXISTS("Danathar", "Bazzite-DX-Test")
        self.assertEqual(len(seen), 1)
        self.assertIn("danathar/bazzite-dx-test", seen[0])
        self.assertNotIn("Danathar", seen[0])

    def test_confirm_ghcr_package_conflict_is_silent_when_nothing_conflicts(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch("atomic_image_builder.ghcr_package_exists", return_value=False):
            self.assertTrue(app.confirm_ghcr_package_conflict("example", "test-image"))
        self.assertEqual(app.gum.messages, [])

    def test_confirm_ghcr_package_conflict_explains_the_fix_and_defaults_to_no(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        prompts: list[tuple[str, bool]] = []

        def fake_confirm(prompt, *, default=True):
            prompts.append((prompt, default))
            return default

        app.gum.confirm = fake_confirm
        with patch("atomic_image_builder.ghcr_package_exists", return_value=True):
            with redirect_stdout(io.StringIO()):
                proceed = app.confirm_ghcr_package_conflict("Example", "Test-Image")

        # GumStub.confirm returns the default, so the default must be "no".
        self.assertFalse(proceed)
        self.assertEqual(prompts[0][1], False)
        self.assertIn("Create Example/Test-Image anyway?", prompts[0][0])
        warnings = [message for level, message in app.gum.messages if level == "warn"]
        self.assertTrue(any("ghcr.io/example/test-image" in message for message in warnings))
        hints = " ".join(message for level, message in app.gum.messages if level == "hint")
        self.assertIn("does not delete its packages", hints)
        self.assertIn("packages/container/test-image/settings", hints)
        # The link-the-repo remedy is impossible until the repo exists, so the
        # hint must say so rather than sending the user to a dead end.
        self.assertIn("only", hints)
        self.assertIn("lists repos that already exist", hints)

    def test_do_build_does_not_create_the_repo_when_the_package_conflict_is_declined(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "repo", "view"], 1, "", "")
                with patch.object(app, "confirm_ghcr_package_conflict", return_value=False) as conflict:
                    self.assertFalse(app.do_build())

        conflict.assert_called_once_with("example", "test-image")
        self.assertTrue(all(call.args[0][:3] != ["gh", "repo", "create"] for call in run_mock.call_args_list))
        self.assertIn("Press Enter to go back to the review screen...", app.gum.prompts)

    def test_do_build_stops_and_hints_update_flow_when_repo_was_created_by_this_tool(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "repo", "view"], 0, "", "")
                with patch.object(app, "repo_has_state_file", return_value=True):
                    self.assertFalse(app.do_build())

        self.assertIn(("error", "example/test-image already exists on GitHub."), app.gum.messages)
        self.assertTrue(any("Update Existing Image" in message for level, message in app.gum.messages if level == "hint"))
        self.assertIn("Press Enter to go back to the review screen...", app.gum.prompts)
        self.assertTrue(all(call.args[0][:3] != ["gh", "repo", "create"] for call in run_mock.call_args_list))

    def test_do_build_stops_and_hints_manual_management_when_repo_is_foreign(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.gum = GumStub()

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "repo", "view"], 0, "", "")
                with patch.object(app, "repo_has_state_file", return_value=False):
                    self.assertFalse(app.do_build())

        self.assertTrue(any("was not created by this tool" in message for level, message in app.gum.messages if level == "hint"))
        self.assertTrue(any("only updates repos it created itself" in message for level, message in app.gum.messages if level == "hint"))

    def test_push_update_sets_local_git_identity_before_commit(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([False, True])
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: next(confirm_results)
        app.gum = stub

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertIn(["git", "config", "user.name", "example"], run_calls)
        self.assertIn(["git", "config", "user.email", "example@users.noreply.github.com"], run_calls)
        self.assertIn(["git", "commit", "-m", f"Update image configuration via {TOOL_SLUG} v{VERSION}"], run_calls)

    def test_push_update_does_not_configure_signing_until_push_is_confirmed(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([False, False])
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: next(confirm_results)
        app.gum = stub

        def fake_run(args, **_kwargs):
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready") as ensure_mock:
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)
        ensure_mock.assert_not_called()

    def test_push_update_reconfirms_when_signing_changes_the_final_diff(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_prompts: list[str] = []
        confirm_results = iter([False, True, False, False])
        stub = GumStub()

        def fake_confirm(prompt, default=False):
            confirm_prompts.append(prompt)
            return next(confirm_results)

        stub.confirm = fake_confirm
        app.gum = stub

        diff_calls = {"count": 0}
        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if list(args) == ["git", "diff", "--stat"]:
                diff_calls["count"] += 1
                if diff_calls["count"] == 1:
                    return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n cosign.pub | 1 +\n", "")
            if list(args) == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
                return subprocess.CompletedProcess(
                    list(args), 0, "" if diff_calls["count"] == 1 else "cosign.pub\0", ""
                )
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertTrue(any("final update changed" in message for level, message in app.gum.messages if level == "warn"))
        self.assertIn("Push final changes to example/test-image?", confirm_prompts)
        self.assertTrue(all(call[:2] != ["git", "add"] for call in run_calls))

    def test_push_update_warns_about_hand_edited_managed_repos(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([False, True])
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: next(confirm_results)
        app.gum = stub

        def fake_run(args, **_kwargs):
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertIn(("warn", MANAGED_REPO_WARNING), app.gum.messages)

    def test_push_update_returns_when_no_changes_detected(self) -> None:
        # An empty pre-signing diff must end the flow before any confirm,
        # signing setup, or git write happens.
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        stub = GumStub()
        confirm_prompts: list[str] = []
        stub.confirm = lambda prompt, default=False: confirm_prompts.append(prompt) or default
        app.gum = stub

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready") as ensure_mock:
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertIn(("warn", "No changes detected."), stub.messages)
        self.assertEqual(confirm_prompts, [])
        ensure_mock.assert_not_called()
        self.assertTrue(all(call[:2] not in (["git", "add"], ["git", "push"]) for call in run_calls))

    def test_push_update_shows_full_diff_when_requested(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([True, True])
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: next(confirm_results)
        paged: list[str] = []
        stub.pager = lambda text: paged.append(text)
        app.gum = stub

        def fake_run(args, **_kwargs):
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertEqual(len(paged), 1)
        self.assertIn("diff --git a/x b/x", paged[0])
        self.assertTrue(any(level == "success" and "Pushed changes" in message for level, message in stub.messages))

    def test_push_update_returns_when_signing_leaves_no_changes(self) -> None:
        # If regenerating with signing produces an empty diff there is nothing
        # to push; the flow must stop before committing.
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([False, True])
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: next(confirm_results)
        app.gum = stub

        diff_calls = {"count": 0}
        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if list(args) == ["git", "diff", "--stat"]:
                diff_calls["count"] += 1
                if diff_calls["count"] == 1:
                    return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        self.assertIn(("warn", "No changes detected."), stub.messages)
        self.assertTrue(all(call[:2] not in (["git", "add"], ["git", "push"]) for call in run_calls))

    def test_push_update_shows_final_full_diff_before_reconfirming(self) -> None:
        # When signing changes the diff, asking to view the final full diff
        # must show it, and the push must still wait for the re-confirm.
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"
        confirm_results = iter([False, True, True, True])
        stub = GumStub()
        events: list[tuple[str, str]] = []

        def fake_confirm(prompt, default=False):
            events.append(("confirm", prompt))
            return next(confirm_results)

        stub.confirm = fake_confirm
        stub.pager = lambda text: events.append(("pager", text))
        app.gum = stub

        diff_calls = {"count": 0}
        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if list(args) == ["git", "diff", "--stat"]:
                diff_calls["count"] += 1
                if diff_calls["count"] == 1:
                    return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n cosign.pub | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/cosign.pub b/cosign.pub\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "ensure_signing_ready", return_value=True):
                        with patch.object(app, "write_project_files", return_value=None):
                            app.push_update("example", "test-image", repo_dir)

        paged = [text for kind, text in events if kind == "pager"]
        self.assertEqual(len(paged), 1)
        self.assertIn("diff --git a/cosign.pub b/cosign.pub", paged[0])
        # The final full diff must be shown before the re-confirm is asked, so
        # the user approves what will actually be pushed.
        pager_index = events.index(("pager", paged[0]))
        reconfirm_index = events.index(("confirm", "Push final changes to example/test-image?"))
        self.assertLess(pager_index, reconfirm_index)
        self.assertIn(["git", "push", "origin", "HEAD"], run_calls)
        self.assertTrue(any(level == "success" and "Pushed changes" in message for level, message in stub.messages))

    def test_create_new_image_starts_from_fresh_config(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["tmux"]
        app.config.services = ["sshd.service"]
        app.config.repo_name = "old-repo"
        seen: dict[str, object] = {}
        method_calls = {"count": 0}

        def fake_choose_base_image(**_kwargs):
            seen["packages"] = list(app.config.packages)
            seen["services"] = list(app.config.services)
            seen["repo_name"] = app.config.repo_name
            seen["github_user"] = app.config.github_user
            raise ScreenBack()

        def fake_choose_method(**_kwargs):
            method_calls["count"] += 1
            if method_calls["count"] > 1:
                raise ScreenBack()
            app.config.method = "containerfile"

        with patch.object(app, "choose_method", side_effect=fake_choose_method):
            with patch.object(app, "choose_base_image", side_effect=fake_choose_base_image):
                app.create_new_image()

        self.assertEqual(seen["packages"], [])
        self.assertEqual(seen["services"], [])
        self.assertEqual(seen["repo_name"], "")
        self.assertEqual(seen["github_user"], "example")

    def test_create_new_image_recovers_from_do_build_command_error(self) -> None:
        # A CommandError from do_build() (e.g. a failed signing-secret upload)
        # must return to the review screen with wizard state intact, not take
        # the whole app down.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        review_actions = iter(["build", "cancel"])

        def fake_configure_repo(**_kwargs):
            app.config.repo_name = "sentinel-repo"

        with patch.object(app, "choose_method", return_value=None):
            with patch.object(app, "choose_base_image", return_value=None):
                with patch.object(app, "configure_repo", side_effect=fake_configure_repo):
                    with patch.object(app, "select_packages", return_value=None):
                        with patch.object(app, "review_new_image", side_effect=lambda **_kwargs: next(review_actions)):
                            with patch.object(app, "do_build", side_effect=CommandError("build boom")):
                                app.create_new_image()

        self.assertIn(("error", "build boom"), stub.messages)
        self.assertTrue(stub.prompts)
        self.assertEqual(app.config.repo_name, "sentinel-repo")

    def test_create_new_image_returns_to_review_when_build_declines(self) -> None:
        # do_build() returning False is a decision, not a failure: the user
        # backed out at its own confirmation. The wizard has to hand them back
        # the review screen with everything still filled in, rather than
        # treating the answer as "done" and dropping the whole config.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        review_actions = iter(["build", "cancel"])

        def fake_configure_repo(**_kwargs):
            app.config.repo_name = "sentinel-repo"

        with patch.object(app, "choose_method", return_value=None):
            with patch.object(app, "choose_base_image", return_value=None):
                with patch.object(app, "configure_repo", side_effect=fake_configure_repo):
                    with patch.object(app, "select_packages", return_value=None):
                        with patch.object(app, "review_new_image", side_effect=lambda **_kwargs: next(review_actions)):
                            with patch.object(app, "do_build", return_value=False) as build_mock:
                                app.create_new_image()

        build_mock.assert_called_once()
        self.assertEqual(app.config.repo_name, "sentinel-repo")
        self.assertFalse(any(level == "error" for level, _message in stub.messages))

    def test_create_new_image_returns_after_successful_build(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "choose_method", return_value=None):
            with patch.object(app, "choose_base_image", return_value=None):
                with patch.object(app, "configure_repo", return_value=None):
                    with patch.object(app, "select_packages", return_value=None):
                        # A single review action: if the wizard looped back to
                        # review after a successful build, next() would raise
                        # StopIteration and fail the test.
                        with patch.object(app, "review_new_image", side_effect=iter(["build"])):
                            with patch.object(app, "do_build", return_value=True) as build_mock:
                                app.create_new_image()
        build_mock.assert_called_once()

    def test_scanned_wizard_never_asks_for_a_base_image(self) -> None:
        # The scan read the base off the running system. Universal Blue images
        # are not rebase-compatible, so offering a different one would build an
        # image the user cannot switch to.
        app = self.make_app()
        app.gum = GumStub()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable"
        app.config.base_image_name = "Bazzite (KDE)"

        with patch.object(app, "choose_method"):
            with patch.object(app, "choose_base_image") as base_mock:
                with patch.object(app, "configure_repo"):
                    with patch.object(app, "select_packages"):
                        with patch.object(app, "review_new_image", side_effect=iter(["cancel"])) as review:
                            app.create_new_image(scanned=True)

        base_mock.assert_not_called()
        self.assertFalse(review.call_args.kwargs["allow_base_edit"])

    def test_unscanned_wizard_still_asks_for_a_base_image(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "choose_method"):
            with patch.object(app, "choose_base_image") as base_mock:
                with patch.object(app, "configure_repo"):
                    with patch.object(app, "select_packages"):
                        with patch.object(app, "review_new_image", side_effect=iter(["cancel"])) as review:
                            app.create_new_image()

        base_mock.assert_called_once()
        self.assertTrue(review.call_args.kwargs["allow_base_edit"])

    def test_scanned_wizard_numbers_its_steps_without_the_skipped_one(self) -> None:
        # "Step 3 of 5" when there are only four screens is a bug the user sees.
        app = self.make_app()
        app.gum = GumStub()
        seen: list[tuple[int, int]] = []

        def record(**kwargs):
            seen.append((kwargs["step"], kwargs["total_steps"]))

        with patch.object(app, "choose_method", side_effect=record):
            with patch.object(app, "configure_repo", side_effect=record):
                with patch.object(app, "select_packages", side_effect=record):
                    with patch.object(app, "review_new_image", side_effect=lambda **kw: (record(**kw), "cancel")[1]):
                        app.create_new_image(scanned=True)

        self.assertEqual(seen, [(1, 4), (2, 4), (3, 4), (4, 4)])

    def test_scanned_wizard_back_navigation_skips_the_base_step(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        calls: list[str] = []
        repo_attempts = iter([ScreenBack(), None])

        def repo(**_kwargs):
            calls.append("repo")
            outcome = next(repo_attempts)
            if outcome is not None:
                raise outcome

        with patch.object(app, "choose_method", side_effect=lambda **_k: calls.append("method")):
            with patch.object(app, "choose_base_image", side_effect=lambda **_k: calls.append("base")):
                with patch.object(app, "configure_repo", side_effect=repo):
                    with patch.object(app, "select_packages", side_effect=lambda **_k: calls.append("software")):
                        with patch.object(app, "review_new_image", side_effect=iter(["cancel"])):
                            app.create_new_image(scanned=True)

        # Backing out of repo returns to method, not to a base step that is
        # not part of this flow.
        self.assertEqual(calls, ["method", "repo", "method", "repo", "software"])

    def scanned_fedora_status(self) -> str:
        return json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": (
                            "ostree-unverified-registry:quay.io/fedora-ostree-desktops/silverblue:"
                            f"{FEDORA_ATOMIC_DEFAULT_TAG}"
                        ),
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )

    def run_scanned_fedora_create_image(self, *, brew_answer: bool) -> App:
        """The real Create Image flow on a Fedora Atomic host, up to the build.

        Nothing is mocked between scan_os() and do_build(): the wizard runs its
        own screens against a Gum stub, which is what makes the missing
        Homebrew question visible at all -- patching create_new_image, as the
        flow tests above do, is exactly what hid it.
        """
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()

        def choose(options, **_kwargs):
            for wanted in ("Containerfile", "Continue to review", "Start GitHub build"):
                match = [option for option in options if option.startswith(wanted)]
                if match:
                    return [match[0]]
            raise AssertionError(f"unexpected menu: {options}")

        def confirm(prompt, default=False):
            if "Homebrew" in prompt:
                return brew_answer
            return True

        stub.choose = choose
        stub.confirm = confirm
        stub.input = lambda **_kwargs: ""
        app.gum = stub

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(self.scanned_fedora_status())
            with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                with patch.object(app, "do_build", return_value=True) as build:
                    with redirect_stdout(io.StringIO()):
                        app.create_image()
        self.assertTrue(build.called, "the flow never reached the build")
        return app

    def test_scanned_fedora_run_offers_homebrew_before_the_first_build(self) -> None:
        # The reported defect: create_new_image(scanned=True) drops the base
        # step, and the Homebrew question was attached to that screen, so a
        # supported Fedora Atomic host reached its first build with Homebrew
        # off and nothing having asked. The workaround was to create the repo
        # and then turn it on through Update Existing Image, which misses the
        # build that just ran.
        app = self.run_scanned_fedora_create_image(brew_answer=True)
        self.assertTrue(app.config.brew_enabled)
        self.assertEqual(app.config.base_image_name, "Fedora Silverblue")

    def test_scanned_fedora_run_honours_a_declined_homebrew_answer(self) -> None:
        app = self.run_scanned_fedora_create_image(brew_answer=False)
        self.assertFalse(app.config.brew_enabled)

    def test_scanned_universal_blue_run_does_not_ask_about_homebrew(self) -> None:
        # Universal Blue images already ship Homebrew, so the step is not in
        # the scanned wizard at all rather than being asked and ignored.
        app = self.make_app()
        app.gum = GumStub()
        seen: list[str] = []
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable"

        def record(name):
            return lambda **_kwargs: seen.append(name)

        with patch.object(app, "choose_method", side_effect=record("method")):
            with patch.object(app, "offer_brew_if_applicable", side_effect=record("brew")):
                with patch.object(app, "configure_repo", side_effect=record("repo")):
                    with patch.object(app, "select_packages", side_effect=record("software")):
                        with patch.object(app, "review_new_image", return_value="cancel"):
                            app.create_new_image(scanned=True)

        self.assertEqual(seen, ["method", "repo", "software"])

    def test_scanned_fedora_wizard_numbers_the_homebrew_step(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        seen: list[tuple[str, int, int]] = []

        def record(name):
            return lambda **kwargs: seen.append((name, kwargs["step"], kwargs["total_steps"]))

        with patch.object(app, "choose_method", side_effect=record("method")):
            with patch.object(app, "offer_brew_if_applicable", side_effect=record("brew")):
                with patch.object(app, "configure_repo", side_effect=record("repo")):
                    with patch.object(app, "select_packages", side_effect=record("software")):
                        with patch.object(app, "review_new_image", side_effect=lambda **kw: (record("review")(**kw), "cancel")[1]):
                            app.create_new_image(scanned=True)

        self.assertEqual(
            seen,
            [("method", 1, 5), ("brew", 2, 5), ("repo", 3, 5), ("software", 4, 5), ("review", 5, 5)],
        )

    def test_offer_brew_if_applicable_renders_a_step_header_when_it_is_a_step(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        stub = GumStub()
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.offer_brew_if_applicable(step=2, total_steps=5)
        self.assertIn(("hint", "Step 2 of 5."), stub.messages)

    def test_scanned_wizard_back_navigation_returns_to_the_homebrew_step(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        calls: list[str] = []
        repo_attempts = iter([ScreenBack(), None])

        def repo(**_kwargs):
            calls.append("repo")
            outcome = next(repo_attempts)
            if outcome is not None:
                raise outcome

        with patch.object(app, "choose_method", side_effect=lambda **_k: calls.append("method")):
            with patch.object(app, "offer_brew_if_applicable", side_effect=lambda **_k: calls.append("brew")):
                with patch.object(app, "configure_repo", side_effect=repo):
                    with patch.object(app, "select_packages", side_effect=lambda **_k: calls.append("software")):
                        with patch.object(app, "review_new_image", return_value="cancel"):
                            app.create_new_image(scanned=True)

        self.assertEqual(calls, ["method", "brew", "repo", "brew", "repo", "software"])

    def test_review_offers_homebrew_for_a_fedora_base_and_edits_it_in_place(self) -> None:
        # The other half: a scanned run shows the detected base as a fact, so
        # review is the only screen left that could change this. It reported
        # "Not included" with no way to act on it.
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.base_image_name = "Fedora Silverblue"
        stub = GumStub()
        offered: list[str] = []

        def choose(options, **_kwargs):
            offered.extend(options)
            return [next(option for option in options if option.startswith("Homebrew"))]

        stub.choose = choose
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            action = app.review_new_image(step=5, total_steps=5, allow_base_edit=False)

        self.assertEqual(action, "brew")
        self.assertTrue(
            any(option.startswith("Homebrew") and "Not included" in option for option in offered),
            offered,
        )

    def test_review_hides_homebrew_for_a_universal_blue_base(self) -> None:
        # Same rule the update menu's task list uses: the choice exists only
        # when the base does not already provide it.
        app = self.make_app()
        stub = GumStub()
        offered: list[str] = []

        def choose(options, **_kwargs):
            offered.extend(options)
            return [options[-1]]

        stub.choose = choose
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.review_new_image(step=4, total_steps=4, allow_base_edit=False)
        self.assertFalse([option for option in offered if option.startswith("Homebrew")], offered)

    def test_review_homebrew_action_runs_in_place_and_returns_to_review(self) -> None:
        # "brew" is not a wizard step on the manual-base path, so the action
        # is handled in place rather than jumped to -- returning to review
        # either way, never falling through to cancel.
        app = self.make_app()
        app.gum = GumStub()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        actions = iter(["brew", "cancel"])
        brew_calls: list[None] = []

        with patch.object(app, "choose_method"):
            with patch.object(app, "choose_base_image"):
                with patch.object(app, "configure_repo"):
                    with patch.object(app, "select_packages"):
                        with patch.object(app, "offer_brew_if_applicable", side_effect=lambda **_k: brew_calls.append(None)):
                            with patch.object(app, "review_new_image", side_effect=lambda **_k: next(actions)):
                                app.create_new_image()

        self.assertEqual(len(brew_calls), 1)

    def test_review_screen_shows_the_detected_base_when_it_cannot_be_edited(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda options, **_kw: [options[-1]]
        app.gum = stub
        app.config.base_image_name = "Bazzite (KDE)"
        with redirect_stdout(io.StringIO()):
            app.review_new_image(step=4, total_steps=4, allow_base_edit=False)
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("Bazzite (KDE)", hints)
        self.assertIn("detected from your running system", hints)

    def test_create_new_image_review_actions_route_to_matching_step(self) -> None:
        # Each review action must jump back to its own wizard step; a wrong
        # mapping would re-run the wrong screen after a partial edit.
        app = self.make_app()
        app.gum = GumStub()
        events: list[str] = []
        review_actions = iter(["method", "base", "repo", "software", "cancel"])

        def record(name):
            return lambda **_kwargs: events.append(name)

        def fake_review(**_kwargs):
            events.append("review")
            return next(review_actions)

        with patch.object(app, "choose_method", side_effect=record("method")):
            with patch.object(app, "choose_base_image", side_effect=record("base")):
                with patch.object(app, "configure_repo", side_effect=record("repo")):
                    with patch.object(app, "select_packages", side_effect=record("software")):
                        with patch.object(app, "review_new_image", side_effect=fake_review):
                            app.create_new_image()

        # The step run immediately after each review is the one the action
        # named; the wizard then walks forward to review again.
        resumed_steps = [
            events[i + 1]
            for i, name in enumerate(events)
            if name == "review" and i + 1 < len(events)
        ]
        self.assertEqual(resumed_steps, ["method", "base", "repo", "software"])
        self.assertEqual(events[-1], "review")

    def test_main_menu_recovers_from_command_error(self) -> None:
        # A CommandError raised by any dispatched action must be reported and
        # return to the main menu instead of propagating out of the app.
        app = self.make_app()
        stub = GumStub()
        choices = ["Create Image", "Quit"]
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "create_image", side_effect=CommandError("menu boom")):
            with self.assertRaises(SystemExit):
                app.main_menu()

        self.assertIn(("error", "menu boom"), stub.messages)
        self.assertTrue(stub.prompts)

    def test_main_menu_esc_exits_the_app(self) -> None:
        # Esc at the top-level menu is the documented way out, so it must exit
        # rather than loop forever.
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        with self.assertRaises(SystemExit):
            app.main_menu()

    def test_main_menu_empty_choice_quits(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with self.assertRaises(SystemExit):
            app.main_menu()

    def test_main_menu_scan_os_success_starts_scanned_wizard(self) -> None:
        # A successful scan hands its findings to the wizard via scanned=True,
        # which is what preserves the detected host state.
        app = self.make_app()
        stub = GumStub()
        choices = ["Create Image", "Quit"]
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_OK):
            with patch.object(app, "create_new_image") as create_mock:
                with self.assertRaises(SystemExit):
                    app.main_menu()
        create_mock.assert_called_once_with(scanned=True)

    def test_create_image_falls_back_to_manual_base_when_scanning_is_impossible(self) -> None:
        # A bare `podman run` has no host state to read. That is the one case
        # where the base genuinely is a choice, so the flow offers it there
        # rather than in the menu.
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_UNAVAILABLE):
            with patch.object(app, "create_new_image") as create_mock:
                with redirect_stdout(io.StringIO()):
                    app.create_image()
        create_mock.assert_called_once_with()
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("chosen by hand", hints)

    def test_create_image_declining_the_fallback_starts_nothing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, **_kwargs: False
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_UNAVAILABLE):
            with patch.object(app, "create_new_image") as create_mock:
                with redirect_stdout(io.StringIO()):
                    app.create_image()
        create_mock.assert_not_called()

    def test_create_image_backing_out_of_the_scan_returns_to_the_menu(self) -> None:
        # Cancelling is not the same as being unable to scan: it must not drop
        # the user into a base-image list they did not ask for.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_CANCELLED):
            with patch.object(app, "create_new_image") as create_mock:
                app.create_image()
        create_mock.assert_not_called()
        self.assertEqual(stub.messages, [])

    def test_create_image_offers_a_supported_base_after_refusing_an_unsupported_one(self) -> None:
        # scan_os has already said why it stopped, so this path only routes the
        # offer to start over -- it must not repeat the explanation.
        app = self.make_app()
        stub = GumStub()
        prompts: list[str] = []

        def confirm(prompt: str, **_kwargs: object) -> bool:
            prompts.append(prompt)
            return True

        stub.confirm = confirm
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_UNSUPPORTED_BASE):
            with patch.object(app, "create_new_image") as create_mock:
                with redirect_stdout(io.StringIO()):
                    app.create_image()
        create_mock.assert_called_once_with()
        self.assertEqual(len(prompts), 1)
        self.assertIn("supported base image", prompts[0])

    def test_create_image_unsupported_base_offer_defaults_to_no(self) -> None:
        # If the running image is one this tool manages, starting fresh throws
        # away the settings that repo already holds, so the safe answer is the
        # default one.
        app = self.make_app()
        stub = GumStub()
        defaults: list[object] = []

        def confirm(_prompt: str, **kwargs: object) -> bool:
            defaults.append(kwargs.get("default"))
            return False

        stub.confirm = confirm
        app.gum = stub
        with patch.object(app, "scan_os", return_value=SCAN_UNSUPPORTED_BASE):
            with patch.object(app, "create_new_image") as create_mock:
                with redirect_stdout(io.StringIO()):
                    app.create_image()
        create_mock.assert_not_called()
        self.assertEqual(defaults, [False])

    def test_main_menu_dispatches_to_update_existing_image(self) -> None:
        app = self.make_app()
        stub = GumStub()
        choices = ["Update Existing Image", "Quit"]
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "update_existing_image") as update_mock:
            with patch.object(app, "view_build_status") as status_mock:
                with self.assertRaises(SystemExit):
                    app.main_menu()
        update_mock.assert_called_once()
        status_mock.assert_not_called()

    def test_main_menu_dispatches_to_view_build_status(self) -> None:
        app = self.make_app()
        stub = GumStub()
        choices = ["View Build Status", "Quit"]
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "view_build_status") as status_mock:
            with patch.object(app, "update_existing_image") as update_mock:
                with self.assertRaises(SystemExit):
                    app.main_menu()
        status_mock.assert_called_once()
        update_mock.assert_not_called()

    def test_build_status_reminds_how_to_switch_after_a_scanned_build_succeeds(self) -> None:
        # The reset-then-switch pair is the riskiest step and happens an hour
        # after the tool printed it. This screen is where the user comes back.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps([{"conclusion": "success", "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
            with patch.object(app, "repo_carried_scan_customizations", return_value=True):
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("sudo rpm-ostree reset", hints)
        # Built from the arguments, since the picker does not load the config.
        self.assertIn("ghcr.io/example/my-image:latest", hints)
        self.assertIn("do not reboot in between", hints.lower())

    def test_build_status_says_a_green_build_is_not_yet_readable(self) -> None:
        # A green build is not a switchable image: the package it published is
        # private, and `gh repo create --public` does not change that -- package
        # visibility is a separate setting that does not inherit repository
        # access. ghcr_package_exists is the same anonymous pull a host without
        # credentials would make, so it answers the question that decides
        # whether the switch command works. It is patched to False for every
        # test in setUp, which is the private-or-unreachable case.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps([{"conclusion": "success", "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
            with patch.object(app, "repo_carried_scan_customizations", return_value=False):
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("could not pull ghcr.io/example/my-image anonymously", hints)
        self.assertIn("https://github.com/Example/my-image/pkgs/container/my-image", hints)
        self.assertIn(atomic_image_builder.BOOTC_REGISTRY_DOCS_URL, hints)

    def test_build_status_stops_saying_it_once_the_package_is_public(self) -> None:
        # Self-clearing: the check that reports the problem is the one that
        # confirms it is fixed, so nobody has to dismiss a stale warning.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps([{"conclusion": "success", "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.ghcr_package_exists", return_value=True):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
                with patch.object(app, "repo_carried_scan_customizations", return_value=False):
                    with redirect_stdout(io.StringIO()):
                        app.render_build_status("Example", "my-image")
        self.assertNotIn("anonymously", " ".join(m for _l, m in stub.messages))

    def test_build_status_does_not_probe_before_a_build_has_succeeded(self) -> None:
        # There is no package to be private about until a build has published
        # one, and reporting one as unreadable then would be noise.
        app = self.make_app()
        app.gum = GumStub()
        runs = json.dumps([{"conclusion": None, "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.ghcr_package_exists") as probe:
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")
        probe.assert_not_called()

    def test_build_status_stays_quiet_when_nothing_was_scanned(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps([{"conclusion": "success", "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
            with patch.object(app, "repo_carried_scan_customizations", return_value=False):
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")
        self.assertNotIn("rpm-ostree reset", " ".join(m for _l, m in stub.messages))

    def test_build_status_stays_quiet_until_a_build_has_succeeded(self) -> None:
        # Switching to an image that has not been built yet is the mistake this
        # reminder must not encourage.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps([{"conclusion": None, "workflowName": "build", "displayTitle": "t", "url": "u"}])
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
            with patch.object(app, "repo_carried_scan_customizations", return_value=True) as carried:
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")
        self.assertNotIn("rpm-ostree reset", " ".join(m for _l, m in stub.messages))
        carried.assert_not_called()

    def test_render_build_status_reports_unparseable_run_data(self) -> None:
        # `gh run list` succeeded (returncode 0) but printed something that is
        # not valid JSON -- seen in practice when gh emits a warning banner on
        # stdout ahead of the payload. This must not raise out of the screen.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, "not json", "")):
            with redirect_stdout(io.StringIO()):
                app.render_build_status("Example", "my-image")
        errors = " ".join(m for level, m in stub.messages if level == "error")
        self.assertIn("Unable to read GitHub Actions run data.", errors)
        # view_build_status has nothing after this call, so control returns into
        # main_menu's loop and redraws. Without a pause the error never gets read.
        self.assertIn("Press Enter to return to the main menu...", stub.prompts)

    def test_every_render_build_status_exit_pauses_before_the_menu_redraws(self) -> None:
        # The three failure branches drifted apart: two paused, one did not, so
        # malformed gh output flashed an error and vanished. They are only
        # correct together, so assert them together.
        cases = {
            "gh call failed": subprocess.CompletedProcess([], 1, "", "boom"),
            "malformed json": subprocess.CompletedProcess([], 0, "not json", ""),
            "no runs": subprocess.CompletedProcess([], 0, "[]", ""),
            "not a list": subprocess.CompletedProcess([], 0, '{"runs": []}', ""),
        }
        for label, proc in cases.items():
            with self.subTest(case=label):
                app = self.make_app()
                stub = GumStub()
                app.gum = stub
                with patch("atomic_image_builder.run", return_value=proc):
                    with redirect_stdout(io.StringIO()):
                        app.render_build_status("Example", "my-image")
                self.assertIn("Press Enter to return to the main menu...", stub.prompts)

    def test_render_build_status_skips_entries_that_are_not_objects(self) -> None:
        # gh's --json output is a list of objects, but a single malformed entry
        # must not take the whole screen down -- every field read below this
        # guard assumes a dict. The good rows either side still render.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = json.dumps(
            [
                {"conclusion": "success", "workflowName": "build", "displayTitle": "first", "url": "u1"},
                "not an object",
                None,
                42,
                {"conclusion": "failure", "workflowName": "build", "displayTitle": "last", "url": "u2"},
            ]
        )
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, runs, "")):
            with patch.object(app, "repo_carried_scan_customizations", return_value=False):
                with redirect_stdout(io.StringIO()):
                    app.render_build_status("Example", "my-image")

        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("first", hints)
        self.assertIn("last", hints)
        self.assertNotIn("not an object", hints)
        self.assertIn("Press Enter to return to the main menu...", stub.prompts)

    def test_render_build_status_warns_when_no_runs_are_returned(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, "[]", "")):
            with redirect_stdout(io.StringIO()):
                app.render_build_status("Example", "my-image")
        warnings = " ".join(m for level, m in stub.messages if level == "warn")
        self.assertIn("No recent GitHub Actions runs found for Example/my-image.", warnings)

    def test_render_build_status_warns_when_run_data_is_not_a_list(self) -> None:
        # gh's --json flag is documented to emit an array, but a future gh
        # version or a wrapping error object should degrade the same way an
        # empty list does rather than crashing the screen.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        payload = json.dumps({"error": "unexpected"})
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, payload, "")):
            with redirect_stdout(io.StringIO()):
                app.render_build_status("Example", "my-image")
        warnings = " ".join(m for level, m in stub.messages if level == "warn")
        self.assertIn("No recent GitHub Actions runs found for Example/my-image.", warnings)

    def test_repo_carried_scan_customizations_reads_the_remote_state_file(self) -> None:
        import base64
        payload = base64.b64encode(json.dumps({"scan_customizations_carried": True}).encode()).decode()
        app = self.make_app()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, payload, "")):
            self.assertTrue(app.repo_carried_scan_customizations("owner", "repo"))
        # Anything unreadable means "do not know", and the caller stays quiet.
        for proc in (
            subprocess.CompletedProcess([], 1, "", "boom"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "not-base64!!", ""),
        ):
            with patch("atomic_image_builder.run", return_value=proc):
                self.assertFalse(app.repo_carried_scan_customizations("owner", "repo"))

    def test_open_url_in_browser_discards_output_and_does_not_wait(self) -> None:
        # A GUI browser is chatty on stderr. Inheriting the terminal writes
        # Mesa and EGL warnings straight over the running TUI -- seen for real
        # on a VM with software rendering.
        calls: list[dict] = []

        def fake_popen(args, **kwargs):
            calls.append({"args": args, **kwargs})
            return object()

        with patch("atomic_image_builder.command_exists", side_effect=lambda n: n == "xdg-open"):
            with patch("subprocess.Popen", side_effect=fake_popen):
                self.assertTrue(atomic_image_builder.open_url_in_browser("https://example.com"))

        self.assertEqual(calls[0]["args"], ["xdg-open", "https://example.com"])
        self.assertEqual(calls[0]["stdout"], subprocess.DEVNULL)
        self.assertEqual(calls[0]["stderr"], subprocess.DEVNULL)
        # Detached, so a slow browser cannot stall the prompt that follows.
        self.assertTrue(calls[0]["start_new_session"])

    def test_open_url_in_browser_reports_when_it_cannot_open_one(self) -> None:
        with patch("atomic_image_builder.command_exists", return_value=False):
            self.assertFalse(atomic_image_builder.open_url_in_browser("https://example.com"))
        with patch("atomic_image_builder.command_exists", side_effect=lambda n: n == "xdg-open"):
            with patch("subprocess.Popen", side_effect=OSError("no display")):
                self.assertFalse(atomic_image_builder.open_url_in_browser("https://example.com"))

    def test_github_setup_guide_tells_you_the_url_when_no_browser_opens(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, **_kwargs: True
        # Take the "I need to create a GitHub account first" branch.
        stub.choose = lambda options, **_kwargs: [next(o for o in options if o.startswith("I need to create"))]
        app.gum = stub
        with patch("atomic_image_builder.open_url_in_browser", return_value=False):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with redirect_stdout(io.StringIO()):
                    with contextlib.suppress(Exception):
                        app.github_setup_guide()
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("github.com/signup", hints)

    def test_github_setup_guide_stays_quiet_when_the_browser_opens(self) -> None:
        # The "go there manually" hint is the fallback. When the browser does
        # open, repeating the URL is noise on a screen that already has plenty.
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda _prompt, **_kwargs: True
        stub.choose = lambda options, **_kwargs: [next(o for o in options if o.startswith("I need to create"))]
        app.gum = stub
        with patch("atomic_image_builder.open_url_in_browser", return_value=True):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with redirect_stdout(io.StringIO()):
                    with contextlib.suppress(Exception):
                        app.github_setup_guide()
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertNotIn("manually", hints)

    def test_every_colour_stays_legible_on_light_and_dark_terminals(self) -> None:
        # The original palette was picked on a dark terminal. 252 is almost
        # white and 117 a pale blue, and gum was handed 117 for
        # `choose --selected.foreground` -- so on a light terminal a list with
        # everything selected rendered as a blank screen.
        # A single numeric range cannot express this: 16-231 is a colour cube
        # and 232-255 a greyscale ramp, so 244 is a perfectly readable mid grey
        # while 252 is nearly white. Exclude the extremes of each instead.
        near_white = set(range(250, 256)) | {15, 231}
        near_black = set(range(232, 239)) | {0, 16}
        for name in ("ACCENT_COLOR", "MUTED_COLOR", "SUCCESS_COLOR", "WARNING_COLOR", "NOTICE_COLOR"):
            value = getattr(atomic_image_builder, name)
            with self.subTest(colour=name):
                self.assertNotIn(value, near_white, f"{name} washes out on a light background")
                self.assertNotIn(value, near_black, f"{name} disappears on a dark background")

    def test_no_colour_is_hardcoded_past_the_named_palette(self) -> None:
        # A bare index slipped past the constants is how this regressed before.
        source = Path(__file__).resolve().parents[1].joinpath("atomic_image_builder.py").read_text()
        stray = re.findall(r"foreground=(\d+)", source) + re.findall(r'foreground",\s*\n\s*"(\d+)"', source)
        self.assertEqual(stray, [], f"colours must go through the named palette, found {stray}")

    def test_scan_results_explain_what_happens_next(self) -> None:
        # The results table is all facts and no orientation: a user is left
        # looking at their own system's details with nothing telling them what
        # the tool will do with them.
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda options, **_kwargs: list(options)
        app.gum = stub
        payload = self.scan_payload(["tmux"], [])
        with redirect_stdout(io.StringIO()):
            self.run_scan(app, payload)
        hints = " ".join(m for level, m in stub.messages if level == "hint")
        self.assertIn("built on", hints)
        self.assertIn("the base this system already runs", hints)
        self.assertIn("Nothing is created on GitHub until you confirm", hints)
        # The chooser takes the screen the moment it opens, so the results need
        # a pause or they flash past unread.
        self.assertTrue(any("carry over" in prompt for prompt in stub.prompts), stub.prompts)

    def test_scan_results_and_the_package_chooser_are_separate_pages(self) -> None:
        # gum choose draws inline, so without a clear the results, the
        # explanation and a twenty-item list all land on one screen.
        app = self.make_app()
        stub = GumStub()
        headers: list[str] = []
        stub.header = lambda title, **_kwargs: headers.append(title)
        stub.choose = lambda options, **_kwargs: list(options)
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            self.run_scan(app, self.scan_payload(["tmux"], ["firefox"]))
        # One page for the results, then one per chooser. ("Scanning Running OS"
        # comes first, while the scan is still reading the host.)
        self.assertEqual(
            headers,
            ["Scanning Running OS", "Scan Results", "Packages To Carry Over", "Base Packages To Remove"],
        )

    def test_scan_os_resets_stale_config_before_loading_host_state(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["old-package"]
        app.config.removed_packages = ["old-removal"]

        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, status_payload, ""),
            ):
                result = app.scan_os()

        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.config.removed_packages, [])
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")
        self.assertEqual(app.config.github_user, "example")

    def test_scan_os_preserves_exact_running_image_ref(self) -> None:
        app = self.make_app()
        app.github_user = "example"

        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:testing",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        gum = GumStub()
        # Decline only the tag-normalization prompt so the exact ref is kept,
        # but accept other confirm prompts (like "Continue anyway?") by default.
        gum.confirm = lambda prompt, default=False: False if "recommended" in prompt else default
        app.gum = gum
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, status_payload, ""),
            ):
                result = app.scan_os()

        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:testing")
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")
        # Verify the warning was shown
        self.assertTrue(any("testing" in msg and "stable" in msg for _, msg in gum.messages))

    def test_scan_os_returns_false_when_rpm_ostree_is_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=False):
            with patch("atomic_image_builder.run") as run_mock:
                self.assertEqual(app.scan_os(), SCAN_UNAVAILABLE)

        run_mock.assert_not_called()
        self.assertTrue(
            any(level == "error" and "rpm-ostree not found" in message for level, message in stub.messages)
        )

    def test_scan_os_retries_without_booted_and_uses_that_result(self) -> None:
        # rpm-ostree on some hosts rejects or empties out `--booted`; the retry
        # without it is what keeps scanning working there, so the fallback
        # payload must be the one actually parsed.
        app = self.make_app()
        app.github_user = "example"
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        commands: list[list[str]] = []

        def fake_run(args, **_kwargs):
            commands.append(list(args))
            if "--booted" in args:
                return subprocess.CompletedProcess(list(args), 1, "", "not booted")
            return subprocess.CompletedProcess(list(args), 0, status_payload, "")

        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                self.assertEqual(app.scan_os(), SCAN_OK)

        self.assertEqual(
            commands,
            [
                ["rpm-ostree", "status", "--json", "--booted"],
                ["rpm-ostree", "status", "--json"],
            ],
        )
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:stable")

    def test_scan_os_retries_without_booted_when_output_is_empty(self) -> None:
        # A zero exit with empty stdout is the other shape that triggers the
        # retry; exit status alone is not enough to accept the first attempt.
        app = self.make_app()
        app.github_user = "example"
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        commands: list[list[str]] = []

        def fake_run(args, **_kwargs):
            commands.append(list(args))
            if "--booted" in args:
                return subprocess.CompletedProcess(list(args), 0, "   \n", "")
            return subprocess.CompletedProcess(list(args), 0, status_payload, "")

        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                self.assertEqual(app.scan_os(), SCAN_OK)

        self.assertEqual(len(commands), 2)

    def test_scan_os_returns_false_when_both_rpm_ostree_attempts_fail(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree"], 1, "", "boom"),
            ):
                self.assertEqual(app.scan_os(), SCAN_UNAVAILABLE)

        self.assertTrue(
            any(level == "error" and "Failed to read rpm-ostree status" in message for level, message in stub.messages)
        )

    def test_scan_os_returns_false_on_malformed_rpm_ostree_json(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        # rpm-ostree exited 0 with non-empty stdout that is not valid JSON.
        # The original code raised an unhandled JSONDecodeError here; the fix
        # surfaces a friendly error and returns False instead.
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(
                    ["rpm-ostree", "status", "--json", "--booted"],
                    0,
                    "warning: cache busy\nnot json at all",
                    "",
                ),
            ):
                result = app.scan_os()

        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(
                level == "error" and "rpm-ostree" in message
                for level, message in stub.messages
            )
        )

    def test_scan_os_returns_false_when_deployment_has_no_container_reference(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        # A booted deployment with neither container-image-reference nor origin
        # (e.g. a legacy ostree-commit deployment) cannot be carried into an
        # image repo. Bail early instead of silently producing an empty URI.
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "requested-packages": ["tmux"],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(
                    ["rpm-ostree", "status", "--json", "--booted"],
                    0,
                    status_payload,
                    "",
                ),
            ):
                result = app.scan_os()

        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(
                level == "error" and "container image" in message
                for level, message in stub.messages
            )
        )

    def test_scan_os_matches_fedora_atomic_remote_registry_origin(self) -> None:
        app = self.make_app()
        app.github_user = "example"

        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "origin": "ostree-remote-registry:fedora:quay.io/fedora-ostree-desktops/kinoite:43",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, status_payload, ""),
            ):
                result = app.scan_os()

        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.base_image_uri, "quay.io/fedora-ostree-desktops/kinoite:44")
        self.assertEqual(app.config.base_image_name, "Fedora Kinoite")

    def test_scan_os_refuses_an_image_that_is_not_a_supported_base(self) -> None:
        # choose_base_image already refuses an image that is not curated. The
        # scanned path has to agree: the same image must not be rejected when
        # typed in and accepted silently when detected.
        app = self.make_app()
        app.github_user = "example"
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "ostree-unverified-registry:ghcr.io/danathar/my-custom-image:latest",
                        "requested-packages": ["htop"],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree"], 0, status_payload, ""),
            ):
                with redirect_stdout(io.StringIO()):
                    result = app.scan_os()

        self.assertEqual(result, SCAN_UNSUPPORTED_BASE)
        self.assertEqual(app.config.base_image_uri, "")
        self.assertEqual(app.config.base_image_name, "")
        warnings = " ".join(m for level, m in app.gum.messages if level == "warn")
        self.assertIn("ghcr.io/danathar/my-custom-image:latest", warnings)
        self.assertIn("not one of the images this tool supports", warnings)

    def test_scan_os_names_the_repo_when_the_running_image_is_tool_managed(self) -> None:
        # Telling someone their own image apart from a stranger's changes the
        # advice completely: update the repo, do not rebuild from scratch.
        app = self.make_app()
        app.github_user = "danathar"
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "ostree-unverified-registry:ghcr.io/danathar/my-image:latest",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        sections: list[tuple[str, tuple[str, ...]]] = []
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree"], 0, status_payload, ""),
            ):
                with patch.object(app, "repo_has_state_file", return_value=True) as state_mock:
                    with patch.object(app, "menu_section", side_effect=lambda title, *lines: sections.append((title, lines))):
                        with redirect_stdout(io.StringIO()):
                            result = app.scan_os()

        self.assertEqual(result, SCAN_UNSUPPORTED_BASE)
        state_mock.assert_called_once_with("danathar", "my-image")
        self.assertEqual(len(sections), 1)
        self.assertIn("danathar/my-image", " ".join(sections[0][1]))
        self.assertIn("Update Existing Image", " ".join(sections[0][1]))

    def test_scan_os_unsupported_base_lookup_failure_falls_back_to_generic_advice(self) -> None:
        # A failed lookup means "cannot tell", not "not yours". The refusal
        # still stands; only the wording is less specific.
        app = self.make_app()
        app.github_user = "danathar"
        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "ostree-unverified-registry:ghcr.io/danathar/my-image:latest",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        sections: list[tuple[str, tuple[str, ...]]] = []
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree"], 0, status_payload, ""),
            ):
                with patch.object(app, "repo_has_state_file", side_effect=CommandError("boom")):
                    with patch.object(app, "menu_section", side_effect=lambda title, *lines: sections.append((title, lines))):
                        with redirect_stdout(io.StringIO()):
                            result = app.scan_os()

        self.assertEqual(result, SCAN_UNSUPPORTED_BASE)
        self.assertEqual(len(sections), 1)
        self.assertIn("custom image is not supported", " ".join(sections[0][1]))

    def test_scanned_image_owner_repo_parses_only_ghcr_owner_repo_refs(self) -> None:
        app = self.make_app()
        self.assertEqual(app.scanned_image_owner_repo("ghcr.io/owner/repo:latest"), ("owner", "repo"))
        self.assertEqual(app.scanned_image_owner_repo("ghcr.io/owner/repo"), ("owner", "repo"))
        self.assertEqual(app.scanned_image_owner_repo("ghcr.io/owner/repo@sha256:abc123"), ("owner", "repo"))
        self.assertIsNone(app.scanned_image_owner_repo("quay.io/fedora-ostree-desktops/kinoite:44"))
        self.assertIsNone(app.scanned_image_owner_repo("ghcr.io/ublue-os/akmods/extra:latest"))
        self.assertIsNone(app.scanned_image_owner_repo("ghcr.io/owner"))
        self.assertIsNone(app.scanned_image_owner_repo("localhost:5000/owner/repo"))

    def test_scanned_image_is_managed_skips_the_lookup_without_gh(self) -> None:
        # No gh means no answer to be had, and the refusal does not depend on
        # one, so the call is not worth attempting.
        app = self.make_app()
        with patch("atomic_image_builder.command_exists", return_value=False):
            with patch.object(app, "repo_has_state_file") as state_mock:
                self.assertIsNone(app.scanned_image_is_managed("ghcr.io/owner/repo:latest"))
        state_mock.assert_not_called()

    def test_scanned_image_is_managed_returns_none_for_a_repo_without_state(self) -> None:
        app = self.make_app()
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch.object(app, "repo_has_state_file", return_value=False):
                self.assertIsNone(app.scanned_image_is_managed("ghcr.io/owner/repo:latest"))

    def run_scan_with_status(self, deployment: dict, *, gum: object | None = None) -> tuple[str, App, "GumStub"]:
        """scan_os() against one synthetic booted deployment."""
        app = self.make_app()
        app.github_user = "example"
        stub = gum or GumStub()
        stub.choose = lambda options, **_kwargs: list(options)
        app.gum = stub
        payload = json.dumps({"deployments": [{"booted": True, **deployment}]})
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "rpm-ostree-status.json"
            status_path.write_text(payload)
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                    with redirect_stdout(io.StringIO()):
                        result = app.scan_os()
        return result, app, stub

    def accepting_gum(self) -> "GumStub":
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        return stub

    BLUEFIN = "ostree-unverified-registry:ghcr.io/ublue-os/bluefin:stable"

    def test_scan_os_names_local_rpms_it_cannot_carry_and_asks_first(self) -> None:
        # The reported case: htop is carried, the local VPN RPM and the local
        # base replacement are not, and nothing said so. The scan reported
        # success and the generated README recommended `rpm-ostree reset`,
        # which would have removed both from the next deployment.
        result, app, stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": ["htop"],
                "requested-base-removals": [],
                "requested-local-packages": ["example-vpn-1.0-1.x86_64"],
                "requested-base-local-replacements": ["example-driver-2.0-1.x86_64"],
            },
            gum=self.accepting_gum(),
        )
        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.packages, ["htop"])
        self.assertTrue(
            any(level == "warn" and "cannot be carried" in message for level, message in stub.messages),
            stub.messages,
        )

    def test_scan_os_stops_when_the_omitted_customizations_are_declined(self) -> None:
        # Defaulting to no is the point: continuing without a local RPM has to
        # be a decision someone made, not one made for them by a field nothing
        # read. GumStub.confirm returns the default it is passed.
        result, _app, _stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": ["htop"],
                "requested-base-removals": [],
                "requested-local-packages": ["example-vpn-1.0-1.x86_64"],
            }
        )
        self.assertEqual(result, SCAN_CANCELLED)

    def test_scan_os_does_not_call_a_local_rpm_only_host_unlayered(self) -> None:
        # "No layered packages found." was the entire message such a host got,
        # and it is false: there is layering, it just cannot be carried.
        _result, _app, stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": [],
                "requested-base-removals": [],
                "requested-local-packages": ["example-vpn-1.0-1.x86_64"],
            },
            gum=self.accepting_gum(),
        )
        warnings = [message for level, message in stub.messages if level == "warn"]
        self.assertNotIn("No layered packages found.", warnings)
        self.assertIn("No layered packages this tool can carry over were found.", warnings)

    def test_scan_os_keeps_the_plain_message_when_nothing_is_omitted(self) -> None:
        _result, _app, stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": [],
                "requested-base-removals": [],
            },
            gum=self.accepting_gum(),
        )
        warnings = [message for level, message in stub.messages if level == "warn"]
        self.assertIn("No layered packages found.", warnings)

    def test_scan_os_asks_nothing_extra_when_every_customization_is_supported(self) -> None:
        # The existing behaviour, asserted so the new prompt cannot leak into
        # the ordinary path: a host with only repository packages and base
        # removals is carried exactly as before.
        result, app, stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": ["htop", "tmux"],
                "requested-base-removals": ["firefox"],
            },
            gum=self.accepting_gum(),
        )
        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.packages, ["htop", "tmux"])
        self.assertEqual(app.config.removed_packages, ["firefox"])
        self.assertFalse(
            [m for level, m in stub.messages if level == "warn" and "cannot be carried" in m],
            stub.messages,
        )

    def test_unsupported_scan_customizations_reads_every_category(self) -> None:
        # One assertion per field rpm-ostree documents, because each is a
        # separate way for a customization to go missing and the defect was
        # that four of them were read as absent rather than as unsupported.
        app = self.make_app()
        self.assertEqual(
            app.unsupported_scan_customizations(
                {
                    "requested-local-packages": ["local-1.0-1.x86_64"],
                    "requested-local-fileoverride-packages": ["fileoverride-1.0-1.x86_64"],
                    "requested-base-local-replacements": ["local-replacement-1.0-1.x86_64"],
                    "requested-base-remote-replacements": ["remote-replacement-1.0-1.x86_64"],
                    "initramfs-etc": ["/etc/crypttab"],
                    "initramfs-args": ["--arg"],
                    "regenerate-initramfs": True,
                }
            ),
            [
                ("Locally installed RPMs", ["local-1.0-1.x86_64"]),
                ("Local file overrides", ["fileoverride-1.0-1.x86_64"]),
                ("Base packages replaced by a local RPM", ["local-replacement-1.0-1.x86_64"]),
                ("Base packages replaced from a repository", ["remote-replacement-1.0-1.x86_64"]),
                ("Files kept in the initramfs from /etc", ["/etc/crypttab"]),
                ("Custom initramfs arguments", ["--arg"]),
                ("A locally regenerated initramfs", []),
            ],
        )

    def test_unsupported_scan_customizations_is_empty_for_a_supported_host(self) -> None:
        app = self.make_app()
        self.assertEqual(
            app.unsupported_scan_customizations(
                {"requested-packages": ["htop"], "requested-base-removals": ["firefox"]}
            ),
            [],
        )

    def test_unsupported_scan_customizations_tolerates_a_malformed_field(self) -> None:
        # Same contract as the two supported fields: a future schema change
        # must reach the friendly path, not an AttributeError.
        app = self.make_app()
        self.assertEqual(
            app.unsupported_scan_customizations(
                {"requested-local-packages": "local-1.0-1.x86_64", "initramfs-etc": None}
            ),
            [],
        )

    def test_scan_results_count_a_valueless_category_as_one(self) -> None:
        # regenerate-initramfs is a boolean, not a list, so summing value
        # counts alone showed "Cannot Be Carried Over: 0" on the very screen
        # that then warns about it.
        rows: list[tuple[str, str]] = []
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        stub.table = lambda table_rows, **_kwargs: rows.extend(table_rows)
        _result, _app, _stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": ["htop"],
                "requested-base-removals": [],
                "regenerate-initramfs": True,
            },
            gum=stub,
        )
        self.assertIn(("Cannot Be Carried Over", "1"), rows)

    def test_scan_results_count_every_omitted_item(self) -> None:
        rows: list[tuple[str, str]] = []
        stub = GumStub()
        stub.confirm = lambda _prompt, default=False: True
        stub.table = lambda table_rows, **_kwargs: rows.extend(table_rows)
        self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": [],
                "requested-base-removals": [],
                "requested-local-packages": ["one-1.0-1.x86_64", "two-1.0-1.x86_64"],
                "regenerate-initramfs": True,
            },
            gum=stub,
        )
        self.assertIn(("Cannot Be Carried Over", "3"), rows)

    def test_scan_os_reports_an_initramfs_regeneration_it_cannot_reproduce(self) -> None:
        # Not a package list, and still something the recommended reset undoes.
        _result, _app, stub = self.run_scan_with_status(
            {
                "container-image-reference": self.BLUEFIN,
                "requested-packages": ["htop"],
                "requested-base-removals": [],
                "regenerate-initramfs": True,
            },
            gum=self.accepting_gum(),
        )
        self.assertTrue(
            any(level == "warn" and "cannot be carried" in message for level, message in stub.messages),
            stub.messages,
        )

    def test_scan_os_honors_status_file_override(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["old-package"]
        app.config.removed_packages = ["old-removal"]

        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "rpm-ostree-status.json"
            status_path.write_text(status_payload)
            # command_exists always False and run() unpatched (would raise if
            # actually invoked): the override path must never touch either.
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch("atomic_image_builder.run", side_effect=AssertionError("rpm-ostree must not be invoked")):
                    with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                        result = app.scan_os()

        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.config.removed_packages, [])
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")
        self.assertEqual(app.config.github_user, "example")

    def test_scan_os_status_file_override_missing_file_returns_false(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "does-not-exist.json"
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(missing_path)}):
                    result = app.scan_os()

        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

    def test_scan_os_status_file_override_invalid_json_returns_false(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "rpm-ostree-status.json"
            status_path.write_text("not json at all")
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                    result = app.scan_os()

        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

    def scan_with_status(self, payload: str, stub: GumStub | None = None) -> tuple[App, GumStub, bool]:
        # Shared driver for the malformed-status cases below: valid JSON that is
        # not the object shape scan_os expects must reach the friendly error
        # rather than an AttributeError out of .get()/.strip().
        app = self.make_app()
        app.github_user = "example"
        stub = stub if stub is not None else GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "rpm-ostree-status.json"
            status_path.write_text(payload)
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                    result = app.scan_os()
        return app, stub, result

    def test_scan_os_status_file_override_json_array_returns_false(self) -> None:
        _, stub, result = self.scan_with_status("[]")
        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

    def test_scan_os_status_file_override_json_scalar_returns_false(self) -> None:
        _, stub, result = self.scan_with_status('"a string, not an object"')
        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

    def test_scan_os_status_file_override_non_dict_deployments_returns_false(self) -> None:
        _, stub, result = self.scan_with_status('{"deployments": [1, 2]}')
        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "deployment" in message.lower() for level, message in stub.messages)
        )

    def test_scan_os_status_file_override_non_string_container_ref_returns_false(self) -> None:
        _, stub, result = self.scan_with_status(
            '{"deployments": [{"booted": true, "container-image-reference": 123}]}'
        )
        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "container image reference" in message for level, message in stub.messages)
        )

    def test_scan_os_status_file_override_drops_non_string_packages(self) -> None:
        # Non-string entries must be dropped, and a field that is a bare string
        # instead of a list must NOT be iterated into individual characters.
        class ChoosingStub(GumStub):
            def choose(self, items, **_kwargs):
                return list(items)

        app, _, result = self.scan_with_status(
            '{"deployments": [{"booted": true, '
            '"container-image-reference": "ostree-image-signed:docker://ghcr.io/ublue-os/bazzite:stable", '
            '"requested-packages": [1, "vim", null, "git"], '
            '"requested-base-removals": "notalist"}]}',
            stub=ChoosingStub(),
        )
        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.scanned_packages, ["vim", "git"])
        self.assertEqual(app.config.scanned_removed, [])

    def test_string_list_coerces_untrusted_json_values(self) -> None:
        self.assertEqual(string_list(["a", 1, None, "b"]), ["a", "b"])
        self.assertEqual(string_list("abc"), [])
        self.assertEqual(string_list(None), [])
        self.assertEqual(string_list({"a": "b"}), [])

    def test_scan_os_status_file_override_non_utf8_returns_false(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "rpm-ostree-status.json"
            # A non-UTF-8 (e.g. binary) file: read_text() raises
            # UnicodeDecodeError, which is not an OSError. Must still land on
            # the friendly error path rather than propagating a traceback.
            status_path.write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
            with patch("atomic_image_builder.command_exists", return_value=False):
                with patch.dict("os.environ", {"AIB_RPM_OSTREE_STATUS_FILE": str(status_path)}):
                    result = app.scan_os()

        self.assertEqual(result, SCAN_UNAVAILABLE)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

    def test_update_existing_image_defers_signing_setup_until_push(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        with patch.object(app, "select_repo", return_value=("example", "test-image")):
            with patch.object(app, "clone_repo", return_value=None):
                with patch.object(app, "load_repo_config", return_value=None):
                    with patch.object(app, "repo_default_branch", return_value="main"):
                        with patch.object(app, "update_menu", return_value=False):
                            with patch.object(app, "ensure_signing_ready") as ensure_mock:
                                app.update_existing_image()
        ensure_mock.assert_not_called()

    def test_update_existing_image_returns_when_github_is_unavailable(self) -> None:
        # The auth gate is the first thing this flow checks; failing it must
        # stop before any repo is picked or cloned.
        app = self.make_app()
        with patch.object(app, "require_github", return_value=False):
            with patch.object(app, "select_repo") as select_mock:
                with patch.object(app, "clone_repo") as clone_mock:
                    app.update_existing_image()
        select_mock.assert_not_called()
        clone_mock.assert_not_called()

    def test_update_existing_image_returns_when_repo_picker_is_escaped(self) -> None:
        # Esc out of the repo picker backs out of the whole flow rather than
        # cloning something the user never chose.
        app = self.make_app()
        with patch.object(app, "require_github", return_value=True):
            with patch.object(app, "select_repo", side_effect=ScreenBack):
                with patch.object(app, "clone_repo") as clone_mock:
                    app.update_existing_image()
        clone_mock.assert_not_called()

    def test_update_existing_image_pushes_when_update_menu_saves(self) -> None:
        # Saving from the update menu is what carries the flow through to the
        # push, against the same temporary clone that was edited.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        clone_dirs: list[Path] = []

        def fake_clone(_owner, _repo, target):
            clone_dirs.append(target)

        with patch.object(app, "select_repo", return_value=("example", "test-image")):
            with patch.object(app, "clone_repo", side_effect=fake_clone):
                with patch.object(app, "load_repo_config") as load_mock:
                    with patch.object(app, "update_menu", return_value=True) as menu_mock:
                        with patch.object(app, "show_summary") as summary_mock:
                            with patch.object(app, "push_update") as push_mock:
                                app.update_existing_image()

        summary_mock.assert_called_once()
        push_mock.assert_called_once()
        # Every stage must operate on the one clone: reading its config,
        # editing it, and pushing it. A stage pointed elsewhere would push a
        # tree that never received the edits.
        self.assertEqual(load_mock.call_args.args[0], clone_dirs[0])
        self.assertEqual(menu_mock.call_args.kwargs["repo_dir"], clone_dirs[0])
        owner, repo, repo_dir = push_mock.call_args.args
        self.assertEqual((owner, repo), ("example", "test-image"))
        self.assertEqual(repo_dir, clone_dirs[0])

    def test_load_repo_config_keeps_authenticated_session_user(self) -> None:
        app = self.make_app()
        app.github_user = "current-user"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / STATE_FILE).write_text(
                json.dumps(
                    {
                        "method": "containerfile",
                        "base_image_uri": "ghcr.io/ublue-os/bazzite:stable",
                        "base_image_name": "Bazzite (KDE)",
                        "repo_name": "test-image",
                        "image_desc": "Test image",
                        "github_user": "old-user",
                    }
                )
            )
            app.load_repo_config(repo_dir)
        self.assertEqual(app.github_user, "current-user")

    def test_view_build_status_renders_recent_runs(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        runs = [
            {
                "databaseId": 1,
                "displayTitle": "Build succeeded",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-04-17T10:00:00Z",
                "updatedAt": "2026-04-17T10:05:00Z",
                "url": "https://github.com/example/test-image/actions/runs/1",
                "workflowName": "Build",
            },
            {
                "databaseId": 2,
                "displayTitle": "Build failed",
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-04-17T09:00:00Z",
                "updatedAt": "2026-04-17T09:03:00Z",
                "url": "https://github.com/example/test-image/actions/runs/2",
                "workflowName": "Build",
            },
            {
                "databaseId": 3,
                "displayTitle": "Build running",
                "status": "in_progress",
                "conclusion": None,
                "createdAt": "2026-04-17T08:00:00Z",
                "updatedAt": "2026-04-17T08:01:00Z",
                "url": "https://github.com/example/test-image/actions/runs/3",
                "workflowName": "Build",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / STATE_FILE).write_text(json.dumps(app.state_payload()) + "\n")
            with patch("atomic_image_builder.Path.cwd", return_value=repo_dir):
                with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "run"], 0, json.dumps(runs), "")):
                    app.view_build_status()

        rendered = "\n".join(message for level, message in stub.messages if level == "hint")
        self.assertEqual(rendered.count("https://github.com/example/test-image/actions/runs/"), 3)
        self.assertIn("✓", rendered)
        self.assertIn("✗", rendered)
        self.assertIn("●", rendered)

    def test_view_build_status_returns_to_menu_when_gh_run_list_fails(self) -> None:
        # A transient gh/network failure should report and return to the menu,
        # not raise CommandError out of the whole app.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / STATE_FILE).write_text(json.dumps(app.state_payload()) + "\n")
            failure = subprocess.CompletedProcess(["gh", "run"], 1, "", "network error")
            with patch("atomic_image_builder.Path.cwd", return_value=repo_dir):
                with patch("atomic_image_builder.run", return_value=failure):
                    app.view_build_status()

        self.assertTrue(any(level == "error" for level, _message in stub.messages))
        self.assertTrue(app.gum.prompts)

    def test_view_build_status_falls_back_to_repo_picker_when_not_in_repo(self) -> None:
        # Most sessions never have a local clone of a managed repo, so this
        # falls back to the same picker "Update Existing Image" uses instead
        # of only working when run from inside a clone.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atomic_image_builder.Path.cwd", return_value=Path(tmp)):
                with patch.object(app, "select_repo", return_value=("example", "test-image")) as select_mock:
                    with patch.object(app, "render_build_status") as render_mock:
                        app.view_build_status()
        select_mock.assert_called_once_with(require_state_file=True)
        render_mock.assert_called_once_with("example", "test-image")

    def test_view_build_status_returns_when_repo_picker_is_cancelled(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        stub = GumStub()
        app.gum = stub
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atomic_image_builder.Path.cwd", return_value=Path(tmp)):
                with patch.object(app, "select_repo", side_effect=ScreenBack):
                    with patch.object(app, "render_build_status") as render_mock:
                        app.view_build_status()
        render_mock.assert_not_called()

    def test_test_build_locally_warns_when_podman_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name != "podman"):
            with patch("atomic_image_builder.run") as run_mock:
                app.test_build_locally()

        self.assertTrue(any(level == "warn" and "podman" in message.lower() for level, message in stub.messages))
        run_mock.assert_not_called()

    def test_test_build_locally_degrades_cleanly_when_disabled_by_env(self) -> None:
        app = self.make_app()
        app.config.method = "containerfile"
        stub = GumStub()
        app.gum = stub
        # AIB_DISABLE_LOCAL_BUILD short-circuits before anything is rendered or
        # built, even with podman present (as it is in the container image).
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch.object(app, "seed_project_template") as seed_mock:
                with patch.dict("os.environ", {"AIB_DISABLE_LOCAL_BUILD": "1"}):
                    app.test_build_locally()

        seed_mock.assert_not_called()
        self.assertTrue(any(level == "warn" and "not available" in message for level, message in stub.messages))

    def test_test_build_locally_runs_podman_on_rendered_tree(self) -> None:
        app = self.make_app()
        stub = GumStub()
        commands: list[list[str]] = []
        context_exists: list[bool] = []
        containerfile_exists: list[bool] = []

        def fake_spinner_result(_title, command, *, cwd=None):
            command = list(command)
            commands.append(command)
            context = Path(command[-1])
            context_exists.append(context.exists())
            containerfile_exists.append((context / "Containerfile").exists())
            return subprocess.CompletedProcess(command, 0, "", "")

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            app.test_build_locally()

        self.assertEqual(commands[0][0], "podman")
        self.assertTrue(context_exists[0])
        self.assertTrue(containerfile_exists[0])
        self.assertTrue(any(level == "success" and "atomic-image-builder-local-test:dryrun" in message for level, message in stub.messages))

    def test_test_build_locally_hints_when_method_is_not_containerfile(self) -> None:
        app = self.make_app()
        app.config.method = "bluebuild"
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "seed_project_template") as seed_mock:
            app.test_build_locally()

        seed_mock.assert_not_called()
        self.assertTrue(
            any(level == "hint" and "Containerfile-only" in message for level, message in stub.messages)
        )

    def test_test_build_locally_reports_failure_with_stderr_tail(self) -> None:
        # A non-zero podman exit must be reported as a failure, with only the
        # last eight stderr lines surfaced as the hint.
        app = self.make_app()
        stub = GumStub()
        stderr_lines = [f"line {i}" for i in range(1, 11)]

        def fake_spinner_result(_title, command, *, cwd=None):
            return subprocess.CompletedProcess(list(command), 125, "", "\n".join(stderr_lines))

        stub.spinner_result = fake_spinner_result
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            app.test_build_locally()

        self.assertTrue(
            any(level == "error" and "failed with exit status 125" in message for level, message in stub.messages)
        )
        self.assertFalse(any(level == "success" for level, _message in stub.messages))
        tails = [message for level, message in stub.messages if level == "hint"]
        self.assertIn("\n".join(stderr_lines[-8:]), tails)
        self.assertTrue(all("line 1\n" not in tail for tail in tails))

    def test_test_build_locally_reports_a_failure_with_no_stderr(self) -> None:
        # podman can exit non-zero with nothing on stderr. The failure still
        # has to be reported; there is simply no tail to show beneath it.
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, command, *, cwd=None: subprocess.CompletedProcess(list(command), 1, "", "")
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            app.test_build_locally()

        self.assertTrue(any(level == "error" and "failed with exit status 1" in message for level, message in stub.messages))
        self.assertEqual(stub.messages[-1][0], "error")

    def test_search_packages_counts_nothing_added_when_the_names_are_rejected(self) -> None:
        # add_packages_to_config returns False when validation rejects the
        # names, and the count has to stay at zero. Deriving it from the list
        # length instead would report packages the config never gained.
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"
        stub.choose = lambda options, **_kwargs: [options[0]]
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([("fish", "Friendly interactive shell")], False, None)):
            with patch.object(app, "add_packages_to_config", return_value=False) as add_mock:
                app.search_packages()

        add_mock.assert_called_once()
        self.assertEqual(app.config.packages, [])
        self.assertFalse(any("Added" in prompt for prompt in stub.prompts))

    def test_search_packages_uses_value_delimiter_for_selected_results(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]
        choose_selected: list[str] = []
        choose_options: list[str] = []
        choose_label_delimiter: list[str | None] = [None]
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"

        def fake_choose(options, **kwargs):
            choose_options.extend(options)
            choose_selected.extend(kwargs.get("selected", []))
            choose_label_delimiter[0] = kwargs.get("label_delimiter")
            return ["fish"]

        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([("fish", "Friendly, interactive shell, with extras")], False, None)):
            with patch.object(app, "add_packages_to_config", return_value=False):
                app.search_packages()

        self.assertEqual(choose_selected, ["fish"])
        self.assertEqual(choose_label_delimiter[0], "\t")
        self.assertTrue(choose_options)
        self.assertIn("\tfish", choose_options[0])

    def test_render_containerfile_preserves_existing_text_when_no_from_line_is_patchable(self) -> None:
        app = self.make_app()
        existing = "ARG BASE_IMAGE=ghcr.io/example/custom:latest\n# no FROM line here on purpose\n"
        self.assertEqual(app.render_containerfile(existing), existing)

    def test_write_project_files_writes_generated_cosign_pub(self) -> None:
        app = self.make_app()
        app.generated_cosign_pub = "PUBLIC KEY DATA"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            self.assertEqual((repo_dir / "cosign.pub").read_text(), "PUBLIC KEY DATA\n")

    def test_write_bluebuild_project_files_writes_generated_cosign_pub(self) -> None:
        # The Containerfile-method write path has its own cosign.pub write
        # (test_write_project_files_writes_generated_cosign_pub above); the
        # BlueBuild-method path has a separate one that was untested.
        app = self.make_bluebuild_app()
        app.generated_cosign_pub = "BLUEBUILD PUBLIC KEY DATA"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            self.assertEqual((repo_dir / "cosign.pub").read_text(), "BLUEBUILD PUBLIC KEY DATA\n")

    def test_write_container_project_files_generates_workflow_when_missing(self) -> None:
        # Every other write_project_files(include_workflow=True) test seeds
        # the repo via clone_container_template() first, so the workflow file
        # already exists and only the patch branch runs. This covers the
        # from-scratch generate branch for a repo with no workflow yet.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=True, default_branch="master")
            workflow = (repo_dir / ".github/workflows/build.yml").read_text()
        self.assertEqual(workflow, app.generate_container_workflow(default_branch="master"))

    def test_write_container_project_files_survives_a_missing_justfile_snapshot(self) -> None:
        # The bundled template snapshot can be absent -- a trimmed install, a
        # partial checkout. Restoring a deleted Justfile is best-effort, so a
        # missing snapshot has to skip quietly rather than raise on a repo the
        # user is otherwise updating fine.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.CONTAINERFILE_TEMPLATE_DIR", Path(empty)):
                app.write_project_files(repo_dir, include_workflow=False)
            self.assertFalse((repo_dir / "Justfile").exists())
            # The rest of the materialize step still ran.
            self.assertTrue((repo_dir / "Containerfile").exists())

    def test_write_container_project_files_survives_a_missing_env_snapshot(self) -> None:
        # A repo whose Justfile dotenv-loads image-template.env but has lost
        # the file itself. The restore is keyed on that reference, so it is
        # attempted here -- and must still no-op when the snapshot is gone.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo_dir = Path(tmp)
            (repo_dir / "Justfile").write_text("set dotenv-load := true\nset dotenv-filename := 'image-template.env'\n")
            with patch("atomic_image_builder.CONTAINERFILE_TEMPLATE_DIR", Path(empty)):
                app.write_project_files(repo_dir, include_workflow=False)
            self.assertFalse((repo_dir / "image-template.env").exists())
            self.assertIn("image-template.env", (repo_dir / "Justfile").read_text())

    def test_write_project_files_updates_template_workflow_branch_filters(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            app.write_project_files(repo_dir, include_workflow=True, default_branch="master")
            workflow = (repo_dir / ".github/workflows/build.yml").read_text()
        self.assertIn("  pull_request:\n    branches:\n      - master", workflow)
        self.assertIn("  push:\n    branches:\n      - master", workflow)
        self.assertIn("    runs-on: ubuntu-26.04", workflow)
        self.assertNotIn("ubuntu-24.04", workflow)

    def test_write_project_files_repairs_disk_workflow_and_installer_configs(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            app.write_project_files(repo_dir, include_workflow=True, default_branch="master")
            disk_workflow = (repo_dir / ".github/workflows/build-disk.yml").read_text()
            iso_toml = (repo_dir / "disk_config/iso.toml").read_text()
            iso_gnome = (repo_dir / "disk_config/iso-gnome.toml").read_text()
            iso_kde = (repo_dir / "disk_config/iso-kde.toml").read_text()

        self.assertIn("  pull_request:\n    branches:\n      - master", disk_workflow)
        self.assertIn("'ubuntu-26.04' || 'ubuntu-26.04-arm'", disk_workflow)
        self.assertNotIn("ubuntu-24.04", disk_workflow)
        self.assertIn(ACTION_REF_PINS["osbuild/bootc-image-builder-action@main"][0], disk_workflow)
        self.assertNotIn("osbuild/bootc-image-builder-action@main", disk_workflow)
        self.assertEqual(iso_toml, iso_kde)
        self.assertIn("ghcr.io/example/test-image:latest", iso_toml)
        self.assertIn("ghcr.io/example/test-image:latest", iso_gnome)
        self.assertIn("ghcr.io/example/test-image:latest", iso_kde)
        self.assertNotIn("image-template", iso_toml)
        self.assertNotIn("image-template", iso_gnome)
        self.assertNotIn("image-template", iso_kde)

    def test_generate_container_workflow_uses_default_branch_and_pins_cosign_release(self) -> None:
        app = self.make_app()
        app.config.signing_enabled = True
        workflow = app.generate_container_workflow(default_branch="master")
        self.assertIn("  pull_request:\n    branches:\n      - master", workflow)
        self.assertIn("  push:\n    branches:\n      - master", workflow)
        self.assertIn("    runs-on: ubuntu-26.04", workflow)
        self.assertNotIn("ubuntu-24.04", workflow)
        self.assertIn("          cosign-release: 'v3.1.2'", workflow)
        self.assertIn("--new-bundle-format=false --use-signing-config=false", workflow)

    def test_select_repo_manual_entry_recovers_after_missing_repo(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        existing_label = f"{'existing-repo':<30} (no description)"
        filters = [manual_label, existing_label]
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: filters.pop(0)
        stub.input = lambda **_kwargs: "missing repo"
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with patch.object(app, "gh_json", side_effect=[CommandError("not found")]):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "existing-repo"))
        errors = [msg for level, msg in app.gum.messages if level == "error"]
        self.assertTrue(any("missing-repo" in message for message in errors))
        self.assertEqual(app.gum.prompts, ["Press Enter to choose a different repository..."])

    def test_select_repo_allows_manual_entry_when_no_managed_repos_are_found(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: manual_label
        stub.input = lambda **_kwargs: "managed-repo"
        app.gum = stub
        with patch.object(app, "gh_json_with_spinner", return_value=[]):
            with patch.object(app, "gh_json", return_value={"name": "managed-repo"}):
                with patch.object(app, "repo_has_state_file", return_value=True):
                    owner, repo = app.select_repo(require_state_file=True)

        self.assertEqual((owner, repo), ("example", "managed-repo"))

    def test_select_repo_allows_manual_entry_when_repo_list_payload_is_null(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: manual_label
        stub.input = lambda **_kwargs: "managed-repo"
        app.gum = stub
        with patch.object(app, "gh_json_with_spinner", return_value=None):
            with patch.object(app, "gh_json", return_value={"name": "managed-repo"}):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "managed-repo"))

    def test_select_repo_allows_manual_entry_when_repo_list_fetch_fails(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: manual_label
        stub.input = lambda **_kwargs: "managed-repo"
        app.gum = stub
        with patch.object(app, "gh_json_with_spinner", side_effect=CommandError("gh failed")):
            with patch.object(app, "gh_json", return_value={"name": "managed-repo"}):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "managed-repo"))
        self.assertTrue(any("couldn't load your repository list" in message.lower() for level, message in app.gum.messages if level == "warn"))

    def test_select_repo_manual_entry_reprompts_when_repo_view_payload_is_null(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        existing_label = f"{'existing-repo':<30} (no description)"
        filters = [manual_label, existing_label]
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: filters.pop(0)
        stub.input = lambda **_kwargs: "broken-repo"
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with patch.object(app, "gh_json", side_effect=[None]):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "existing-repo"))
        self.assertTrue(any("Unable to confirm example/broken-repo" in message for level, message in app.gum.messages if level == "error"))
        self.assertEqual(app.gum.prompts, ["Press Enter to choose a different repository..."])

    def test_select_repo_manual_entry_reprompts_when_repo_view_json_is_invalid(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        existing_label = f"{'existing-repo':<30} (no description)"
        filters = [manual_label, existing_label]
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: filters.pop(0)
        stub.input = lambda **_kwargs: "broken-repo"
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with patch.object(app, "gh_json", side_effect=[json.JSONDecodeError("bad json", "x", 0)]):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "existing-repo"))
        self.assertTrue(any("Unable to confirm example/broken-repo" in message for level, message in app.gum.messages if level == "error"))
        self.assertEqual(app.gum.prompts, ["Press Enter to choose a different repository..."])

    def test_select_repo_manual_entry_reprompts_on_empty_input(self) -> None:
        # An empty manual entry loops back to the picker instead of asking
        # GitHub to confirm an empty repository name.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        existing_label = f"{'existing-repo':<30} (no description)"
        filters = [manual_label, existing_label]
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: filters.pop(0)
        stub.input = lambda **_kwargs: "   "
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with patch.object(app, "gh_json") as view_mock:
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "existing-repo"))
        view_mock.assert_not_called()

    def test_select_repo_rejects_manual_repo_without_state_file(self) -> None:
        # require_state_file is the gate that stops the update flow from being
        # pointed at a GitHub repo this tool did not create. A repo without the
        # state file must be refused and the picker re-shown, not returned.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        inputs = iter(["foreign-repo", "managed-repo"])
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: manual_label
        stub.input = lambda **_kwargs: next(inputs)
        app.gum = stub
        with patch.object(app, "gh_json_with_spinner", return_value=[]):
            with patch.object(app, "gh_json", side_effect=[{"name": "foreign-repo"}, {"name": "managed-repo"}]):
                with patch.object(app, "repo_has_state_file", side_effect=[False, True]) as state_mock:
                    owner, repo = app.select_repo(require_state_file=True)

        self.assertEqual((owner, repo), ("example", "managed-repo"))
        self.assertEqual(
            state_mock.call_args_list[0].args,
            ("example", "foreign-repo"),
        )
        self.assertTrue(
            any(
                level == "error" and "example/foreign-repo was not created by this tool" in message
                for level, message in app.gum.messages
            )
        )
        self.assertIn("Press Enter to choose a different repository...", app.gum.prompts)

    def test_select_repo_truncates_long_descriptions_in_the_picker_label(self) -> None:
        # Descriptions over 40 chars are clipped to keep the picker's fixed-width
        # rows aligned; this pins the clip point (37 chars + "...") rather than
        # letting a long GitHub description overflow the row.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        long_description = "x" * 45
        expected_label = f"{'long-repo':<30} {'x' * 37}..."
        seen_options: list[str] = []

        def fake_filter(options, **_kwargs):
            seen_options.extend(options)
            return options[0]

        stub = GumStub()
        stub.filter = fake_filter
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "long-repo", "description": long_description}],
        ):
            owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "long-repo"))
        self.assertIn(expected_label, seen_options)

    def test_select_repo_backs_out_when_github_is_not_available(self) -> None:
        # require_github() gates every entry into the picker; when it returns
        # False (e.g. gh missing or the user declines login) select_repo must
        # back out immediately rather than attempt a repo fetch.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "require_github", return_value=False):
            with patch.object(app, "gh_json_with_spinner") as fetch_mock:
                with self.assertRaises(ScreenBack):
                    app.select_repo()
        fetch_mock.assert_not_called()

    def test_select_repo_backs_out_on_unmapped_picker_choice(self) -> None:
        # gum.filter is expected to return one of the labels handed to it, but
        # if the picker returns a value outside that set (and it isn't the
        # manual-entry sentinel), select_repo must treat it as a back-out
        # instead of raising an unrelated error.
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        stub = GumStub()
        stub.filter = lambda _options, **_kwargs: "some unrecognized choice"
        app.gum = stub
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with self.assertRaises(ScreenBack):
                app.select_repo()

    def test_copy_template_snapshot_errors_when_bundled_snapshot_is_missing(self) -> None:
        # The snapshots are pinned inputs shipped with the tool; a missing one
        # is a packaging fault that must fail closed rather than produce a
        # half-seeded project tree.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            missing_source = Path(tmp) / "no-such-snapshot"
            with self.assertRaisesRegex(CommandError, "Bundled template snapshot not found for acme/template"):
                app.copy_template_snapshot(target, repo="acme/template", source_dir=missing_source)
            self.assertFalse(target.exists())

    def test_copy_template_snapshot_refuses_to_overwrite_non_empty_target(self) -> None:
        # Seeding into a directory that already holds files would clobber the
        # user's work, so the collision is refused and the file left intact.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot"
            source.mkdir()
            (source / "Containerfile").write_text("FROM scratch\n")
            target = Path(tmp) / "project"
            target.mkdir()
            existing = target / "keep-me.txt"
            existing.write_text("user data\n")

            with self.assertRaisesRegex(CommandError, "already exists and is not empty"):
                app.copy_template_snapshot(target, repo="acme/template", source_dir=source)

            self.assertEqual(existing.read_text(), "user data\n")
            self.assertFalse((target / "Containerfile").exists())

    def test_copy_template_snapshot_seeds_into_an_empty_existing_target(self) -> None:
        # An empty target is replaced rather than refused, and the snapshot's
        # metadata files are left out of the generated project.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot"
            source.mkdir()
            (source / "Containerfile").write_text("FROM scratch\n")
            (source / ".template-source").write_text("upstream\n")
            (source / "renovate.json5").write_text("{}\n")
            target = Path(tmp) / "project"
            target.mkdir()

            app.copy_template_snapshot(target, repo="acme/template", source_dir=source)

            self.assertEqual((target / "Containerfile").read_text(), "FROM scratch\n")
            self.assertFalse((target / ".template-source").exists())
            self.assertFalse((target / "renovate.json5").exists())

    def test_repo_default_branch_uses_graphql_result_without_rest_fallback(self) -> None:
        app = self.make_app()
        with patch.object(app, "gh_json", return_value={"defaultBranchRef": {"name": "trunk"}}):
            with patch("atomic_image_builder.run") as run_mock:
                self.assertEqual(app.repo_default_branch("example", "test-image"), "trunk")
        run_mock.assert_not_called()

    def test_repo_default_branch_warns_when_rest_fallback_json_is_invalid(self) -> None:
        # gh can exit 0 with output that is not JSON; the decode failure must
        # fall through to the warning instead of raising.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "gh_json", return_value=None):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["gh", "api"], 0, "not json at all", ""),
            ):
                self.assertEqual(app.repo_default_branch("example", "test-image"), "main")

        self.assertTrue(
            any(level == "warn" and "Could not detect the GitHub default branch" in message for level, message in stub.messages)
        )

    def test_repo_default_branch_warns_when_the_rest_payload_omits_the_branch(self) -> None:
        # Valid JSON, valid object, no default_branch key (or an empty one).
        # Every earlier check passes, so this is the last thing standing
        # between a malformed payload and a workflow whose branch filter
        # points at a branch the repo does not have.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch.object(app, "gh_json", return_value=None):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["gh", "api"], 0, '{"default_branch":""}', ""),
            ):
                self.assertEqual(app.repo_default_branch("example", "test-image"), "main")

        self.assertTrue(
            any(level == "warn" and "Could not detect the GitHub default branch" in message for level, message in stub.messages)
        )

    def test_repo_default_branch_falls_back_to_rest_api_when_graphql_payload_is_null(self) -> None:
        app = self.make_app()

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(list(args), 0, '{"default_branch":"stable"}', "")

        with patch.object(app, "gh_json", return_value=None):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                branch = app.repo_default_branch("example", "test-image")

        self.assertEqual(branch, "stable")

    def test_repo_default_branch_falls_back_to_rest_api_for_empty_repo_null_ref(self) -> None:
        # A freshly created repo with no commits reports defaultBranchRef as
        # null. The key is present with a None value, so this must not crash and
        # should fall through to the REST default_branch lookup.
        app = self.make_app()

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(list(args), 0, '{"default_branch":"main"}', "")

        with patch.object(app, "gh_json", return_value={"defaultBranchRef": None}):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                branch = app.repo_default_branch("example", "test-image")

        self.assertEqual(branch, "main")

    def test_repo_default_branch_falls_back_to_rest_api_when_graphql_query_fails(self) -> None:
        app = self.make_app()

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(list(args), 0, '{"default_branch":"stable"}', "")

        with patch.object(app, "gh_json", side_effect=CommandError("gh failed")):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                branch = app.repo_default_branch("example", "test-image")

        self.assertEqual(branch, "stable")

    def test_repo_default_branch_falls_back_to_rest_api_when_graphql_json_is_invalid(self) -> None:
        app = self.make_app()

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(list(args), 0, '{"default_branch":"stable"}', "")

        with patch.object(app, "gh_json", side_effect=json.JSONDecodeError("bad json", "x", 0)):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                branch = app.repo_default_branch("example", "test-image")

        self.assertEqual(branch, "stable")

    def test_repo_default_branch_warns_when_both_detection_paths_fail(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(list(args), 1, "", "API rate limit exceeded")

        with patch.object(app, "gh_json", side_effect=CommandError("gh failed")):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                branch = app.repo_default_branch("example", "test-image")

        self.assertEqual(branch, "main")
        self.assertTrue(
            any(
                level == "warn" and "default branch" in message
                for level, message in stub.messages
            )
        )

    def test_github_login_name_rejects_null_payload(self) -> None:
        app = self.make_app()
        with patch.object(app, "gh_json", return_value=None):
            with self.assertRaisesRegex(CommandError, "Unable to determine GitHub username"):
                app.github_login_name()

    def test_github_login_name_wraps_api_command_error(self) -> None:
        # The gh failure text is preserved so the caller can report why the
        # lookup failed, not just that it did.
        app = self.make_app()
        with patch.object(app, "gh_json", side_effect=CommandError("gh api exploded")):
            with self.assertRaisesRegex(CommandError, "gh api exploded"):
                app.github_login_name()

    def test_github_login_name_wraps_invalid_json(self) -> None:
        app = self.make_app()
        with patch.object(app, "gh_json", side_effect=json.JSONDecodeError("bad json", "x", 0)):
            with self.assertRaisesRegex(CommandError, "Unable to determine GitHub username"):
                app.github_login_name()

    def test_github_login_name_rejects_missing_login_field(self) -> None:
        app = self.make_app()
        with patch.object(app, "gh_json", return_value={"id": 1}):
            with self.assertRaisesRegex(CommandError, "login field missing"):
                app.github_login_name()

    def test_github_login_name_rejects_blank_login(self) -> None:
        # A whitespace-only login would otherwise be accepted and then used to
        # build owner/repo references.
        app = self.make_app()
        with patch.object(app, "gh_json", return_value={"login": "   "}):
            with self.assertRaisesRegex(CommandError, "login field missing"):
                app.github_login_name()

    def test_github_login_name_strips_surrounding_whitespace(self) -> None:
        app = self.make_app()
        with patch.object(app, "gh_json", return_value={"login": " octocat \n"}):
            self.assertEqual(app.github_login_name(), "octocat")

    def test_update_menu_restores_base_image_when_cancelled(self) -> None:
        app = self.make_app()
        base_choice = app.format_task_choice("Base image", "Bazzite (KDE)")
        choices = [base_choice, "Cancel and go back"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "choose_base_image", side_effect=ScreenBack()):
            result = app.update_menu()

        self.assertFalse(result)
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:stable")
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")

    def test_bundled_template_snapshots_exist(self) -> None:
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / "Containerfile").is_file())
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / ".template-source").is_file())

    def test_current_containerfile_snapshot_uses_upstream_ubuntu_2604_runners(self) -> None:
        build_workflow = (
            CONTAINERFILE_TEMPLATE_DIR / ".github/workflows/build.yml"
        ).read_text()
        disk_workflow = (
            CONTAINERFILE_TEMPLATE_DIR / ".github/workflows/build-disk.yml"
        ).read_text()

        self.assertIn("    runs-on: ubuntu-26.04", build_workflow)
        self.assertIn(
            "    runs-on: ${{ inputs.platform == 'amd64' && "
            "'ubuntu-26.04' || 'ubuntu-26.04-arm' }}",
            disk_workflow,
        )
        self.assertNotIn("ubuntu-24.04", build_workflow)
        self.assertNotIn("ubuntu-24.04", disk_workflow)

    def test_current_containerfile_snapshot_uses_local_ostree_chunker_image(self) -> None:
        justfile = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        self.assertIn(
            'RPM_OSTREE_CHUNKER_IMAGE="localhost/${target_image}:${tag}"',
            justfile,
        )
        # Asserted on the flag rather than its exact line, since upstream
        # reflows this podman invocation (b9783f6 collapsed the first three
        # flags onto one line). The point is that no remote image is pulled.
        self.assertIn("--pull=never", justfile)
        self.assertNotIn('RPM_OSTREE_CHUNKER_IMAGE="quay.io/fedora/fedora-bootc:latest"', justfile)

    def test_current_bluebuild_snapshot_defaults_to_latest_image_version(self) -> None:
        recipe = (BLUEBUILD_TEMPLATE_DIR / "recipes/recipe.yml").read_text()
        self.assertIn("image-version: latest # You can pin to a specific version of Fedora as well", recipe)
        self.assertNotIn("image-version: 42", recipe)

    def test_clone_container_template_uses_bundled_snapshot(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "seeded"
            app.clone_container_template(target)
            self.assertTrue((target / "Containerfile").is_file())
            self.assertFalse((target / ".template-source").exists())

    def test_clone_container_template_wraps_copy_errors(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "seeded"
            with patch("atomic_image_builder.shutil.copytree", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(CommandError, "Unable to copy bundled template snapshot"):
                    app.clone_container_template(target)

    def test_gum_input_raises_screen_back_when_interactive_command_aborts(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 1, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(ScreenBack):
                gum.input(prompt="Repository name: ")

    def test_gum_input_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 130, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(KeyboardInterrupt):
                gum.input(prompt="Repository name: ")

    def test_gum_confirm_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "confirm"], 130, "", "")
        with patch("atomic_image_builder.run", return_value=completed):
            with self.assertRaises(KeyboardInterrupt):
                gum.confirm("Continue?")

    # ── gum.choose / gum.filter / gum.table ─────────────────────────────

    def test_gum_choose_builds_minimal_args_and_returns_selected_lines(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, "alpha\nbeta\n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed) as stdout_mock:
            result = gum.choose(["alpha", "beta", "gamma"])
        self.assertEqual(result, ["alpha", "beta"])
        args, kwargs = stdout_mock.call_args
        call_args = args[0]
        self.assertEqual(call_args[:4], ["gum", "choose", "--no-show-help", "--height"])
        self.assertIn("10", call_args)
        self.assertNotIn("--no-limit", call_args)
        self.assertNotIn("--selected", call_args)
        self.assertNotIn("--header", call_args)
        self.assertEqual(kwargs["stdin"], "alpha\nbeta\ngamma\n")

    def test_gum_choose_includes_optional_flags_when_provided(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, "alpha\n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed) as stdout_mock:
            gum.choose(
                ["alpha", "beta"],
                height=5,
                no_limit=True,
                selected=["alpha"],
                header="Pick one",
                label_delimiter="|",
                cursor_prefix=">",
                selected_prefix="[x]",
                unselected_prefix="[ ]",
            )
        call_args = stdout_mock.call_args[0][0]
        self.assertIn("--no-limit", call_args)
        self.assertIn("--selected", call_args)
        self.assertIn("alpha", call_args[call_args.index("--selected") + 1])
        self.assertIn("--header", call_args)
        self.assertIn("Pick one", call_args)
        self.assertIn("--label-delimiter", call_args)
        self.assertIn("|", call_args)
        self.assertIn("--cursor-prefix", call_args)
        self.assertIn(">", call_args)
        self.assertIn("--selected-prefix", call_args)
        self.assertIn("[x]", call_args)
        self.assertIn("--unselected-prefix", call_args)
        self.assertIn("[ ]", call_args)

    def test_gum_choose_drops_blank_lines_from_output(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, "alpha\n\nbeta\n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            result = gum.choose(["alpha", "beta"])
        self.assertEqual(result, ["alpha", "beta"])

    def test_gum_choose_raises_screen_back_when_cancelled(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "choose"], 1, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(ScreenBack):
                gum.choose(["alpha", "beta"])

    def test_gum_choose_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "choose"], 130, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(KeyboardInterrupt):
                gum.choose(["alpha", "beta"])

    def test_gum_filter_builds_args_and_returns_stripped_result(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "filter"], 0, "  beta  \n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed) as stdout_mock:
            result = gum.filter(["alpha", "beta"], height=15, placeholder="Type to search")
        self.assertEqual(result, "beta")
        args, kwargs = stdout_mock.call_args
        call_args = args[0]
        self.assertEqual(call_args[:3], ["gum", "filter", "--no-show-help"])
        self.assertIn("15", call_args)
        self.assertIn("--placeholder", call_args)
        self.assertIn("Type to search", call_args)
        self.assertEqual(kwargs["stdin"], "alpha\nbeta\n")

    def test_gum_filter_raises_screen_back_when_cancelled(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "filter"], 1, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(ScreenBack):
                gum.filter(["alpha", "beta"])

    def test_gum_table_builds_args_and_stdin_from_rows(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "table"], 0, "", "")
        with patch("atomic_image_builder.run", return_value=completed) as run_mock:
            gum.table([["a", "1"], ["b", "2"]], columns="Name,Count", widths="10,5")
        args, kwargs = run_mock.call_args
        call_args = args[0]
        # --print matters: without it `gum table` is an interactive row picker
        # that draws the rows and then blocks on "1/4 navigate / enter select",
        # so every screen with a table stopped there and nothing after it ran.
        self.assertEqual(
            call_args,
            ["gum", "table", "--print", "--separator", "\t", "--columns", "Name,Count", "--widths", "10,5"],
        )
        self.assertEqual(kwargs["capture"], False)
        self.assertEqual(kwargs["stdin"], "a\t1\nb\t2\n")

    def test_gum_pager_pipes_text_to_gum_pager(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run") as run_mock:
            gum.pager("some long text\nmore lines")
        run_mock.assert_called_once_with(["gum", "pager"], capture=False, stdin="some long text\nmore lines")

    def test_gum_enter_to_continue_shows_instruction_then_waits_for_input(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 0, "", "")
        with patch.object(Gum, "instruction") as instruction_mock:
            with patch.object(Gum, "interactive_stdout", return_value=completed) as stdout_mock:
                gum.enter_to_continue("Press Enter to continue...")

        instruction_mock.assert_called_once_with("Press Enter to continue...")
        args = stdout_mock.call_args[0][0]
        self.assertEqual(args[:2], ["gum", "input"])
        self.assertIn("--width", args)
        self.assertEqual(args[args.index("--width") + 1], "3")

    def test_gum_enter_to_continue_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 130, "", "")
        with patch.object(Gum, "instruction"):
            with patch.object(Gum, "interactive_stdout", return_value=completed):
                with self.assertRaises(KeyboardInterrupt):
                    gum.enter_to_continue()

    # ── gum flag-injection guards ───────────────────────────────────────
    # gum parses any leading-dash positional as a flag and exits 80. Captured
    # command output routinely starts with a dash (buildah "--> <layer>" step
    # markers, diff lines), and run(check=True) would turn that into a
    # CommandError raised from a display call, unwinding the whole wizard.

    def captured_args(self, call) -> list[str]:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum"], 0, "", "")
        with patch("atomic_image_builder.run", return_value=completed) as run_mock:
            call(gum)
        return list(run_mock.call_args[0][0])

    def assert_dash_text_is_positional(self, args: list[str], text: str) -> None:
        self.assertIn("--", args, f"missing -- separator in {args!r}")
        self.assertIn(text, args)
        self.assertLess(args.index("--"), args.index(text))

    def test_gum_style_passes_dash_leading_text_after_separator(self) -> None:
        text = "--> 8a3f0c1 error building layer"
        args = self.captured_args(lambda g: g.style(text, width=40))
        self.assert_dash_text_is_positional(args, text)

    def test_gum_log_passes_dash_leading_text_after_separator(self) -> None:
        text = "--force is not supported here"
        args = self.captured_args(lambda g: g.error(text))
        self.assert_dash_text_is_positional(args, text)

    def test_gum_confirm_keeps_default_flag_before_separator(self) -> None:
        # The prompt must sit after the separator while --default stays a flag.
        prompt = "--> retry the build?"
        args = self.captured_args(lambda g: g.confirm(prompt, default=True))
        self.assert_dash_text_is_positional(args, prompt)
        self.assertIn("--default=true", args)
        self.assertLess(args.index("--default=true"), args.index("--"))

    def test_gum_style_omits_a_boolean_flag_that_is_false(self) -> None:
        # Boolean options are flag-presence, not flag-with-value: passing
        # bold=False has to leave --bold off entirely, because "--bold false"
        # is not something gum understands.
        gum = Gum()
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess([], 0, "styled", "")) as run_mock:
            gum.style("text", bold=False, italic=True)
        args = run_mock.call_args.args[0]
        self.assertNotIn("--bold", args)
        self.assertIn("--italic", args)

    def test_gum_header_can_leave_the_screen_alone(self) -> None:
        # Screens that print a header partway through their own output must
        # not wipe what they have already shown.
        gum = Gum()
        with patch.object(gum, "clear") as clear_mock:
            with patch.object(gum, "style", return_value="styled"):
                with redirect_stdout(io.StringIO()):
                    gum.header("Scan Results", clear_screen=False)
        clear_mock.assert_not_called()

    def test_gum_style_survives_real_gum_with_dash_text(self) -> None:
        # Integration check against the actual binary, skipped when absent.
        if shutil.which("gum") is None:
            self.skipTest("gum is not installed")
        Gum().style("--> 8a3f0c1 step marker", width=40)

    # ── menu-screen error containment ───────────────────────────────────

    def test_run_screen_action_contains_command_error(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub

        def boom() -> None:
            raise CommandError("Invalid package value: bad;rm")

        app.run_screen_action(boom, return_hint="Press Enter to return...")
        self.assertTrue(
            any(level == "error" and "bad;rm" in message for level, message in stub.messages)
        )
        self.assertIn("Press Enter to return...", stub.prompts)

    def test_run_screen_action_lets_screen_back_through(self) -> None:
        # ScreenBack is navigation, not failure: the caller still owns it.
        app = self.make_app()
        app.gum = GumStub()

        def back() -> None:
            raise ScreenBack()

        with self.assertRaises(ScreenBack):
            app.run_screen_action(back, return_hint="unused")

    def test_review_new_image_keeps_config_when_local_build_fails(self) -> None:
        # A failing local test build must return to the review screen with the
        # wizard's state intact, not unwind to main_menu and discard it.
        app = self.make_app()
        app.config.method = "containerfile"
        app.config.packages = ["tmux", "ripgrep"]

        class Stub(GumStub):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def choose(self, options, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return [next(o for o in options if "Test build locally" in o)]
                return [next(o for o in options if o.startswith("Start GitHub build"))]

        app.gum = Stub()
        with patch.object(
            App, "test_build_locally", side_effect=CommandError("Invalid package value: bad;rm")
        ):
            action = app.review_new_image(step=5, total_steps=5)

        self.assertEqual(action, "build")
        self.assertEqual(app.config.packages, ["tmux", "ripgrep"])
        self.assertEqual(app.config.method, "containerfile")
        self.assertTrue(
            any(level == "error" and "bad;rm" in message for level, message in app.gum.messages)
        )

    def test_update_task_choices_show_current_status(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "ripgrep"]
        app.config.copr_repos = ["foo/bar"]
        app.config.services = ["sshd.service"]
        app.config.removed_packages = ["vim-enhanced"]
        choices = dict(app.update_task_choices())
        self.assertEqual(choices["Packages"], "tmux, ripgrep")
        self.assertEqual(choices["COPR repositories"], "foo/bar")
        self.assertEqual(choices["Services"], "sshd.service")
        self.assertEqual(choices["Removed base packages"], "vim-enhanced")

    def test_pager_text_with_hint_puts_exit_instruction_in_pager(self) -> None:
        app = self.make_app()
        text = app.pager_text_with_hint("diff --git a/file b/file\n+new line\n")
        self.assertTrue(text.startswith("Press q to close this diff and return to the previous screen."))
        self.assertIn("diff --git a/file b/file", text)

    def test_pager_text_with_hint_handles_empty_body(self) -> None:
        app = self.make_app()
        text = app.pager_text_with_hint("")
        self.assertEqual(text, "Press q to close this diff and return to the previous screen.\n")

    def test_read_only_pager_text_includes_title_hint_and_lines(self) -> None:
        app = self.make_app()
        text = app.read_only_pager_text("Build Status", ["run-1: success", "run-2: failure"])
        self.assertEqual(
            text,
            "Build Status\n\n"
            "Press q to close this screen and return to the previous menu.\n\n"
            "run-1: success\nrun-2: failure\n",
        )

    def test_read_only_pager_text_handles_no_lines(self) -> None:
        app = self.make_app()
        text = app.read_only_pager_text("Build Status", [])
        self.assertEqual(
            text,
            "Build Status\n\nPress q to close this screen and return to the previous menu.\n",
        )

    def test_format_key_value_rows_aligns_labels(self) -> None:
        app = self.make_app()
        rows = app.format_key_value_rows([("Repo", "example/test-image"), ("Method", "containerfile")])
        self.assertEqual(rows, ["Repo    example/test-image", "Method  containerfile"])

    def test_format_key_value_rows_handles_no_rows(self) -> None:
        app = self.make_app()
        self.assertEqual(app.format_key_value_rows([]), [])

    def test_repo_full_diff_includes_untracked_files(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            (repo_dir / "new.txt").write_text("hello world\n")
            diff = app.repo_full_diff(repo_dir)

        self.assertIn("diff --git", diff)
        self.assertIn("new.txt", diff)
        self.assertIn("+hello world", diff)

    def test_repo_diff_summary_includes_tracked_and_untracked_files(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
            tracked = repo_dir / "tracked.txt"
            tracked.write_text("before\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)
            tracked.write_text("after\n")
            (repo_dir / "cosign.pub").write_text("PUBLIC KEY\n")

            summary = app.repo_diff_summary(repo_dir)

        self.assertIn("tracked.txt", summary)
        self.assertIn("?? cosign.pub", summary)

    def test_repo_untracked_files_fails_closed_when_git_probe_fails(self) -> None:
        app = self.make_app()
        failed = subprocess.CompletedProcess(
            ["git", "ls-files"], 128, "", "fatal: not a git repository"
        )
        with patch("atomic_image_builder.run", return_value=failed):
            with self.assertRaisesRegex(CommandError, "Could not enumerate untracked files"):
                app.repo_untracked_files(Path("/tmp/not-a-repository"))

    def test_repo_full_diff_fails_closed_when_untracked_diff_fails(self) -> None:
        app = self.make_app()
        results = [
            subprocess.CompletedProcess(["git", "diff"], 0, "", ""),
            subprocess.CompletedProcess(["git", "ls-files"], 0, "cosign.pub\0", ""),
            subprocess.CompletedProcess(
                ["git", "diff", "--no-index"], 128, "", "fatal: unable to read cosign.pub"
            ),
        ]
        with patch("atomic_image_builder.run", side_effect=results):
            with self.assertRaisesRegex(CommandError, "Could not generate a full diff for untracked file cosign.pub"):
                app.repo_full_diff(Path("/tmp/example-repository"))

    def test_repo_full_diff_skips_an_untracked_file_with_an_empty_diff(self) -> None:
        # git diff --no-index exits 0 with no output for an empty file. That
        # is not a failure, and appending it would put a stray blank block in
        # the diff the user is asked to approve.
        app = self.make_app()
        results = [
            subprocess.CompletedProcess(["git", "diff"], 0, "", ""),
            subprocess.CompletedProcess(["git", "ls-files"], 0, "empty.txt\0", ""),
            subprocess.CompletedProcess(["git", "diff", "--no-index"], 0, "", ""),
        ]
        with patch("atomic_image_builder.run", side_effect=results):
            self.assertEqual(app.repo_full_diff(Path("/tmp/example-repository")), "")

    def test_repo_full_diff_includes_nested_untracked_files(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            nested = repo_dir / "new directory" / "nested.txt"
            nested.parent.mkdir()
            nested.write_text("nested content\n")
            diff = app.repo_full_diff(repo_dir)

        self.assertIn("new directory/nested.txt", diff)
        self.assertIn("+nested content", diff)

    def test_repo_full_diff_returns_empty_string_when_nothing_changed(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            diff = app.repo_full_diff(repo_dir)

        self.assertEqual(diff, "")

    def test_contrib_wrapper_does_not_put_github_token_in_podman_argv(self) -> None:
        wrapper = (Path(__file__).resolve().parents[1] / "contrib/aib").read_text()
        self.assertIn("podman_args+=(-e GH_TOKEN)", wrapper)
        self.assertIn('GH_TOKEN="$(gh auth token)"', wrapper)
        self.assertNotIn('GH_TOKEN=$(gh auth token)', wrapper)

    def test_contrib_wrapper_checks_for_a_newer_image_on_every_run(self) -> None:
        # Podman's default (--pull=missing) would pin wrapper users to whatever
        # copy they first fetched. The tool bakes in its own action pins and
        # template snapshots, so a stale image generates repos from stale pins.
        wrapper = (Path(__file__).resolve().parents[1] / "contrib/aib").read_text()
        self.assertIn("--pull=newer", wrapper)
        self.assertIn("podman_args=(--rm -it --pull=newer)", wrapper)

    def test_docs_document_pulling_a_newer_image_for_container_runs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installing = (root / "docs/installing.md").read_text()
        self.assertIn("podman run --rm -it --pull=newer", installing)
        # distrobox enter never re-pulls, so --pull=newer cannot cover it and
        # the recreate step has to be written down instead.
        self.assertIn("distrobox create --name aib --pull --image", installing)
        self.assertIn("distrobox rm aib", installing)
        # The README is deliberately short, but it must still lead somewhere.
        self.assertIn("docs/installing.md", (root / "README.md").read_text())

    def test_readme_coverage_badge_links_to_the_explainer(self) -> None:
        # Clicking the badge used to land on the raw trend CSV -- a wall of
        # date/SHA/percentage rows that explains nothing to a reader who does
        # not already know what coverage is. It points at the explainer
        # instead, which is the only thing in the repo that does explain it.
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("Unit coverage", readme)
        self.assertIn("coverage-unit.json)](docs/coverage.md)", readme)
        self.assertNotIn("coverage-trend.csv", readme)

    def test_readme_points_security_reports_at_security_md(self) -> None:
        # A public issue is the wrong place for a live vulnerability; the
        # README's Feedback section is where a reporter looks first, so the
        # redirect has to live there rather than only in SECURITY.md itself.
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("[SECURITY.md](SECURITY.md)", readme)

    def test_security_md_points_at_a_real_reporting_path(self) -> None:
        # GitHub only renders a working "Report a vulnerability" button when
        # private vulnerability reporting is enabled for the repo -- pointing
        # at that URL while the feature is off would be a broken instruction.
        # This only pins the file's own content; it cannot check the repo
        # setting itself.
        security = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text()
        self.assertIn("security/advisories/new", security)
        self.assertIn("do not open a public issue", security.lower())

    def test_readme_points_contributors_at_contributing_md(self) -> None:
        # Someone who wants to fix a bug rather than just report it has no way
        # to find CONTRIBUTING.md without this -- it is not linked from
        # anywhere else a first-time visitor would land.
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("[CONTRIBUTING.md](CONTRIBUTING.md)", readme)

    def test_contributing_md_explains_how_to_submit_a_change(self) -> None:
        # Passing tests/coverage/lint locally still leaves "what do I do with
        # this" unanswered without an explicit fork/branch/PR walkthrough.
        contributing = (Path(__file__).resolve().parents[1] / "CONTRIBUTING.md").read_text()
        self.assertIn("## Submitting a change", contributing)
        self.assertIn("Fork the repo", contributing)
        self.assertIn("pull request", contributing.lower())

    def test_contributing_md_pins_the_same_tooling_versions_as_ci(self) -> None:
        # Read ci.yml's own pin instead of hardcoding it a second time here --
        # a bumped CI version would otherwise leave the doc silently stale
        # with a test that still passes.
        root = Path(__file__).resolve().parents[1]
        ci_workflow = (root / ".github/workflows/ci.yml").read_text()
        match = re.search(r"pip install coverage==\S+ ruff==\S+", ci_workflow)
        self.assertIsNotNone(match, "ci.yml's pinned tooling install line has changed shape")
        contributing = (root / "CONTRIBUTING.md").read_text()
        self.assertIn(match.group(0), contributing)

    def test_ruff_config_lives_in_exactly_one_file(self) -> None:
        # ruff reads `.ruff.toml` in preference to `ruff.toml`, so a repo that
        # has both silently lints against the dotted one while edits land in
        # the other. Which name wins does not matter; having only one does.
        root = Path(__file__).resolve().parents[1]
        present = [name for name in ("ruff.toml", ".ruff.toml") if (root / name).exists()]
        self.assertEqual(present, ["ruff.toml"])

    def test_coverage_gate_threshold_has_one_source_of_truth(self) -> None:
        # The gate number was spelled out in five places: ci.yml plus four
        # sentences across three docs. Nothing tied them together, so moving
        # the gate meant finding all five by memory and a doc left behind
        # would go on quoting the old number indefinitely. ci.yml now reads
        # .coverage-thresholds.json, and this asserts everything else agrees
        # with it.
        root = Path(__file__).resolve().parents[1]
        thresholds = json.loads((root / ".coverage-thresholds.json").read_text())
        threshold = thresholds["gated"]["unit"]
        self.assertIsInstance(threshold, int)

        ci_workflow = (root / ".github/workflows/ci.yml").read_text()
        self.assertIn(".coverage-thresholds.json", ci_workflow)
        # A literal here would silently win over the file it reads.
        self.assertNotRegex(ci_workflow, r"--fail-under=\d")

        # Checking each doc merely mentions the threshold somewhere is not
        # enough: CONTRIBUTING.md names it three times, so updating one and
        # leaving the others would still pass while two sentences went on
        # quoting the old number. Every line that talks about the gate is
        # checked instead, and each number on such a line has to be the
        # current threshold.
        gate_line = re.compile(
            r"fail-under|coverage gate|gates on it|fails the build|the gate"
        )
        # Bare \b\d+\b rather than a percentage: "The 90 is not written into
        # ci.yml" is a gate claim with no % sign, and is exactly the kind of
        # sentence that gets left behind. The word boundary keeps `python3`
        # and version pins out of it.
        number = re.compile(r"\b\d+\b")
        # Clock times and ISO dates are stripped before numbers are read. A
        # line can legitimately mention the gate and a schedule in the same
        # breath -- "Daily 04:00 UTC ... re-runs the gate" -- and reading 04
        # as a stale threshold is a false positive that would push docs into
        # avoiding the word rather than into being correct.
        not_a_threshold = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")

        documented = [
            "CONTRIBUTING.md",
            "maintainer_docs/MAINTAINER.md",
            "docs/coverage.md",
            # The PR template's checklist is a command a contributor copies,
            # so a stale threshold there is one they run and believe.
            ".github/pull_request_template.md",
        ]
        for relative_path in documented:
            checked = 0
            for lineno, line in enumerate(
                (root / relative_path).read_text().splitlines(), start=1
            ):
                if not gate_line.search(line):
                    continue
                for found in number.findall(not_a_threshold.sub(" ", line)):
                    checked += 1
                    self.assertEqual(
                        int(found),
                        threshold,
                        f"{relative_path}:{lineno} quotes {found}, "
                        f"not the {threshold} in .coverage-thresholds.json",
                    )
            # A rewrite that phrases the gate out of every pattern above
            # would otherwise pass by checking nothing at all.
            self.assertTrue(
                checked,
                f"{relative_path} no longer states the gate threshold anywhere "
                f"this test recognises",
            )

    def test_every_workflow_that_gates_on_coverage_reads_the_threshold_file(self) -> None:
        # The test above proves ci.yml reads .coverage-thresholds.json rather
        # than spelling the number, but it names ci.yml specifically. Two more
        # workflows now run the same gate -- nightly-compliance.yml re-runs it
        # from main, ai-fix.yml runs it inside the agent loop -- and a literal
        # in either would be the copy that #144 removed, reintroduced where
        # nothing looks. Discovered by scanning rather than listed, so a
        # fourth workflow is covered the day it is added.
        root = Path(__file__).resolve().parents[1]
        gating = {}
        for workflow_path in sorted((root / ".github/workflows").iterdir()):
            if workflow_path.suffix not in {".yml", ".yaml"}:
                continue
            text = workflow_path.read_text()
            if "--fail-under" in text:
                gating[str(workflow_path.relative_to(root))] = text

        # A rename that moved the gate out of every workflow would otherwise
        # pass by checking nothing at all.
        self.assertTrue(gating, "no workflow runs the coverage gate any more")

        for relative_path, text in gating.items():
            self.assertIn(
                ".coverage-thresholds.json",
                text,
                f"{relative_path} runs the coverage gate without reading "
                f"the threshold from .coverage-thresholds.json",
            )
            # A literal here would silently win over the file it reads, the
            # same way it would in ci.yml. Checked per line so the failure
            # names the offending one rather than printing the workflow.
            for lineno, line in enumerate(text.splitlines(), start=1):
                self.assertNotRegex(
                    line.strip(),
                    # Either quote style, or none: a literal is a literal
                    # however it is written, and `--fail-under='90'` slipped
                    # through when only the double quote was allowed.
                    r"--fail-under=['\"]?\d",
                    f"{relative_path}:{lineno} spells the gate threshold out "
                    f"instead of reading .coverage-thresholds.json",
                )

    def test_quality_index_quotes_the_gate_from_its_source(self) -> None:
        # docs/quality.md's signal table is the sixth place the gate number
        # appears, and the one most likely to be read first, since it is the
        # index every other quality document is reached from. The doc sweep
        # above walks a fixed list of four files and this is not one of them,
        # so a moved gate would leave this table quoting the old number to the
        # readers most likely to trust it. Asserted per column rather than per
        # line, because the same row also says "Flat at 100%" -- a true
        # statement about the current measurement, not a stale threshold.
        root = Path(__file__).resolve().parents[1]
        threshold = json.loads((root / ".coverage-thresholds.json").read_text())["gated"]["unit"]

        rows = []
        for line in (root / "docs/quality.md").read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) == 4 and not set(cells[0]) <= {"-", ":"}:
                rows.append(cells)
        self.assertTrue(rows, "docs/quality.md no longer has a signal table")

        gated_rows = [row for row in rows if row[0] == "Unit coverage"]
        self.assertEqual(
            len(gated_rows),
            1,
            "docs/quality.md's signal table no longer has exactly one "
            "'Unit coverage' row",
        )
        self.assertIn(
            f"{threshold}%",
            gated_rows[0][1],
            f"docs/quality.md's unit row quotes {gated_rows[0][1]!r}, not the "
            f"{threshold} in .coverage-thresholds.json",
        )

        # And no other row may carry a number in the Gated? column: the
        # advisory tiers have no threshold to quote, so a number appearing
        # there is either a second copy of the gate or an ungated tier being
        # described as though it had one.
        for row in rows[1:]:
            if row[0] == "Unit coverage":
                continue
            self.assertNotRegex(
                row[1],
                r"\d",
                f"docs/quality.md's {row[0]!r} row quotes a threshold in the "
                f"Gated? column, but only the unit tier is gated",
            )

    def test_ci_runs_the_end_to_end_suites_from_the_repo(self) -> None:
        # The point of tests/e2e/ is that the same scenarios CI runs can be
        # run against a locally built image. That holds only while ci.yml
        # calls the scripts instead of carrying its own copy, so assert the
        # scripts exist, are executable, and are what the workflow invokes.
        root = Path(__file__).resolve().parents[1]
        ci_workflow = (root / ".github/workflows/ci.yml").read_text()
        for name in ("smoke.sh", "coverage_scenarios.sh"):
            script = root / "tests/e2e" / name
            self.assertTrue(script.is_file(), f"tests/e2e/{name} is missing")
            self.assertTrue(
                os.access(script, os.X_OK),
                f"tests/e2e/{name} is not executable, so ci.yml cannot run it",
            )
            self.assertTrue(
                f"tests/e2e/{name}" in ci_workflow,
                f"ci.yml no longer runs tests/e2e/{name}",
            )
        # shellcheck covers them in the `test` job, which always runs --
        # container-build is path-scoped and skips on changes that miss the
        # image, so a lint gate placed there would only run sometimes.
        self.assertTrue(
            "tests/e2e/lib.sh" in ci_workflow,
            "ci.yml no longer shellchecks tests/e2e/lib.sh",
        )
        # And container-build has to treat the suites as a trigger. They are
        # not baked into the image, so they do not belong in that path list
        # on the usual reasoning -- but a PR that edits only a suite would
        # otherwise skip the job that runs it and read as covered.
        diff_paths = re.search(r"git diff --quiet .*?;", ci_workflow)
        self.assertIsNotNone(diff_paths, "container-build's path filter has changed shape")
        self.assertIn("tests/e2e/", diff_paths.group(0))

    def test_agent_guidance_has_one_canonical_file(self) -> None:
        # Four ACMM criteria want agent-facing files that all say the same
        # thing. Writing that content out four times would give the repo four
        # copies of its own traps to keep in step, which is the drift the
        # coverage-threshold test above exists to prevent. So one file carries
        # the content and the rest defer to it.
        root = Path(__file__).resolve().parents[1]
        canonical = root / ".github/copilot-instructions.md"
        self.assertTrue(canonical.is_file(), "the canonical agent guidance file is missing")

        # Every other agent entry point, whichever of them exist. Listed by
        # directory so a new file inside one is covered the day it is added
        # rather than the day someone remembers this test.
        # Tracked files only. The working tree is the wrong thing to assert
        # about: a maintainer keeping a personal AGENTS.md or a scratch note
        # under .claude/memory/ would otherwise get a red suite for a file the
        # repository does not ship. `.mdc` is included because that is
        # Cursor's own rule format, so a pointer written to actually work in
        # Cursor would slip past this check entirely.
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        pointer_prefixes = (
            ".cursor/",
            ".github/prompts/",
            ".claude/skills/",
            ".claude/memory/",
        )
        # CLAUDE.md and AGENTS.md sit at the root rather than in a directory of
        # their own, and each is loaded automatically by the tool that reads
        # it -- which makes them the likeliest place for a second set of
        # conventions to appear and go unnoticed.
        pointer_files = ("CLAUDE.md", "AGENTS.md", ".claude/checkpoint.md")
        pointers = [
            root / name
            for name in sorted(tracked)
            if name.endswith((".md", ".mdc"))
            and (name.startswith(pointer_prefixes) or name in pointer_files)
        ]

        for path in pointers:
            relative_path = path.relative_to(root)
            text = path.read_text()
            self.assertIn(
                "copilot-instructions.md",
                text,
                f"{relative_path} does not point at the canonical guidance",
            )
            # Copying is what has to be caught, and naming a thing is not
            # copying: a task prompt about refreshing the action pins has to
            # say ACTION_PINS, and a correction log has to name what was
            # corrected. Blocking those words would push both into vagueness
            # to satisfy a test. Verbatim sentences are the real signal, so
            # that is what is checked.
            canonical_text = canonical.read_text()
            canonical_prose = _markdown_prose(canonical_text)
            # A pointer may mirror a few high-consequence paragraphs inside an
            # explicit block. That exists because a link is inert: CLAUDE.md is
            # auto-loaded and the canonical brief is not, so a trap that only
            # lives behind the link costs a tool call an agent may not spend.
            # The rule therefore inverts inside the block -- every line there
            # must match the canonical file -- which turns the duplication
            # into a mirror that cannot drift rather than a second copy.
            # Only CLAUDE.md may mirror. Elsewhere the markers are just text,
            # and treating them as an exemption would let any pointer opt out
            # of the no-copy rule by wrapping a paragraph in them.
            if path.name != "CLAUDE.md":
                self.assertNotIn(
                    MIRROR_START,
                    text,
                    f"{relative_path} uses the mirror markers, which only "
                    f"CLAUDE.md may do -- it is the only auto-loaded file",
                )
            in_mirror = False
            mirror_lines: list[str] = []
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped == MIRROR_START:
                    in_mirror = True
                    continue
                if stripped == MIRROR_END:
                    in_mirror = False
                    continue
                if in_mirror:
                    mirror_lines.append(line.rstrip())
                    continue
                # assertTrue rather than assertNotIn: the latter prints the
                # whole canonical file as the container, burying the one line
                # that has to change.
                self.assertTrue(
                    stripped not in canonical_prose,
                    f"{relative_path}:{lineno} copies a sentence from "
                    f".github/copilot-instructions.md outside the mirror "
                    f"block: {stripped[:60]!r}",
                )
            self.assertFalse(in_mirror, f"{relative_path} opens a mirror block and never closes it")
            if path.name != "CLAUDE.md":
                continue

            # The delivery half, and it has to compare the whole selection
            # rather than line membership. Checking that each mirrored line
            # appears somewhere in the canonical file would pass a block with
            # three of the four paragraphs deleted, or one unrelated sentence
            # -- both of which deliver nothing while looking correct.
            paragraphs = [
                paragraph
                for paragraph in canonical_text.split("\n\n")
                if paragraph.startswith("**")
            ]
            expected = []
            for opening in MIRRORED_TRAPS:
                match = [p for p in paragraphs if p.startswith(opening)]
                self.assertEqual(
                    len(match),
                    1,
                    f".github/copilot-instructions.md has no single paragraph "
                    f"opening {opening!r}; CLAUDE.md's mirror names it",
                )
                expected.append(match[0].rstrip())
            actual = "\n".join(mirror_lines).strip()
            self.assertEqual(
                actual,
                "\n\n".join(expected),
                "CLAUDE.md's mirror block is not the four canonical "
                "paragraphs verbatim -- regenerate it from "
                ".github/copilot-instructions.md",
            )

    def test_checkpoint_entries_are_all_dated(self) -> None:
        # The checkpoint holds no current-state claims -- those are looked up
        # live -- so everything in it is a dated historical fact, which is
        # what stops it rotting the way a session summary normally does. An
        # undated entry is how that starts, so it is a failure rather than a
        # style note. The file says a test enforces this; this is it.
        root = Path(__file__).resolve().parents[1]
        checkpoint = root / ".claude/checkpoint.md"
        self.assertTrue(checkpoint.is_file(), ".claude/checkpoint.md is missing")

        # Entries are bold-led paragraphs under the content sections. The
        # sections explaining the format do not carry entries and are skipped.
        explanatory = {"Checkpoint", "Why this file cannot go stale"}
        section = None
        entries = 0
        for lineno, line in enumerate(checkpoint.read_text().splitlines(), start=1):
            heading = re.match(r"^#+\s+(.*)$", line)
            if heading:
                section = heading.group(1).strip()
                continue
            if section in explanatory or not line.startswith("**"):
                continue
            entries += 1
            self.assertRegex(
                line,
                r"^\*\*\d{4}-\d{2}-\d{2} ",
                f".claude/checkpoint.md:{lineno} starts an entry without a "
                f"leading ISO date, which is what keeps the file from rotting",
            )
        # A rewrite that drops every entry would otherwise pass by checking
        # nothing, and an empty checkpoint is not a checkpoint.
        self.assertGreater(entries, 0, ".claude/checkpoint.md has no dated entries")

    def test_auto_qa_declaration_names_the_gate_without_copying_it(self) -> None:
        # .github/auto-qa-tuning.json declares that the gate is never moved by
        # a machine. The temptation is to restate the number there for
        # readability, which would make it the fifth copy and undo #144. It
        # names the file instead, and this asserts both halves: that the path
        # it points at is the real one, and that no threshold value is written
        # into it.
        root = Path(__file__).resolve().parents[1]
        declaration = root / ".github/auto-qa-tuning.json"
        self.assertTrue(declaration.is_file(), ".github/auto-qa-tuning.json is missing")

        raw = declaration.read_text()
        data = json.loads(raw)
        source = data["never_auto_tuned"]["unit_coverage_gate"]["source_of_truth"]
        self.assertEqual(source, ".coverage-thresholds.json")
        self.assertTrue(
            (root / source).is_file(),
            f"the declaration points at {source}, which does not exist",
        )

        thresholds = json.loads((root / source).read_text())
        self.assertNotIn(
            str(thresholds["gated"]["unit"]),
            raw,
            "the declaration copies the threshold value instead of naming its source",
        )

        # The advisory tier list is the same duplication one level down: the
        # declaration restates it for readability, so adding or renaming a
        # tier in the source could leave this file quietly describing a
        # policy the repo no longer has.
        self.assertEqual(
            data["advisory_and_deliberately_untuned"]["tiers"],
            thresholds["advisory"],
            "the declaration's advisory tiers disagree with "
            ".coverage-thresholds.json",
        )

    def test_markdown_tables_are_aligned_for_a_fixed_width_reader(self) -> None:
        # Most of this repo's documentation is read in a terminal, a pager or
        # a diff, not a browser. A table that renders fine in HTML is
        # unreadable there unless the pipes line up, so alignment is the
        # default rather than a tidy-up: format_markdown_tables.py fixes a
        # file, and this fails when one drifts.
        #
        # template_snapshots/ is excluded by the formatter itself -- it is a
        # pinned upstream copy, and reformatting it would be a defect however
        # it looks.
        import format_markdown_tables

        root = Path(__file__).resolve().parents[1]
        unaligned = [
            str(path.relative_to(root))
            for path in format_markdown_tables.tracked_markdown(root)
            if format_markdown_tables.format_text(path.read_text()) != path.read_text()
        ]
        self.assertEqual(
            unaligned,
            [],
            "run python3 format_markdown_tables.py to align these",
        )

    def test_every_row_of_a_tracked_table_ends_at_the_same_column(self) -> None:
        # The check above asks whether the formatter would change the file,
        # which only ever proved the docs are a fixed point of format_text --
        # not that they are aligned. A table the formatter renders wrong is a
        # fixed point too, so that check passed straight over a delimiter row
        # drawn wider than its own table. This asserts the guarantee itself,
        # reading the committed text rather than the tool that writes it, so a
        # bug in the width arithmetic cannot vouch for its own output.
        import format_markdown_tables

        root = Path(__file__).resolve().parents[1]
        ragged: list[str] = []
        for path in format_markdown_tables.tracked_markdown(root):
            block: list[tuple[int, str]] = []

            def close(block: list[tuple[int, str]], path: Path = path) -> None:
                # Two lines is a header and a delimiter -- the shortest thing
                # that is a table at all.
                if len(block) < 2 or not format_markdown_tables.is_delimiter(
                    format_markdown_tables.split_row(block[1][1]) or []
                ):
                    return
                if len({len(line) for _, line in block}) > 1:
                    name = path.relative_to(root)
                    ragged.append(f"{name}:{block[0][0]}")

            # Which lines are code comes from the module, so this check and
            # the formatter cannot disagree about it: a check that read a
            # four-backtick example or a four-space indented one as prose
            # would report it as a ragged table, and the formatter would then
            # correctly refuse to touch it -- a failure with no way to clear
            # it. The alignment arithmetic below, which is what this test
            # exists to check independently, is still its own.
            lines = path.read_text().split("\n")
            for number, (line, is_code) in enumerate(
                zip(lines, format_markdown_tables.code_block_flags(lines)), start=1
            ):
                # A bare "|" carries no cell, so it ends the table rather than
                # belonging to it -- the same place the formatter stops.
                if is_code or not format_markdown_tables.split_row(line):
                    close(block)
                    block = []
                    continue
                block.append((number, line))
            close(block)

        self.assertEqual(
            ragged,
            [],
            "these tables have rows of differing width, so their closing "
            "pipes do not line up in a fixed-width viewer",
        )

    def test_coverage_explainer_defines_coverage_and_hands_off(self) -> None:
        # The explainer has to define the term for a newcomer, keep the trend
        # history reachable now that the badge no longer links to it, and route
        # a reader who wants more to CONTRIBUTING, where all four measurements
        # (unit, e2e, maintenance-audit, homebrew-release) are documented.
        explainer = (Path(__file__).resolve().parents[1] / "docs/coverage.md").read_text()
        self.assertIn("https://en.wikipedia.org/wiki/Code_coverage", explainer)
        self.assertIn("coverage-trend.csv", explainer)
        self.assertIn("../CONTRIBUTING.md#coverage", explainer)

    def test_show_summary_uses_pager_for_read_only_view(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["tmux", "ripgrep"]
        paged: list[str] = []
        stub = GumStub()
        stub.pager = lambda text: paged.append(text)
        app.gum = stub
        app.show_summary(step=4, total_steps=4, next_hint="This is the full build summary.")

        self.assertEqual(len(paged), 1)
        self.assertIn("Review Build Configuration", paged[0])
        self.assertIn("Press q to close this screen", paged[0])
        self.assertIn("Repository", paged[0])
        self.assertIn("Step 4 of 4.", paged[0])

    def test_show_summary_includes_homebrew_status_for_non_universal_blue_base(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = False
        app.github_user = "example"
        paged: list[str] = []
        stub = GumStub()
        stub.pager = lambda text: paged.append(text)
        app.gum = stub
        app.show_summary()

        self.assertEqual(len(paged), 1)
        self.assertIn("Homebrew", paged[0])
        self.assertIn("Not included", paged[0])

    def test_view_selections_uses_pager_for_read_only_view(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux"]
        app.config.services = ["sshd.service"]
        paged: list[str] = []
        stub = GumStub()
        stub.pager = lambda text: paged.append(text)
        app.gum = stub
        app.view_selections()

        self.assertEqual(len(paged), 1)
        self.assertIn("Current Selections", paged[0])
        self.assertIn("- tmux", paged[0])
        self.assertIn("- sshd.service", paged[0])

    def test_view_selections_includes_homebrew_status_for_non_universal_blue_base(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        paged: list[str] = []
        stub = GumStub()
        stub.pager = lambda text: paged.append(text)
        app.gum = stub
        app.view_selections()

        self.assertEqual(len(paged), 1)
        self.assertIn("Homebrew", paged[0])
        self.assertIn("- Enabled", paged[0])

    def test_patch_container_workflow_injects_cosign_key_into_existing_job_env(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            jobs:
              build_push:
                env:
                  FOO: bar
                steps:
                  - name: Install Cosign
                    if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
                    uses: sigstore/cosign-installer@v3
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn("      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}", patched)
        self.assertIn("      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}", patched)
        self.assertIn("      FOO: bar", patched)

    def test_generate_build_sh_cleans_dnf_metadata_after_package_changes(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux"]
        build_sh = app.generate_build_sh()
        self.assertIn("dnf5 clean all", build_sh)

    def test_generate_build_sh_skips_missing_removed_packages_at_build_time(self) -> None:
        app = self.make_app()
        app.config.removed_packages = ["vim-enhanced", "nano"]
        build_sh = app.generate_build_sh()
        self.assertIn("packages_to_remove=()", build_sh)
        self.assertIn("vim-enhanced \\", build_sh)
        self.assertIn("nano", build_sh)
        self.assertIn('if rpm -q --quiet "$pkg"; then', build_sh)
        self.assertIn('echo "Skipping removal of $pkg because it is not installed in the base image."', build_sh)
        self.assertIn('dnf5 remove -y "${packages_to_remove[@]}"', build_sh)

    def test_generate_build_sh_with_copr_repos(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["kylegospo/bazzite"]
        app.config.packages = ["steam"]
        build_sh = app.generate_build_sh()
        self.assertIn("dnf5 -y copr enable kylegospo/bazzite", build_sh)
        self.assertIn("dnf5 -y copr disable kylegospo/bazzite", build_sh)
        # Enable should come before install, disable should come after
        enable_pos = build_sh.index("copr enable")
        install_pos = build_sh.index("dnf5 install")
        disable_pos = build_sh.index("copr disable")
        self.assertLess(enable_pos, install_pos)
        self.assertLess(install_pos, disable_pos)
        self.assertIn("dnf5 clean all", build_sh)

    def test_generate_build_sh_with_services(self) -> None:
        app = self.make_app()
        app.config.services = ["tailscaled.service", "docker.socket"]
        build_sh = app.generate_build_sh()
        self.assertIn("systemctl enable tailscaled.service", build_sh)
        self.assertIn("systemctl enable docker.socket", build_sh)

    def test_generate_build_sh_empty_config(self) -> None:
        app = self.make_app()
        app.config.packages = []
        app.config.removed_packages = []
        app.config.copr_repos = []
        app.config.services = []
        build_sh = app.generate_build_sh()
        self.assertIn("set -ouex pipefail", build_sh)
        self.assertIn("# dnf5 install -y <your-packages-here>", build_sh)
        self.assertNotIn("dnf5 clean all", build_sh)
        self.assertNotIn("systemctl enable", build_sh)
        self.assertNotIn("copr", build_sh)

    def test_generate_build_sh_packages_only_cleans_dnf(self) -> None:
        app = self.make_app()
        app.config.packages = ["htop", "tmux"]
        build_sh = app.generate_build_sh()
        self.assertIn("dnf5 install -y", build_sh)
        self.assertIn("dnf5 clean all", build_sh)
        self.assertNotIn("copr", build_sh)

    def test_generate_build_sh_guards_system_files_copy(self) -> None:
        # Newer upstream Containerfile snapshots stage a system_files/ overlay
        # into the ctx build stage; older snapshots and from-scratch
        # Containerfiles never COPY it there, so the copy must stay guarded.
        app = self.make_app()
        build_sh = app.generate_build_sh()
        self.assertIn("if [ -d /ctx/system_files ]; then", build_sh)
        self.assertIn("cp -avf /ctx/system_files/. /", build_sh)
        self.assertIn("fi", build_sh)

    # The build.sh shapes below are shared by the three structural tests that
    # follow. Every other generate_build_sh test asserts substrings, which
    # says nothing about whether the file the tool writes into a user's repo
    # is a script bash can actually run.
    BUILD_SH_SHAPES = {
        "empty": {},
        "packages only": {"packages": ["htop"]},
        "several packages": {"packages": ["htop", "tmux", "fastfetch"]},
        "one removal": {"removed_packages": ["nano"]},
        "several removals": {"removed_packages": ["vim-enhanced", "nano"]},
        "copr and packages": {"copr_repos": ["kylegospo/bazzite"], "packages": ["steam"]},
        "services only": {"services": ["tailscaled.service"]},
        "everything": {
            "copr_repos": ["kylegospo/bazzite", "atim/starship"],
            "packages": ["steam", "starship"],
            "removed_packages": ["vim-enhanced", "nano"],
            "services": ["tailscaled.service", "docker.socket"],
        },
        "names needing quoting": {
            "packages": ["a b", "it's"],
            "removed_packages": ["x$(id)"],
            "services": ["a b.service"],
        },
    }

    def build_sh_for_shape(self, selections: dict) -> str:
        app = self.make_app()
        app.config.packages = list(selections.get("packages", []))
        app.config.removed_packages = list(selections.get("removed_packages", []))
        app.config.copr_repos = list(selections.get("copr_repos", []))
        app.config.services = list(selections.get("services", []))
        return app.generate_build_sh()

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_generate_build_sh_output_parses_as_bash(self) -> None:
        # build.sh is written into the generated repo and executed inside the
        # image build, so a syntax error here breaks every build the user runs
        # -- but no test parses the output, only searches it for substrings.
        # Dropping the `removesuffix(" \\")` that terminates the removal list
        # leaves `nano \` running into the `do`, which is a hard syntax error
        # and which the whole existing suite still passes.
        for name, selections in self.BUILD_SH_SHAPES.items():
            with self.subTest(shape=name):
                build_sh = self.build_sh_for_shape(selections)
                parsed = subprocess.run(
                    ["bash", "-n"],
                    input=build_sh,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    parsed.returncode,
                    0,
                    f"generated build.sh is not valid bash: {parsed.stderr.strip()}\n{build_sh}",
                )

    def test_generate_build_sh_never_leaves_a_dangling_line_continuation(self) -> None:
        # The package and removal lists are emitted one item per line with a
        # trailing backslash, and the last item's backslash is stripped after
        # the fact. Emitting it unconditionally instead leaves `htop \` joined
        # to the blank line that follows -- still parseable, so `bash -n`
        # misses it, and still a match for every `assertIn` in this file, but
        # it means the next line the generator emits gets swallowed into the
        # dnf5 command rather than run on its own.
        for name, selections in self.BUILD_SH_SHAPES.items():
            with self.subTest(shape=name):
                build_sh = self.build_sh_for_shape(selections)
                lines = build_sh.split("\n")
                for index, line in enumerate(lines):
                    if not line.endswith("\\"):
                        continue
                    self.assertLess(
                        index + 1,
                        len(lines),
                        f"build.sh ends on a line continuation:\n{build_sh}",
                    )
                    self.assertTrue(
                        lines[index + 1].strip(),
                        f"line {index + 1} continues into a blank line:\n{build_sh}",
                    )

    def test_generate_build_sh_removes_packages_before_installing_and_cleans_after_both(self) -> None:
        # Order is the contract, not just presence: a removal emitted after the
        # install can uninstall something the user asked for (or a dependency
        # the install just pulled in), and `dnf5 clean all` emitted before
        # either one leaves the metadata it was added to drop in the image.
        # test_generate_build_sh_with_copr_repos pins enable/install/disable
        # this way already; nothing pins removals or the clean.
        app = self.make_app()
        app.config.removed_packages = ["vim-enhanced"]
        app.config.packages = ["htop"]
        app.config.services = ["tailscaled.service"]
        build_sh = app.generate_build_sh()
        remove_pos = build_sh.index("dnf5 remove -y")
        install_pos = build_sh.index("dnf5 install -y")
        clean_pos = build_sh.index("dnf5 clean all")
        enable_pos = build_sh.index("systemctl enable")
        self.assertLess(remove_pos, install_pos)
        self.assertLess(install_pos, clean_pos)
        self.assertLess(clean_pos, enable_pos)

    def test_generate_container_workflow_includes_template_tag_variants(self) -> None:
        app = self.make_app()
        workflow = app.generate_container_workflow()
        self.assertIn("type=raw,value={{date 'YYYYMMDD'}}", workflow)
        self.assertIn("type=ref,event=pr", workflow)
        self.assertIn("COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}", workflow)

    def test_installer_profile_maps_kde_and_gnome_base_images_correctly(self) -> None:
        app = self.make_app()
        kde_bases = {"bazzite", "bazzite-dx", "aurora", "aurora-dx", "kinoite"}
        gnome_bases = {"bazzite-gnome", "bazzite-dx-gnome", "bluefin", "bluefin-dx", "silverblue", "sway-atomic", "budgie-atomic", "cosmic-atomic"}
        for bi in BASE_IMAGES:
            app.config.base_image_uri = bi.image_uri
            profile = app.installer_profile()
            if bi.key in kde_bases:
                self.assertEqual(profile, "kde", f"{bi.key} should map to kde")
            elif bi.key in gnome_bases:
                self.assertEqual(profile, "gnome", f"{bi.key} should map to gnome")
            else:
                self.fail(f"Base image {bi.key} is not covered by this test")

    def test_generate_readme_uses_custom_base_title_and_lists_packages(self) -> None:
        app = self.make_app()
        app.config.base_image_name = "Bazzite"
        app.config.packages = ["tmux", "ripgrep"]
        readme = app.generate_readme()
        self.assertIn("# Custom Bazzite Image", readme)
        self.assertIn("| Base Image | `Bazzite` |", readme)
        self.assertIn("- `tmux`", readme)
        self.assertIn("- `ripgrep`", readme)
        self.assertIn("## Requested Packages", readme)
        self.assertIn("requested by this repo's generated build script", readme)
        self.assertIn(app.requested_packages_note(), readme)
        self.assertNotIn("## Installed Packages", readme)
        self.assertIn(f"## Managed By {TOOL_NAME}", readme)
        self.assertIn(f"`{STATE_FILE}`", readme)
        self.assertIn(f"stop using `{TOOL_SLUG}` for this repo", readme)
        self.assertNotIn("## Local Build", readme)
        self.assertNotIn("just build", readme)

    def test_generate_readme_uses_lowercase_published_image_owner(self) -> None:
        app = self.make_app()
        app.config.github_user = "ExampleUser"
        readme = app.generate_readme()
        self.assertIn("| Published Image | `ghcr.io/exampleuser/test-image:latest` |", readme)

    def test_write_project_files_updates_readme_when_config_changes(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)

            app.config.packages = ["tmux"]
            app.write_project_files(repo_dir, include_workflow=False)
            first_readme = (repo_dir / "README.md").read_text()

            app.config.packages = ["ripgrep"]
            app.write_project_files(repo_dir, include_workflow=False)
            second_readme = (repo_dir / "README.md").read_text()

        self.assertIn("- `tmux`", first_readme)
        self.assertNotIn("- `tmux`", second_readme)
        self.assertIn("- `ripgrep`", second_readme)

    def test_write_project_files_load_repo_config_roundtrip(self) -> None:
        """State file written by write_project_files survives load_repo_config."""
        app = self.make_app()
        app.config.packages = ["htop", "tmux"]
        app.config.removed_packages = ["firefox"]
        app.config.copr_repos = ["kylegospo/bazzite"]
        app.config.services = ["tailscaled.service"]
        app.config.image_desc = "Test roundtrip image"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            app2 = self.make_app()
            app2.load_repo_config(repo_dir)
        self.assertEqual(app2.config.packages, ["htop", "tmux"])
        self.assertEqual(app2.config.removed_packages, ["firefox"])
        self.assertEqual(app2.config.copr_repos, ["kylegospo/bazzite"])
        self.assertEqual(app2.config.services, ["tailscaled.service"])
        self.assertEqual(app2.config.image_desc, "Test roundtrip image")
        self.assertEqual(app2.config.base_image_uri, app.config.base_image_uri)

    def test_clone_container_template_excludes_renovate_but_keeps_dependabot(self) -> None:
        """Template copy should exclude renovate.json5 but keep upstream dependabot.yml."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            app.clone_container_template(target)
            github_dir = target / ".github"
            self.assertFalse((github_dir / "renovate.json5").exists(),
                             "renovate.json5 should be excluded from template copies")
            self.assertTrue((github_dir / "dependabot.yml").exists(),
                            "dependabot.yml should be preserved from the upstream template snapshot")

    # ------------------------------------------------------------------
    # Brew OCI integration
    # ------------------------------------------------------------------

    def test_generate_containerfile_includes_brew_block_when_enabled(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        cf = app.generate_containerfile()
        self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", cf)
        self.assertIn("brew-setup.service", cf)
        self.assertIn("brew-update.timer", cf)
        self.assertIn("brew-upgrade.timer", cf)

    def test_generate_containerfile_excludes_brew_block_when_disabled(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = False
        cf = app.generate_containerfile()
        self.assertNotIn("brew", cf.lower())
        self.assertNotIn("system_files", cf)

    def test_render_containerfile_injects_brew_block_into_existing(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        existing = textwrap.dedent("""\
            FROM scratch AS ctx
            COPY build_files /

            FROM ghcr.io/ublue-os/bazzite:stable

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", result)
        self.assertIn("brew-setup.service", result)
        # The original RUN line should still be present.
        self.assertIn("/ctx/build.sh", result)
        # Brew block must appear before the build.sh RUN.
        brew_pos = result.index("brew-setup.service")
        build_pos = result.index("/ctx/build.sh")
        self.assertLess(brew_pos, build_pos)
        # No triple-newline (double blank line).
        self.assertNotIn("\n\n\n", result)

    def test_render_containerfile_removes_brew_block_from_existing(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM scratch AS ctx
            COPY build_files /

            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /
            RUN --mount=type=cache,dst=/var/cache \\
                --mount=type=cache,dst=/var/log \\
                --mount=type=tmpfs,dst=/tmp \\
                /usr/bin/systemctl preset brew-setup.service && \\
                /usr/bin/systemctl preset brew-update.timer && \\
                /usr/bin/systemctl preset brew-upgrade.timer

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("brew", result.lower())
        self.assertNotIn("system_files", result)
        self.assertIn("/ctx/build.sh", result)
        # Should not leave a double blank line after removal.
        self.assertNotIn("\n\n\n", result)

    def test_render_containerfile_removes_brew_block_separated_by_blank_line(self) -> None:
        # A hand-edited Containerfile may put a blank line between the brew COPY
        # and its systemctl preset RUN. Removing only the COPY leaves the RUN
        # presetting units nothing provides any more, and the build fails.
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /

            RUN --mount=type=cache,dst=/var/cache \\
                /usr/bin/systemctl preset brew-setup.service && \\
                /usr/bin/systemctl preset brew-update.timer

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("brew", result.lower())
        self.assertNotIn("system_files", result)
        self.assertIn("/ctx/build.sh", result)
        self.assertNotIn("\n\n\n", result)

    def test_render_containerfile_brew_removal_keeps_unrelated_following_run(self) -> None:
        # With no brew RUN to absorb, the neighbouring RUN must survive.
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /

            RUN /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("system_files", result)
        self.assertIn("RUN /ctx/build.sh", result)

    def test_render_containerfile_brew_removal_is_idempotent(self) -> None:
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /

            RUN --mount=type=cache,dst=/var/cache \\
                /usr/bin/systemctl preset brew-setup.service

            RUN /ctx/build.sh
        """)
        once = app.render_containerfile(existing)
        self.assertEqual(app.render_containerfile(once), once)

    def test_render_containerfile_replaces_blank_separated_brew_block(self) -> None:
        # Re-enabling must collapse the split block into exactly one canonical
        # block, not leave the old RUN alongside the new one.
        app = self.make_app()
        app.config.brew_enabled = True
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /

            RUN --mount=type=cache,dst=/var/cache \\
                /usr/bin/systemctl preset brew-setup.service

            RUN /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertEqual(result.count("/system_files"), 1)
        self.assertEqual(result.count("brew-setup.service"), 1)
        self.assertIn("brew-upgrade.timer", result)
        self.assertIn("RUN /ctx/build.sh", result)

    def test_render_containerfile_replaces_existing_brew_block(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        existing = textwrap.dedent("""\
            FROM scratch AS ctx
            COPY build_files /

            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:old-tag /system_files /
            RUN /usr/bin/systemctl preset brew-setup.service

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        # Old brew reference should be replaced with the current image.
        self.assertNotIn("brew:old-tag", result)
        self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", result)
        self.assertIn("brew-upgrade.timer", result)
        self.assertIn("/ctx/build.sh", result)
        # Should not leave double blank lines after replacement.
        self.assertNotIn("\n\n\n", result)

    def test_render_containerfile_removes_brew_copy_that_ends_the_file(self) -> None:
        # A brew COPY with nothing after it: the scan for the preset RUN walks
        # straight off the end of the file, and so does the trailing-blank
        # check. Both must stop at the boundary rather than index past it.
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("system_files", result)
        self.assertIn("FROM ghcr.io/ublue-os/bazzite:stable", result)

    def test_render_containerfile_removes_brew_copy_followed_by_another_instruction(self) -> None:
        # The next instruction is not a RUN, so there is no preset RUN to
        # absorb. Only the COPY goes; the unrelated instruction stays put.
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /
            LABEL org.opencontainers.image.title="example"

            RUN /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("system_files", result)
        self.assertIn('LABEL org.opencontainers.image.title="example"', result)
        self.assertIn("/ctx/build.sh", result)

    def test_render_containerfile_removes_brew_run_followed_immediately_by_an_instruction(self) -> None:
        # No blank line after the preset RUN, so there is no trailing blank to
        # consume. The instruction on the very next line must survive.
        app = self.make_app()
        app.config.brew_enabled = False
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable

            COPY --from=ghcr.io/ublue-os/brew:latest /system_files /
            RUN /usr/bin/systemctl preset brew-setup.service
            RUN /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertNotIn("brew", result.lower())
        self.assertIn("RUN /ctx/build.sh", result)

    def test_render_containerfile_injects_brew_block_after_a_final_from_line(self) -> None:
        # Injecting after a FROM that ends the file: the skip-a-blank-line step
        # has no line to look at and must not read past the end.
        app = self.make_app()
        app.config.brew_enabled = True
        existing = "FROM ghcr.io/ublue-os/bazzite:stable\n"
        result = app.render_containerfile(existing)
        self.assertIn("FROM ghcr.io/ublue-os/bazzite:stable", result)
        self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", result)
        self.assertLess(result.index("FROM ghcr.io"), result.index("system_files"))

    def test_render_containerfile_injects_brew_block_before_an_adjacent_instruction(self) -> None:
        # FROM followed immediately by an instruction, no blank line to skip.
        # The block goes between them without swallowing the instruction.
        app = self.make_app()
        app.config.brew_enabled = True
        existing = textwrap.dedent("""\
            FROM ghcr.io/ublue-os/bazzite:stable
            RUN /ctx/build.sh
        """)
        result = app.render_containerfile(existing)
        self.assertIn("RUN /ctx/build.sh", result)
        self.assertLess(result.index("system_files"), result.index("/ctx/build.sh"))

    # ── state file must not publish the host inventory ──────────────────

    def scanned_app(self) -> App:
        app = self.make_app()
        app.config.packages = ["tmux"]
        app.config.scanned_packages = ["tmux", "steam", "private-tool", "vpn-client"]
        app.config.scanned_removed = ["firefox"]
        return app

    def test_state_payload_omits_scanned_inventory(self) -> None:
        # The state file is committed and pushed to a repo created --public, so
        # the full layered-package list would become world-readable, including
        # packages the user deselected and never intended to carry over.
        payload = self.scanned_app().state_payload()
        self.assertNotIn("scanned_packages", payload)
        self.assertNotIn("scanned_removed", payload)
        serialized = json.dumps(payload)
        for leaked in ("steam", "private-tool", "vpn-client", "firefox"):
            self.assertNotIn(leaked, serialized)
        # The package the user actually chose to carry over still belongs here.
        self.assertEqual(payload["packages"], ["tmux"])

    def test_state_payload_records_carried_flag_instead(self) -> None:
        self.assertTrue(self.scanned_app().state_payload()["scan_customizations_carried"])

    def test_state_payload_carried_flag_false_without_scan(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux"]
        self.assertFalse(app.state_payload()["scan_customizations_carried"])

    def test_carried_scan_customizations_survives_state_roundtrip(self) -> None:
        # The README and post-build summary wording depends on this, and it must
        # keep working on a later update when only the state file is available.
        payload = self.scanned_app().state_payload()
        reloaded = App()
        reloaded.config = config_from_state_payload(payload)
        self.assertTrue(reloaded.carried_scan_customizations())

    def test_state_payload_strips_inventory_written_by_older_versions(self) -> None:
        # A repo written before this change still carries the lists; reading it
        # must work, and rewriting must clean them out.
        legacy = self.scanned_app().state_payload()
        legacy["scanned_packages"] = ["tmux", "steam", "private-tool"]
        legacy["scanned_removed"] = ["firefox"]
        del legacy["scan_customizations_carried"]
        app = App()
        app.config = config_from_state_payload(legacy)
        self.assertTrue(app.carried_scan_customizations())
        rewritten = app.state_payload()
        self.assertNotIn("scanned_packages", rewritten)
        self.assertNotIn("scanned_removed", rewritten)
        self.assertNotIn("steam", json.dumps(rewritten))

    def test_generate_readme_says_the_package_is_private_before_the_switch(self) -> None:
        # The switch command was presented as ready to run the moment the build
        # went green. It is not: the package is private by default, and a
        # public repository does not change that. This has to come before the
        # command, not as a troubleshooting note after it.
        app = self.make_app()
        readme = app.generate_readme()
        access_at = readme.index("## Before The First Switch")
        switch_at = readme.index("sudo bootc switch")
        self.assertLess(access_at, switch_at)
        self.assertIn("**private** package", readme)
        self.assertIn("https://github.com/example/test-image/pkgs/container/test-image", readme)
        self.assertIn(atomic_image_builder.BOOTC_REGISTRY_DOCS_URL, readme)

    def test_generate_readme_keeps_the_access_step_on_the_scanned_path(self) -> None:
        # The scanned README replaces the whole "Using The Image" block, which
        # is how the access step could have been dropped from exactly the path
        # that has the most to go wrong.
        readme = self.scanned_app().generate_readme()
        self.assertIn("## Before The First Switch", readme)
        self.assertIn("sudo rpm-ostree reset", readme)

    def test_generate_readme_notes_carried_scan_customizations(self) -> None:
        readme = self.scanned_app().generate_readme()
        self.assertIn("This repo carries over package changes scanned from your current system.", readme)
        self.assertIn("sudo rpm-ostree reset", readme)
        self.assertIn("Do not reboot between `rpm-ostree reset` and `bootc switch`.", readme)

    def test_generate_readme_says_what_the_recommended_reset_removes(self) -> None:
        # With no category flags, `rpm-ostree reset` clears overlays,
        # overrides and initramfs customization alike. Presented as the last
        # step of carrying customizations over, it read as undoing exactly
        # what had been carried -- and anything else on the host went with it.
        readme = self.scanned_app().generate_readme()
        self.assertIn("not only the ones this image reproduces", readme)
        self.assertIn("override and initramfs", readme)
        self.assertIn("rpm-ostree status", readme)

    def test_generate_readme_omits_the_reset_block_without_carried_customizations(self) -> None:
        # The qualification belongs to the reset instruction, so it must not
        # appear on a build that never recommends one.
        readme = self.make_app().generate_readme()
        self.assertNotIn("rpm-ostree reset", readme)
        self.assertNotIn("not only the ones this image reproduces", readme)

    def test_config_from_state_payload_roundtrips_brew_enabled(self) -> None:
        cfg = config_from_state_payload({"brew_enabled": True})
        self.assertTrue(cfg.brew_enabled)
        cfg2 = config_from_state_payload({"brew_enabled": False})
        self.assertFalse(cfg2.brew_enabled)

    def test_config_from_state_payload_rejects_non_bool_brew_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "brew_enabled must be a boolean"):
            config_from_state_payload({"brew_enabled": "yes"})

    def test_is_universal_blue_base_true_for_universal_blue_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable"
        self.assertTrue(app.is_universal_blue_base())

    def test_is_universal_blue_base_false_for_fedora_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        self.assertFalse(app.is_universal_blue_base())

    def test_offer_brew_if_applicable_confirm_defaults_to_current_brew_state(self) -> None:
        # GumStub.confirm() returns whatever default it is passed, so this
        # simulates a user pressing Enter to accept the default. Before the
        # fix, the default was hardcoded to False, so accepting it on a repo
        # that already had brew enabled would silently disable it.
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.gum = GumStub()

        app.config.brew_enabled = True
        app.offer_brew_if_applicable()
        self.assertTrue(app.config.brew_enabled)

        app.config.brew_enabled = False
        app.offer_brew_if_applicable()
        self.assertFalse(app.config.brew_enabled)

    def test_offer_brew_if_applicable_skips_prompt_for_universal_blue_base(self) -> None:
        # Universal Blue images already bundle Homebrew, so there is nothing
        # to offer and no prompt should be shown.
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable"
        app.config.brew_enabled = True
        stub = GumStub()

        def fail_confirm(*_args, **_kwargs):
            raise AssertionError("confirm() should not be called for a Universal Blue base")

        stub.confirm = fail_confirm
        app.gum = stub
        app.offer_brew_if_applicable()
        self.assertFalse(app.config.brew_enabled)
        self.assertEqual(app.gum.messages, [])

    def test_software_status_includes_brew_when_enabled(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.brew_enabled = True
        status = app.software_status()
        self.assertIn("brew", status)

    def test_software_status_excludes_brew_when_disabled(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.brew_enabled = False
        status = app.software_status()
        self.assertNotIn("brew", status)

    def test_update_task_choices_includes_homebrew_for_fedora_base(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.base_image_name = "Silverblue"
        app.config.brew_enabled = True
        choices = app.update_task_choices()
        homebrew_choices = [(t, s) for t, s in choices if t == "Homebrew"]
        self.assertEqual(len(homebrew_choices), 1)
        self.assertEqual(homebrew_choices[0][1], "Enabled")

    def test_update_task_choices_excludes_homebrew_for_universal_blue_base(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        # Default make_app uses a UBlue base
        choices = app.update_task_choices()
        homebrew_choices = [t for t, _s in choices if t == "Homebrew"]
        self.assertFalse(homebrew_choices)

    def test_state_payload_includes_brew_enabled(self) -> None:
        app = self.make_app()
        app.config.brew_enabled = True
        payload = app.state_payload()
        self.assertTrue(payload["brew_enabled"])

    def test_write_project_files_roundtrips_brew_enabled(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.base_image_name = "Silverblue"
        app.config.brew_enabled = True
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            state = json.loads((repo_dir / STATE_FILE).read_text())
            self.assertTrue(state["brew_enabled"])
            cf = (repo_dir / "Containerfile").read_text()
            self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", cf)
            self.assertIn("brew-setup.service", cf)


    # ------------------------------------------------------------------
    # BlueBuild build method
    # ------------------------------------------------------------------

    def make_bluebuild_app(self) -> App:
        app = App()
        app.config = Config(
            method="bluebuild",
            base_image_uri="ghcr.io/ublue-os/bazzite:stable",
            base_image_name="Bazzite (KDE)",
            repo_name="test-bb-image",
            image_desc="Test BlueBuild image",
            github_user="example",
        )
        return app

    def test_split_image_ref_separates_tag(self) -> None:
        app = self.make_app()
        self.assertEqual(
            app._split_image_ref("ghcr.io/ublue-os/bazzite:stable"),
            ("ghcr.io/ublue-os/bazzite", "stable"),
        )

    def test_split_image_ref_defaults_to_latest(self) -> None:
        app = self.make_app()
        self.assertEqual(
            app._split_image_ref("ghcr.io/ublue-os/bazzite"),
            ("ghcr.io/ublue-os/bazzite", "latest"),
        )

    def test_split_image_ref_rejects_digest_pinned_ref(self) -> None:
        # A digest ref has no tag, and its colon belongs to the digest. BlueBuild
        # rejoins base-image and image-version with a colon, so there is no pair
        # that can represent a digest: "...@sha256:aaa...:latest" is unparseable
        # and fails every build. Refusing beats emitting a broken recipe.
        app = self.make_app()
        digest = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        with self.assertRaisesRegex(CommandError, "digest-pinned"):
            app._split_image_ref(digest)

    def test_split_image_ref_still_splits_tagged_refs(self) -> None:
        app = self.make_app()
        self.assertEqual(
            app._split_image_ref("ghcr.io/ublue-os/bazzite:stable"),
            ("ghcr.io/ublue-os/bazzite", "stable"),
        )
        self.assertEqual(
            app._split_image_ref("ghcr.io/ublue-os/bazzite"),
            ("ghcr.io/ublue-os/bazzite", "latest"),
        )

    def test_validate_config_rejects_digest_base_image_for_bluebuild(self) -> None:
        # Reachable from a real scan: a host booted on a digest-pinned
        # deployment, the curated-tag offer declined, then BlueBuild chosen.
        app = self.make_bluebuild_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        with self.assertRaisesRegex(CommandError, "digest-pinned base image"):
            app.validate_config()

    def test_validate_config_allows_digest_base_image_for_containerfile(self) -> None:
        # The Containerfile path writes the reference into FROM verbatim, so a
        # digest pin is legitimate there and must keep working.
        app = self.make_app()
        app.config.method = "containerfile"
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        app.validate_config()
        containerfile = app.render_containerfile()
        self.assertIn("@sha256:" + "a" * 64, containerfile)

    def test_generate_recipe_basic(self) -> None:
        app = self.make_bluebuild_app()
        recipe = app.generate_recipe()
        self.assertIn(f"$schema={BLUEBUILD_RECIPE_SCHEMA}", recipe)
        self.assertEqual(parse_block_yaml(recipe)["name"], "test-bb-image")
        self.assertIn("base-image: ghcr.io/ublue-os/bazzite", recipe)
        self.assertIn("image-version:", recipe)
        self.assertIn("stable", recipe)
        self.assertIn("- type: files", recipe)
        self.assertIn("- type: signing", recipe)

    def test_generate_recipe_includes_packages(self) -> None:
        app = self.make_bluebuild_app()
        app.config.packages = ["htop", "tmux"]
        recipe = app.generate_recipe()
        self.assertIn("- type: dnf", recipe)
        self.assertIn('        - "htop"', recipe)
        self.assertIn('        - "tmux"', recipe)

    def test_generate_recipe_name_stays_a_string_for_yaml_literals(self) -> None:
        # "null", "false" and "123" are valid container name components and
        # valid GitHub repository names, so the wizard accepts all three -- but
        # they are YAML literals, and emitted bare they reached BlueBuild as
        # null, a boolean and an integer against a schema that requires a
        # string. The recipe is generated by joining strings, so nothing else
        # in the pipeline repairs the type.
        for name in ("null", "false", "123", "true", "~", "0755"):
            with self.subTest(repo_name=name):
                app = self.make_bluebuild_app()
                app.config.repo_name = name
                parsed = parse_block_yaml(app.generate_recipe())["name"]
                self.assertIsInstance(parsed, str)
                self.assertEqual(parsed, name)

    def test_write_bluebuild_project_files_keeps_a_literal_name_a_string(self) -> None:
        # Through the project writer as well as the generator: the file that
        # actually reaches the repo is the one BlueBuild reads.
        app = self.make_bluebuild_app()
        app.config.repo_name = "null"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            recipe = (repo_dir / "recipes/recipe.yml").read_text()
        self.assertEqual(parse_block_yaml(recipe)["name"], "null")

    def test_generate_recipe_quotes_package_with_trailing_colon(self) -> None:
        # PACKAGE_TOKEN_RE permits ":", so "epel:" passes validation. Emitted
        # bare it would become a mapping node instead of the string BlueBuild
        # expects, silently corrupting the recipe.
        app = self.make_bluebuild_app()
        app.config.packages = ["epel:"]
        recipe = app.generate_recipe()
        self.assertIn('        - "epel:"', recipe)
        self.assertNotIn("        - epel:", recipe)

    def test_generate_recipe_includes_copr_repos(self) -> None:
        app = self.make_bluebuild_app()
        app.config.copr_repos = ["kylegospo/bazzite"]
        recipe = app.generate_recipe()
        self.assertIn("copr:", recipe)
        self.assertIn('        - "kylegospo/bazzite"', recipe)

    def test_generate_recipe_includes_removed_packages(self) -> None:
        app = self.make_bluebuild_app()
        app.config.removed_packages = ["firefox"]
        recipe = app.generate_recipe()
        self.assertIn("remove:", recipe)
        self.assertIn('        - "firefox"', recipe)

    def test_generate_recipe_quotes_removed_package_with_trailing_colon(self) -> None:
        app = self.make_bluebuild_app()
        app.config.removed_packages = ["epel:"]
        recipe = app.generate_recipe()
        self.assertIn('        - "epel:"', recipe)
        self.assertNotIn("        - epel:", recipe)

    def test_generate_recipe_includes_services(self) -> None:
        app = self.make_bluebuild_app()
        app.config.services = ["tailscaled.service", "@my-instance.service"]
        recipe = app.generate_recipe()
        self.assertIn("- type: systemd", recipe)
        self.assertIn('        - "tailscaled.service"', recipe)
        self.assertIn('        - "@my-instance.service"', recipe)
        self.assertNotIn("        - @my-instance.service", recipe)

    def test_generate_recipe_includes_brew_oci_layer(self) -> None:
        app = self.make_bluebuild_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        recipe = app.generate_recipe()
        self.assertIn("- type: containerfile", recipe)
        self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", recipe)
        self.assertIn("brew-setup.service", recipe)
        self.assertIn("brew-update.timer", recipe)
        self.assertIn("brew-upgrade.timer", recipe)

    def test_generate_recipe_excludes_brew_when_disabled(self) -> None:
        app = self.make_bluebuild_app()
        app.config.brew_enabled = False
        recipe = app.generate_recipe()
        self.assertNotIn("containerfile", recipe.split("type: signing")[0])
        self.assertNotIn(UNIVERSAL_BLUE_BREW_IMAGE, recipe)

    def test_generate_recipe_omits_dnf_module_when_no_packages(self) -> None:
        app = self.make_bluebuild_app()
        recipe = app.generate_recipe()
        self.assertNotIn("- type: dnf", recipe)

    def test_generate_recipe_omits_systemd_module_when_no_services(self) -> None:
        app = self.make_bluebuild_app()
        recipe = app.generate_recipe()
        self.assertNotIn("- type: systemd", recipe)

    def test_generate_recipe_full_config(self) -> None:
        app = self.make_bluebuild_app()
        app.config.packages = ["htop"]
        app.config.copr_repos = ["kylegospo/bazzite"]
        app.config.services = ["tailscaled.service"]
        app.config.removed_packages = ["firefox"]
        app.config.brew_enabled = True
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        recipe = app.generate_recipe()
        self.assertIn("- type: files", recipe)
        self.assertIn("- type: containerfile", recipe)
        self.assertIn("- type: dnf", recipe)
        self.assertIn("- type: systemd", recipe)
        self.assertIn("- type: signing", recipe)

    # The tests above assert substring membership, which cannot see the shape
    # of the document: which key a list hangs under, whether a key was emitted
    # twice, whether the indentation still parses at all. BlueBuild reads this
    # file as YAML, so the tests below read it that way too.

    def recipe_document(self, app: App) -> dict:
        recipe = app.generate_recipe()
        document = parse_block_yaml(recipe)
        self.assertIsInstance(document, dict, recipe)
        return document

    def recipe_module(self, document: dict, module_type: str) -> dict:
        modules = document["modules"]
        matching = [module for module in modules if module.get("type") == module_type]
        self.assertEqual(len(matching), 1, modules)
        return matching[0]

    def test_generate_recipe_parses_as_yaml_with_the_documented_top_level_keys(self) -> None:
        app = self.make_bluebuild_app()
        document = self.recipe_document(app)
        self.assertEqual(
            set(document),
            {"name", "description", "base-image", "image-version", "modules"},
        )
        self.assertEqual(document["name"], "test-bb-image")
        self.assertEqual(document["description"], "Test BlueBuild image")
        self.assertEqual(document["base-image"], "ghcr.io/ublue-os/bazzite")
        self.assertEqual(document["image-version"], "stable")
        self.assertEqual([module["type"] for module in document["modules"]], ["files", "signing"])

    def test_generate_recipe_nests_packages_under_install_and_removals_under_remove(self) -> None:
        # Substring assertions cannot tell "install:" from "remove:": emitting
        # the install list under remove keeps every assertIn passing while the
        # generated image uninstalls what the user asked to add, and with both
        # lists set it duplicates the key so one of them is silently dropped.
        app = self.make_bluebuild_app()
        app.config.packages = ["htop", "tmux"]
        app.config.removed_packages = ["firefox"]
        app.config.copr_repos = ["kylegospo/bazzite"]
        dnf = self.recipe_module(self.recipe_document(app), "dnf")
        self.assertEqual(dnf["install"], {"packages": ["htop", "tmux"]})
        self.assertEqual(dnf["remove"], {"packages": ["firefox"]})
        self.assertEqual(dnf["repos"], {"copr": ["kylegospo/bazzite"]})

    def test_generate_recipe_enables_services_rather_than_masking_them(self) -> None:
        # "- type: systemd" plus the quoted unit name matches whichever key
        # the units hang under, including "masked:", which is the opposite of
        # what the user asked for.
        app = self.make_bluebuild_app()
        app.config.services = ["tailscaled.service", "@my-instance.service"]
        systemd = self.recipe_module(self.recipe_document(app), "systemd")
        self.assertEqual(
            systemd["system"],
            {"enabled": ["tailscaled.service", "@my-instance.service"]},
        )

    def test_generate_recipe_keeps_a_package_with_a_trailing_colon_a_string(self) -> None:
        # The quoting exists so "epel:" stays a scalar instead of becoming a
        # mapping node. Asserting the quotes is asserting the fix; this asserts
        # the property the fix is for.
        app = self.make_bluebuild_app()
        app.config.packages = ["epel:"]
        app.config.removed_packages = ["epel:"]
        dnf = self.recipe_module(self.recipe_document(app), "dnf")
        self.assertEqual(dnf["install"]["packages"], ["epel:"])
        self.assertEqual(dnf["remove"]["packages"], ["epel:"])

    def test_generate_recipe_brew_snippets_stay_one_scalar_and_one_literal_block(self) -> None:
        app = self.make_bluebuild_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.brew_enabled = True
        containerfile = self.recipe_module(self.recipe_document(app), "containerfile")
        copy_snippet, run_snippet = containerfile["snippets"]
        self.assertEqual(copy_snippet, f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /")
        self.assertTrue(run_snippet.startswith("RUN --mount=type=cache,dst=/var/cache \\"), run_snippet)
        # The literal block is one shell command: every line but the last has
        # to keep its continuation, or the image builds without the presets.
        run_lines = run_snippet.splitlines()
        self.assertTrue(all(line.endswith("\\") for line in run_lines[:-1]), run_snippet)
        self.assertFalse(run_lines[-1].endswith("\\"), run_snippet)
        self.assertIn("preset brew-upgrade.timer", run_lines[-1])

    def test_generate_recipe_full_config_parses_into_the_expected_module_sequence(self) -> None:
        app = self.make_bluebuild_app()
        app.config.packages = ["htop"]
        app.config.copr_repos = ["kylegospo/bazzite"]
        app.config.services = ["tailscaled.service"]
        app.config.removed_packages = ["firefox"]
        app.config.brew_enabled = True
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        document = self.recipe_document(app)
        self.assertEqual(
            [module["type"] for module in document["modules"]],
            ["files", "containerfile", "dnf", "systemd", "signing"],
        )
        self.assertEqual(document["modules"][0]["files"], [{"source": "system", "destination": "/"}])
        self.assertEqual(document["modules"][-1], {"type": "signing"})

    def test_patch_bluebuild_workflow_pins_action(self) -> None:
        app = self.make_bluebuild_app()
        template = "        uses: blue-build/github-action@v1.11\n"
        patched = app.patch_bluebuild_workflow(template)
        self.assertIn(ACTION_PINS["blue-build/github-action"][0], patched)
        self.assertNotIn("@v1.11\n", patched)

    def test_patch_bluebuild_workflow_updates_cron(self) -> None:
        app = self.make_bluebuild_app()
        template = '    - cron: "00 06 * * *"\n'
        patched = app.patch_bluebuild_workflow(template)
        self.assertIn(DEFAULT_GITHUB_BUILD_CRON, patched)
        self.assertNotIn("00 06", patched)

    def test_patch_bluebuild_workflow_adds_state_file_ignore(self) -> None:
        app = self.make_bluebuild_app()
        template = '    paths-ignore:\n      - "**.md"\n'
        patched = app.patch_bluebuild_workflow(template)
        self.assertIn(STATE_FILE, patched)

    def test_patch_bluebuild_workflow_anchors_state_ignore_to_md_entry(self) -> None:
        # Fallback for a paths-ignore key written in a form the key match does
        # not see. Without it the state file is never ignored and every
        # state-only commit triggers a rebuild.
        app = self.make_bluebuild_app()
        template = '    "paths-ignore":\n      - "**.md"\n'
        patched = app.patch_bluebuild_workflow(template)
        self.assertIn(f"      - '{STATE_FILE}'", patched)
        self.assertIn('      - "**.md"', patched)
        self.assertEqual(app.patch_bluebuild_workflow(patched), patched)

    def test_patch_bluebuild_workflow_adds_state_ignore_only_once(self) -> None:
        app = self.make_bluebuild_app()
        template = '    paths-ignore:\n      - "**.md"\n'
        patched = app.patch_bluebuild_workflow(template)
        self.assertEqual(patched.count(STATE_FILE), 1)

    def test_patch_bluebuild_workflow_adds_branch_filters_and_validation_only_inputs(self) -> None:
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            name: bluebuild
            on:
              push:
                paths-ignore:
                  - "**.md"
              pull_request:
              workflow_dispatch:
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.11
                    with:
                      recipe: ${{ matrix.recipe }}
                      cosign_private_key: ${{ secrets.SIGNING_SECRET }}
                      registry_token: ${{ github.token }}
                      pr_event_number: ${{ github.event.number }}
            """
        )
        patched = app.patch_bluebuild_workflow(template, default_branch="master")
        self.assertIn("  push:\n    branches:\n      - master\n    paths-ignore:", patched)
        self.assertIn("  pull_request:\n    branches:\n      - master\n  workflow_dispatch:", patched)
        self.assertIn(
            "          push: ${{ github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && 'true' || 'false' }}",
            patched,
        )
        self.assertIn("          build_opts: ${{ github.event_name == 'pull_request' && '--no-sign' || '' }}", patched)
        self.assertIn("          chunkah: 'true'", patched)

    def test_patch_bluebuild_action_inputs_is_idempotent_for_chunkah(self) -> None:
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      recipe: ${{ matrix.recipe }}
            """
        )
        once = app.patch_bluebuild_action_inputs(template)
        twice = app.patch_bluebuild_action_inputs(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("chunkah:"), 1)

    def test_patch_bluebuild_action_inputs_removes_conflicting_rechunk_inputs(self) -> None:
        # rechunk/build_chunked_oci conflict with chunkah in the action's own
        # input validation, so a hand-edited or previously-generated workflow
        # that already sets one must have it removed when chunkah is added.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      recipe: ${{ matrix.recipe }}
                      rechunk: true
                      build_chunked_oci: true
            """
        )
        result = app.patch_bluebuild_action_inputs(template)
        self.assertNotIn("rechunk: true", result)
        self.assertNotIn("build_chunked_oci: true", result)
        self.assertIn("chunkah: 'true'", result)
        self.assertEqual(result.count("chunkah:"), 1)

    def test_patch_bluebuild_action_inputs_replaces_existing_push_value(self) -> None:
        # A `push:` with a different value (hand-edited, or written by an older
        # version of this tool) must be replaced, not joined by a second copy.
        # Duplicate keys in one mapping make GitHub Actions reject the workflow
        # outright, so no build runs at all.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      push: 'true'
                      recipe: ${{ matrix.recipe }}
            """
        )
        result = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(result.count("push:"), 1)
        self.assertNotIn("push: 'true'", result)
        self.assertIn("github.event.repository.default_branch", result)
        # An input we do not own must survive untouched.
        self.assertIn("recipe: ${{ matrix.recipe }}", result)
        self.assertEqual(app.patch_bluebuild_action_inputs(result), result)

    def test_patch_bluebuild_action_inputs_replaces_existing_build_opts_and_chunkah(self) -> None:
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      build_opts: '--verbose'
                      chunkah: 'false'
                      recipe: ${{ matrix.recipe }}
            """
        )
        result = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(result.count("build_opts:"), 1)
        self.assertEqual(result.count("chunkah:"), 1)
        self.assertNotIn("--verbose", result)
        self.assertNotIn("chunkah: 'false'", result)
        self.assertIn("chunkah: 'true'", result)
        self.assertEqual(app.patch_bluebuild_action_inputs(result), result)

    def test_patch_bluebuild_action_inputs_removes_block_scalar_value_lines(self) -> None:
        # Removing an entry must take its continuation lines with it, or the
        # orphaned value line lands in the mapping as invalid YAML.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      push: >-
                        true
                      recipe: ${{ matrix.recipe }}
            """
        )
        result = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(result.count("push:"), 1)
        self.assertNotIn(">-", result)
        self.assertNotIn("\n                        true", result)
        self.assertIn("recipe: ${{ matrix.recipe }}", result)

    def test_patch_bluebuild_action_inputs_keeps_dropping_across_blank_scalar_lines(self) -> None:
        # Block scalars (">-", "|") legitimately contain blank lines. Treating
        # a blank line as the end of the dropped entry kept the scalar's
        # remaining lines - orphaned under with: once their key was gone, which
        # GitHub Actions rejects as invalid YAML.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      push: >-
                        ${{ github.event_name != 'pull_request'

                        && github.ref == 'refs/heads/main' }}
                      recipe: ${{ matrix.recipe }}
            """
        )
        result = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(result.count("push:"), 1)
        self.assertNotIn(">-", result)
        self.assertNotIn("refs/heads/main' }}", result)
        self.assertIn("recipe: ${{ matrix.recipe }}", result)
        self.assertEqual(app.patch_bluebuild_action_inputs(result), result)

    def action_input_keys(self, workflow_text: str) -> list[str]:
        """Keys directly under the blue-build action step's `with:` mapping.

        Counting bare substrings over the whole file would also pick up the
        top-level `on: push:` trigger, which is unrelated.
        """
        keys: list[str] = []
        entry_indent: int | None = None
        for line in workflow_text.splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped == "with:":
                entry_indent = indent + 2
                continue
            if entry_indent is None:
                continue
            if stripped and indent < entry_indent:
                entry_indent = None
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*):", stripped)
            if match and indent == entry_indent:
                keys.append(match.group(1))
        return keys

    def test_patch_bluebuild_action_inputs_bundled_snapshot_has_no_duplicate_keys(self) -> None:
        snapshot = (
            BLUEBUILD_TEMPLATE_DIR / ".github" / "workflows" / "build.yml"
        ).read_text()
        app = self.make_bluebuild_app()
        result = app.patch_bluebuild_action_inputs(snapshot)
        keys = self.action_input_keys(result)
        self.assertEqual(len(keys), len(set(keys)), f"duplicate action inputs: {keys}")
        for key in ("push", "build_opts", "chunkah"):
            self.assertIn(key, keys)
        self.assertEqual(app.patch_bluebuild_action_inputs(result), result)

    def test_patch_bluebuild_action_inputs_leaves_unrelated_steps_untouched(self) -> None:
        # Only the blue-build action step owns these inputs; a checkout step
        # that happens to carry a with: block must not gain them.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
                    with:
                      fetch-depth: 0
            """
        )
        patched = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(patched, ensure_trailing_newline(template))
        self.assertNotIn("chunkah", patched)

    def test_patch_bluebuild_action_inputs_skips_action_step_without_with_block(self) -> None:
        # There is no inputs mapping to extend, so the step is returned as-is
        # rather than guessing an indentation. The bundled-snapshot test above
        # is what proves the real shape still gets patched.
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build
                    uses: blue-build/github-action@836161eb076426a451e6a0054f722b1153b8b3ad # v1.12
            """
        )
        patched = app.patch_bluebuild_action_inputs(template)
        self.assertEqual(patched, ensure_trailing_newline(template))
        self.assertNotIn("chunkah", patched)

    def test_patch_bluebuild_action_inputs_no_duplicates_when_keys_preexist(self) -> None:
        app = self.make_bluebuild_app()
        template = textwrap.dedent(
            """\
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.12
                    with:
                      push: 'true'
                      build_opts: '--verbose'
                      chunkah: 'false'
                      recipe: ${{ matrix.recipe }}
            """
        )
        keys = self.action_input_keys(app.patch_bluebuild_action_inputs(template))
        self.assertEqual(sorted(keys), ["build_opts", "chunkah", "push", "recipe"])

    def test_patch_bluebuild_action_inputs_patch_step_returns_empty_list_unchanged(self) -> None:
        # patch_workflow_steps's flush_step never calls patch_step with an
        # empty block in practice, but patch_step still guards for it; capture
        # the closure via the shared walker to exercise that guard directly.
        app = self.make_bluebuild_app()
        captured: dict[str, object] = {}

        def fake_patch_workflow_steps(workflow_text, patch_step):
            captured["patch_step"] = patch_step
            return []

        with patch("atomic_image_builder.patch_workflow_steps", side_effect=fake_patch_workflow_steps):
            app.patch_bluebuild_action_inputs("irrelevant")

        self.assertEqual(captured["patch_step"]([]), [])

    def test_patch_workflow_steps_splits_steps_and_passes_others_through(self) -> None:
        # The shared step walker both workflow patchers now use.
        seen: list[list[str]] = []

        def record(step_lines: list[str]) -> list[str]:
            seen.append(list(step_lines))
            return step_lines

        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              push:
            jobs:
              build:
                steps:
                  - name: One
                    run: echo one
                  - name: Two
                    run: echo two
                if: always()
            """
        )
        output = patch_workflow_steps(workflow, record)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0].strip(), "- name: One")
        self.assertEqual(seen[1][0].strip(), "- name: Two")
        self.assertIn("    run: echo two", seen[1][1])
        # Nothing is lost or reordered when the patcher is a no-op.
        self.assertEqual("\n".join(output), workflow.rstrip("\n"))

    def test_patch_workflow_steps_ignores_text_outside_steps(self) -> None:
        seen: list[list[str]] = []
        workflow = textwrap.dedent(
            """\
            name: Workflow
            on:
              push:
            jobs:
              build:
                runs-on: ubuntu-latest
            """
        )
        output = patch_workflow_steps(workflow, lambda step: (seen.append(step), step)[1])
        self.assertEqual(seen, [])
        self.assertEqual("\n".join(output), workflow.rstrip("\n"))

    def test_clone_bluebuild_template_copies_snapshot(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            app.clone_bluebuild_template(target)
            self.assertTrue((target / ".github/workflows/build.yml").exists())
            self.assertTrue((target / ".github/dependabot.yml").exists())
            self.assertTrue((target / "recipes/recipe.yml").exists())
            self.assertTrue((target / "files/system/etc/.gitkeep").exists())

    def test_seed_project_template_uses_bluebuild_for_bluebuild_method(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            app.seed_project_template(target)
            self.assertTrue((target / "recipes/recipe.yml").exists())
            # Should NOT have Containerfile-specific files
            self.assertFalse((target / "Containerfile").exists())

    def test_seed_project_template_uses_containerfile_for_containerfile_method(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            app.seed_project_template(target)
            self.assertTrue((target / "Containerfile").exists())

    def test_write_bluebuild_project_files_creates_recipe(self) -> None:
        app = self.make_bluebuild_app()
        app.config.packages = ["htop"]
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            recipe_path = repo_dir / "recipes/recipe.yml"
            self.assertTrue(recipe_path.exists())
            recipe = recipe_path.read_text()
            self.assertEqual(parse_block_yaml(recipe)["name"], "test-bb-image")
            self.assertIn("- type: dnf", recipe)
            self.assertIn('        - "htop"', recipe)

    def test_write_bluebuild_project_files_writes_readme(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            readme = (repo_dir / "README.md").read_text()
            self.assertIn("BlueBuild", readme)

    def test_write_bluebuild_project_files_writes_state_file(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            state = json.loads((repo_dir / STATE_FILE).read_text())
            self.assertEqual(state["method"], "bluebuild")

    def test_write_bluebuild_project_files_does_not_create_containerfile(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            self.assertFalse((repo_dir / "Containerfile").exists())
            self.assertFalse((repo_dir / "build_files/build.sh").exists())

    def test_write_bluebuild_project_files_patches_workflow(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            # Seed template first so workflow file exists
            app.clone_bluebuild_template(repo_dir)
            app.write_project_files(repo_dir, include_workflow=True, default_branch="master")
            workflow = (repo_dir / ".github/workflows/build.yml").read_text()
            # Should be pinned
            self.assertIn(ACTION_PINS["blue-build/github-action"][0], workflow)
            self.assertIn(DEFAULT_GITHUB_BUILD_CRON, workflow)
            self.assertIn("  push:\n    branches:\n      - master", workflow)
            self.assertIn("  pull_request:\n    branches:\n      - master", workflow)
            self.assertIn("          build_opts: ${{ github.event_name == 'pull_request' && '--no-sign' || '' }}", workflow)

    def test_write_bluebuild_project_files_survives_a_missing_workflow_snapshot(self) -> None:
        # Same best-effort restore as the Containerfile method: no bundled
        # snapshot means no workflow to write, and nothing to patch afterwards.
        # Neither step may raise on the way out.
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo_dir = Path(tmp)
            with patch("atomic_image_builder.BLUEBUILD_TEMPLATE_DIR", Path(empty)):
                app.write_project_files(repo_dir, include_workflow=True)
            self.assertFalse((repo_dir / ".github/workflows/build.yml").exists())
            self.assertTrue((repo_dir / "recipes/recipe.yml").exists())

    def test_write_bluebuild_project_files_updates_gitignore(self) -> None:
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".gitignore").write_text("*.pyc\n")
            app.write_project_files(repo_dir, include_workflow=False)
            gitignore = (repo_dir / ".gitignore").read_text()
            self.assertIn("cosign.key", gitignore)
            self.assertIn("cosign.private", gitignore)

    def test_write_bluebuild_project_files_roundtrips_config(self) -> None:
        """State file written by BlueBuild write_project_files survives load_repo_config."""
        app = self.make_bluebuild_app()
        app.config.packages = ["htop", "tmux"]
        app.config.removed_packages = ["firefox"]
        app.config.copr_repos = ["kylegospo/bazzite"]
        app.config.services = ["tailscaled.service"]
        app.config.image_desc = "Test BB roundtrip"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            app2 = self.make_bluebuild_app()
            app2.load_repo_config(repo_dir)
        self.assertEqual(app2.config.method, "bluebuild")
        self.assertEqual(app2.config.packages, ["htop", "tmux"])
        self.assertEqual(app2.config.removed_packages, ["firefox"])
        self.assertEqual(app2.config.copr_repos, ["kylegospo/bazzite"])
        self.assertEqual(app2.config.services, ["tailscaled.service"])
        self.assertEqual(app2.config.image_desc, "Test BB roundtrip")

    def test_write_bluebuild_project_files_roundtrips_brew_enabled(self) -> None:
        app = self.make_bluebuild_app()
        app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        app.config.base_image_name = "Silverblue"
        app.config.brew_enabled = True
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.write_project_files(repo_dir, include_workflow=False)
            state = json.loads((repo_dir / STATE_FILE).read_text())
            self.assertTrue(state["brew_enabled"])
            recipe = (repo_dir / "recipes/recipe.yml").read_text()
            self.assertIn(f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /", recipe)
            self.assertIn("brew-setup.service", recipe)

    def test_validate_config_rejects_empty_method(self) -> None:
        app = self.make_app()
        app.config.method = ""
        with self.assertRaisesRegex(CommandError, "supported build method"):
            app.validate_config()

    def test_validate_config_rejects_unknown_method(self) -> None:
        app = self.make_app()
        app.config.method = "buildah"
        with self.assertRaisesRegex(CommandError, "supported build method"):
            app.validate_config()

    def test_validate_config_accepts_bluebuild_method(self) -> None:
        app = self.make_bluebuild_app()
        app.validate_config()

    def test_config_from_state_payload_rejects_unsupported_method(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported build method"):
            config_from_state_payload({"method": "buildah"})

    def test_config_from_state_payload_loads_bluebuild_method(self) -> None:
        cfg = config_from_state_payload({"method": "bluebuild"})
        self.assertEqual(cfg.method, "bluebuild")

    def test_show_summary_includes_build_method(self) -> None:
        app = self.make_bluebuild_app()
        app.gum = GumStub()
        captured: list[str] = []
        app.gum.pager = lambda text: captured.append(text)
        app.show_summary()
        self.assertTrue(captured)
        self.assertIn("BlueBuild", captured[0])

    def test_generate_readme_uses_method_display(self) -> None:
        app = self.make_bluebuild_app()
        readme = app.generate_readme()
        self.assertIn(METHOD_DISPLAY["bluebuild"], readme)

    def test_method_display_covers_all_allowed_methods(self) -> None:
        from atomic_image_builder import ALLOWED_METHODS
        for method in ALLOWED_METHODS:
            self.assertIn(method, METHOD_DISPLAY)
            self.assertTrue(METHOD_DISPLAY[method])

    def test_write_bluebuild_project_files_restores_missing_workflow(self) -> None:
        """If the workflow file is missing (e.g. manually deleted), it should be
        restored from the template snapshot during an update."""
        app = self.make_bluebuild_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            # Write project files WITHOUT seeding the template first so
            # there is no pre-existing workflow file.
            app.write_project_files(repo_dir, include_workflow=True)
            workflow_path = repo_dir / ".github/workflows/build.yml"
            self.assertTrue(workflow_path.exists(), "Workflow should be restored from template snapshot")
            workflow = workflow_path.read_text()
            self.assertIn(ACTION_PINS["blue-build/github-action"][0], workflow)
            self.assertIn(DEFAULT_GITHUB_BUILD_CRON, workflow)


    # ── manage_removed_packages update flow ──────────────────────────────

    def test_manage_removed_packages_add_flow(self) -> None:
        """Adding packages through manage_removed_packages reaches
        add_removed_packages_to_config and updates the config."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Add package names to remove"]
        stub.write = lambda **_kwargs: "vim-enhanced"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            app.manage_removed_packages()
        self.assertIn("vim-enhanced", app.config.removed_packages)

    def test_manage_removed_packages_add_flow_accepts_comma_separated_entry(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Add package names to remove"]
        stub.write = lambda **_kwargs: "vim-enhanced,nano"
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            app.manage_removed_packages()
        self.assertEqual(app.config.removed_packages, ["vim-enhanced", "nano"])

    def test_manage_removed_packages_remove_flow(self) -> None:
        """Choosing 'Stop removing listed packages' lets the user deselect
        previously listed removals."""
        app = self.make_app()
        app.config.removed_packages = ["vim-enhanced", "nano"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Stop removing listed packages"]
        app.gum = stub
        # choose_to_remove will be called; stub it to remove "nano"
        with patch.object(app, "choose_to_remove", return_value=["vim-enhanced"]) as mock:
            app.manage_removed_packages()
        mock.assert_called_once_with(["vim-enhanced", "nano"], "Remove Base Package Removals")
        self.assertEqual(app.config.removed_packages, ["vim-enhanced"])

    def test_manage_removed_packages_back_is_noop(self) -> None:
        """Choosing Back or pressing Esc returns without changes."""
        app = self.make_app()
        app.config.removed_packages = ["vim-enhanced"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        app.manage_removed_packages()
        self.assertEqual(app.config.removed_packages, ["vim-enhanced"])

    # ── manage_services update flow ────────────────────────────────────

    def test_manage_services_add_delegates_to_add_services(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Add services"]
        app.gum = stub
        with patch.object(app, "add_services") as mock:
            app.manage_services()
        mock.assert_called_once()


    # ── interactive wizard steps ───────────────────────────────────────
    #
    # These methods encode the selection decisions that drive what gets
    # written into the generated Containerfile/recipe/workflow, and they were
    # almost entirely uncovered: a regression here silently produces wrong
    # build artifacts instead of failing loudly. Each test drives the real
    # method through a GumStub, mocking only the neighbours it dispatches to.

    def test_choose_method_sets_bluebuild_when_selected(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda options, **_kwargs: [options[1]]
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.choose_method(step=1, total_steps=5)
        self.assertEqual(app.config.method, "bluebuild")
        self.assertTrue(any(level == "success" and "BlueBuild" in msg for level, msg in stub.messages))

    def test_choose_method_defaults_to_containerfile_on_empty_choice(self) -> None:
        app = self.make_app()
        app.config.method = "bluebuild"
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.choose_method()
        self.assertEqual(app.config.method, "containerfile")

    def test_choose_base_image_keeps_confirmed_detected_image(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda *_a, **_k: True
        stub.choose = lambda *_a, **_k: self.fail("choose must not run when the detected image is confirmed")
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable") as brew:
            with redirect_stdout(io.StringIO()):
                app.choose_base_image(step=2, total_steps=5)
        brew.assert_called_once()
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:stable")

    def test_choose_base_image_discards_unsupported_detected_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/example/not-curated:latest"
        app.config.base_image_name = "Mystery"
        stub = GumStub()
        stub.choose = lambda options, **_kwargs: [next(o for o in options if "Aurora (KDE)" in o)]
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable"):
            with redirect_stdout(io.StringIO()):
                app.choose_base_image()
        self.assertTrue(any(level == "warn" and "curated" in msg for level, msg in stub.messages))
        self.assertEqual(app.config.base_image_name, "Aurora (KDE)")
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/aurora:stable")

    def test_choose_base_image_declining_the_detected_image_offers_the_list(self) -> None:
        # Answering no to "Use this base image?" is not a cancel: it means
        # "show me the others", so the picker has to run and win.
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda *_a, **_k: False
        stub.choose = lambda options, **_kwargs: [next(o for o in options if "Aurora (KDE)" in o)]
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable"):
            with redirect_stdout(io.StringIO()):
                app.choose_base_image(step=2, total_steps=5)
        self.assertEqual(app.config.base_image_name, "Aurora (KDE)")
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/aurora:stable")

    def test_choose_base_image_keeps_the_previous_image_when_nothing_matches(self) -> None:
        # The picker matches the returned line back to an image by name. If
        # nothing matches, the loop finishes without setting anything, and the
        # config must be left as it was rather than half-written.
        app = self.make_app()
        app.config.base_image_uri = ""
        app.config.base_image_name = ""
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["[ublue] Something Else  [ghcr.io/example/other:latest]"]
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable"):
            with redirect_stdout(io.StringIO()):
                app.choose_base_image()
        self.assertEqual(app.config.base_image_uri, "")
        self.assertEqual(app.config.base_image_name, "")

    def test_choose_base_image_defaults_to_first_option_on_empty_choice(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = ""
        app.config.base_image_name = ""
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable"):
            with redirect_stdout(io.StringIO()):
                app.choose_base_image()
        self.assertEqual(app.config.base_image_name, BASE_IMAGES[0].name)
        self.assertEqual(app.config.base_image_uri, BASE_IMAGES[0].image_uri)

    def test_configure_repo_rejects_invalid_name_then_accepts_valid_one(self) -> None:
        app = self.make_app()
        stub = GumStub()
        name_attempts = iter(["My Repo.git", "shiny-image", "A custom description"])
        stub.input = lambda **_kwargs: next(name_attempts)
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.configure_repo(step=3, total_steps=5)
        self.assertEqual(app.config.repo_name, "shiny-image")
        self.assertEqual(app.config.image_desc, "A custom description")
        self.assertTrue(any(level == "error" and ".git" in msg for level, msg in stub.messages))
        self.assertTrue(any("try another repository name" in prompt for prompt in stub.prompts))

    def test_configure_repo_rejects_a_name_no_image_reference_can_parse(self) -> None:
        # The wizard is where this has to stop: past it the name reaches
        # `gh repo create` and the signing key, and neither is undone by
        # discovering later that the image reference does not parse.
        app = self.make_app()
        stub = GumStub()
        name_attempts = iter(["test..image", "test.image", "A custom description"])
        stub.input = lambda **_kwargs: next(name_attempts)
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.configure_repo(step=3, total_steps=5)
        self.assertEqual(app.config.repo_name, "test.image")
        self.assertTrue(
            any(level == "error" and "single dot" in msg for level, msg in stub.messages),
            stub.messages,
        )

    def test_configure_repo_empty_inputs_keep_defaults(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.repo_name = ""
        app.config.image_desc = "Existing description"
        stub = GumStub()
        stub.input = lambda **_kwargs: ""
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.configure_repo()
        self.assertEqual(app.config.repo_name, DEFAULT_REPO_NAME)
        self.assertEqual(app.config.image_desc, "Existing description")
        self.assertTrue(any(level == "success" and "example/" in msg for level, msg in stub.messages))

    def test_add_services_dispatches_both_entry_points_then_backs_out(self) -> None:
        app = self.make_app()
        selections = iter(
            [
                ["Choose from common services"],
                ["Type service names manually (advanced)"],
                ["Back"],
            ]
        )
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: next(selections)
        app.gum = stub
        with patch.object(app, "select_common_services") as common:
            with patch.object(app, "add_services_manually") as manual:
                with redirect_stdout(io.StringIO()):
                    app.add_services()
        common.assert_called_once()
        manual.assert_called_once()

    def test_add_services_escape_returns(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.add_services()  # must return, not raise

    def review_choice(self, app, label_fragment: str):
        """Drive review_new_image one selection at a time by substring."""

        def choose(options, **_kwargs):
            if label_fragment == "":
                return []
            return [next(option for option in options if label_fragment in option)]

        return choose

    def test_review_new_image_returns_the_selected_section(self) -> None:
        expectations = [
            ("Start GitHub build", "build"),
            ("Build method", "method"),
            ("Software", "software"),
            ("Repository settings", "repo"),
            ("Base image", "base"),
            ("Cancel and return", "cancel"),
            ("", "cancel"),  # empty choice falls back to cancel
        ]
        for fragment, expected in expectations:
            with self.subTest(fragment=fragment or "(empty)"):
                app = self.make_app()
                stub = GumStub()
                stub.choose = self.review_choice(app, fragment)
                app.gum = stub
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(app.review_new_image(step=5, total_steps=5), expected)

    def test_review_new_image_full_config_and_local_build_loop_back(self) -> None:
        app = self.make_app()
        selections = iter(
            [
                ["View full configuration"],
                ["Test build locally (podman)"],
                ["Start GitHub build"],
            ]
        )
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: next(selections)
        app.gum = stub
        with patch.object(app, "show_summary") as summary:
            with patch.object(app, "test_build_locally") as local:
                with redirect_stdout(io.StringIO()):
                    result = app.review_new_image(step=5, total_steps=5)
        summary.assert_called_once()
        local.assert_called_once()
        self.assertEqual(result, "build")

    def test_review_new_image_hides_local_build_for_bluebuild(self) -> None:
        app = self.make_app()
        app.config.method = "bluebuild"
        seen: list[list[str]] = []

        def choose(options, **_kwargs):
            seen.append(list(options))
            return [next(option for option in options if "Cancel and return" in option)]

        stub = GumStub()
        stub.choose = choose
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.review_new_image(step=5, total_steps=5)
        self.assertFalse(any("podman" in option for option in seen[0]))

    def scan_payload(self, packages: list[str], removals: list[str]) -> str:
        return json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": packages,
                        "requested-base-removals": removals,
                    }
                ]
            }
        )

    def run_scan(self, app, payload: str) -> bool:
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, payload, ""),
            ):
                with redirect_stdout(io.StringIO()):
                    return app.scan_os()

    def test_scan_os_carries_selected_packages_and_removals(self) -> None:
        app = self.make_app()
        stub = GumStub()
        # First choose: keep only tmux of the layered packages. Second choose:
        # keep the base removal selected.
        selections = iter([["tmux"], ["firefox"]])
        stub.choose = lambda _options, **_kwargs: next(selections)
        app.gum = stub
        result = self.run_scan(app, self.scan_payload(["tmux", "htop"], ["firefox"]))
        self.assertEqual(result, SCAN_OK)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(app.config.removed_packages, ["firefox"])

    def test_scan_os_escape_from_package_selection_aborts(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        self.assertEqual(self.run_scan(app, self.scan_payload(["tmux"], [])), SCAN_CANCELLED)

    def test_scan_os_escape_from_removal_selection_aborts(self) -> None:
        app = self.make_app()
        stub = GumStub()
        calls = [0]

        def choose(_options, **_kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return ["tmux"]
            raise ScreenBack()

        stub.choose = choose
        app.gum = stub
        self.assertEqual(self.run_scan(app, self.scan_payload(["tmux"], ["firefox"])), SCAN_CANCELLED)

    def test_scan_os_declining_empty_package_scan_aborts(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.confirm = lambda *_a, **_k: False
        app.gum = stub
        self.assertEqual(self.run_scan(app, self.scan_payload([], [])), SCAN_CANCELLED)
        self.assertTrue(any(level == "warn" and "No layered packages" in msg for level, msg in stub.messages))

    def test_manage_packages_dispatches_each_editing_task(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "htop"]
        selections = iter(
            [
                ["Search package names"],
                ["Type exact package names"],
                ["Remove packages"],
                ["Back"],
            ]
        )
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: next(selections)
        app.gum = stub
        with patch.object(app, "search_packages") as search:
            with patch.object(app, "manual_packages") as manual:
                with patch.object(app, "choose_to_remove", return_value=["tmux"]) as remove:
                    with redirect_stdout(io.StringIO()):
                        app.manage_packages()
        search.assert_called_once()
        manual.assert_called_once()
        remove.assert_called_once_with(["tmux", "htop"], "Remove Packages")
        self.assertEqual(app.config.packages, ["tmux"])

    def test_manage_packages_inner_escape_returns_to_menu(self) -> None:
        app = self.make_app()
        selections = iter([["Search package names"], ["Back"]])
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: next(selections)
        app.gum = stub
        with patch.object(app, "search_packages", side_effect=ScreenBack()):
            with redirect_stdout(io.StringIO()):
                app.manage_packages()  # Esc inside a task loops back, Back exits

    def test_manage_packages_escape_at_menu_returns(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.manage_packages()  # must return, not raise

    def test_choose_to_remove_warns_and_keeps_empty_list(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda *_a, **_k: self.fail("choose must not run with nothing to remove")
        app.gum = stub
        self.assertEqual(app.choose_to_remove([], "Remove Packages"), [])
        self.assertTrue(any(level == "warn" and "Nothing to remove" in msg for level, msg in stub.messages))

    def test_choose_to_remove_drops_selection_preserving_order(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["b", "d"]
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            kept = app.choose_to_remove(["a", "b", "c", "d"], "Remove Packages")
        self.assertEqual(kept, ["a", "c"])

    def test_render_preflight_failure_plain_text_fallback_lists_every_section(self) -> None:
        # Users without gum installed hit this path when preflight fails; it
        # had zero regression protection.
        app = self.make_app()
        stdout = io.StringIO()
        with patch("atomic_image_builder.command_exists", return_value=False):
            with redirect_stdout(stdout):
                app.render_preflight_failure(
                    missing_tools=["gum", "cosign"],
                    missing_host_tools=["dnf5"],
                    github_login_missing=True,
                    github_account_error=True,
                )
        output = stdout.getvalue()
        self.assertIn("Preflight Failed", output)
        self.assertIn("Missing tools: gum, cosign", output)
        self.assertIn("brew install gum cosign", output)
        self.assertIn("Run: gh auth login", output)
        self.assertIn("gh auth status && gh auth login", output)
        self.assertIn("Missing host tools: dnf5", output)
        self.assertIn("rpm-ostree", output)

    def test_render_preflight_failure_gum_path_covers_account_and_host_sections(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub
        stdout = io.StringIO()
        with patch("atomic_image_builder.command_exists", return_value=True):
            with redirect_stdout(stdout):
                app.render_preflight_failure(
                    missing_tools=[],
                    missing_host_tools=["dnf5", "rpm-ostree"],
                    github_account_error=True,
                )
        hints = [msg for level, msg in stub.messages if level == "hint"]
        self.assertTrue(any("gh auth status && gh auth login" in msg for msg in hints))
        self.assertTrue(any("Missing host tools: dnf5, rpm-ostree" in msg for msg in hints))
        self.assertEqual(stub.prompts, ["Press Enter to exit to the terminal..."])

    def test_render_preflight_failure_plain_text_fallback_skips_absent_sections(self) -> None:
        # The sibling test above passes every section's condition as True, so
        # branch coverage never sees the five independent conditionals in
        # this fallback take their False side -- each `if` looked "covered"
        # while only ever being exercised one way. This drives all five false.
        app = self.make_app()
        stdout = io.StringIO()
        with patch("atomic_image_builder.command_exists", return_value=False):
            with redirect_stdout(stdout):
                app.render_preflight_failure(
                    missing_tools=[],
                    missing_host_tools=[],
                    github_login_missing=False,
                    github_account_error=False,
                )
        output = stdout.getvalue()
        self.assertIn("Preflight Failed", output)
        self.assertNotIn("Missing tools:", output)
        self.assertNotIn("Install with Homebrew:", output)
        self.assertNotIn("gh auth login", output)
        self.assertNotIn("gh auth status", output)
        self.assertNotIn("Missing host tools:", output)
        self.assertNotIn("rpm-ostree", output)

    def test_manage_services_remove_calls_choose_to_remove(self) -> None:
        app = self.make_app()
        app.config.services = ["sshd.service", "tailscaled.service"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Remove services"]
        app.gum = stub
        with patch.object(app, "choose_to_remove", return_value=["sshd.service"]) as mock:
            app.manage_services()
        mock.assert_called_once_with(["sshd.service", "tailscaled.service"], "Remove Services")
        self.assertEqual(app.config.services, ["sshd.service"])

    def test_manage_services_back_is_noop(self) -> None:
        app = self.make_app()
        app.config.services = ["sshd.service"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        app.manage_services()
        self.assertEqual(app.config.services, ["sshd.service"])

    # ── manage_copr_repos update flow ──────────────────────────────────

    def test_add_copr_accepts_comma_separated_packages(self) -> None:
        app = self.make_app()
        stub = GumStub()

        def fake_input(*, prompt, **_kwargs):
            if prompt == "COPR repo: ":
                return "kwizart/fedy"
            return "tmux,htop"

        stub.input = fake_input
        app.gum = stub
        with patch.object(app, "lookup_host_packages", side_effect=lambda pkgs: {p: True for p in pkgs}):
            app.add_copr()
        self.assertEqual(app.config.copr_repos, ["kwizart/fedy"])
        self.assertEqual(app.config.packages, ["tmux", "htop"])

    def test_add_copr_returns_silently_when_repo_is_empty(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda **_kwargs: "   "
        app.gum = stub
        app.add_copr()
        self.assertEqual(app.config.copr_repos, [])
        self.assertEqual([m for m in app.gum.messages if m[0] == "error"], [])
        self.assertEqual([m for m in app.gum.messages if m[0] == "success"], [])

    def test_add_copr_rejects_invalid_repo_format(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda *, prompt, **_kwargs: "not-a-valid-repo"
        app.gum = stub
        app.add_copr()
        self.assertEqual(app.config.copr_repos, [])
        self.assertIn(("error", "Enter the COPR repo as owner/project."), app.gum.messages)

    def test_add_copr_returns_without_adding_repo_when_packages_fail_validation(self) -> None:
        app = self.make_app()
        stub = GumStub()

        def fake_input(*, prompt, **_kwargs):
            if prompt == "COPR repo: ":
                return "kwizart/fedy"
            return "bad;rm"

        stub.input = fake_input
        app.gum = stub
        app.add_copr()
        self.assertEqual(app.config.copr_repos, [])
        self.assertEqual(app.config.packages, [])

    def test_manage_copr_repos_add_delegates_to_add_copr(self) -> None:
        app = self.make_app()
        call_count = [0]
        def fake_choose(_options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return ["Add a COPR repository"]
            return ["Back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "add_copr") as mock:
            app.manage_copr_repos()
        mock.assert_called_once()

    def test_manage_copr_repos_remove_calls_choose_to_remove(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        call_count = [0]
        def fake_choose(_options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return ["Remove a COPR repository"]
            return ["Back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "choose_to_remove", return_value=[]) as mock:
            app.manage_copr_repos()
        mock.assert_called_once_with(["foo/bar"], "Remove COPR Repos")

    # ── menus must ignore a selection they do not recognise ────────────
    #
    # Every one of these menus dispatches through an if/elif chain with no
    # else. Nothing in the tool guarantees gum hands back a string from the
    # list it was given -- a truncated read or a garbled line is enough -- and
    # the chain falling through is the behaviour that keeps that from turning
    # into a crash or a silently wrong action. Until now no test had ever let
    # one fall through, so "ignores it" and "does something unintended" were
    # indistinguishable.

    def test_main_menu_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        choices = ["Rebuild everything", "Quit"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "create_image") as create:
            with patch.object(app, "update_existing_image") as update:
                with patch.object(app, "view_build_status") as status:
                    with self.assertRaises(SystemExit):
                        app.main_menu()
        create.assert_not_called()
        update.assert_not_called()
        status.assert_not_called()

    def test_select_packages_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]
        choices = ["Reticulate splines", "Continue to review"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "choose_to_remove") as remove:
            app.select_packages()
        remove.assert_not_called()
        self.assertEqual(app.config.packages, ["fish"])

    def test_add_services_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        choices = ["Choose from uncommon services", "Back"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "select_common_services") as common:
            with patch.object(app, "add_services_manually") as manual:
                with redirect_stdout(io.StringIO()):
                    app.add_services()
        common.assert_not_called()
        manual.assert_not_called()

    def test_update_menu_ignores_a_task_with_no_dispatch_arm(self) -> None:
        # update_menu is the odd one out: it maps the chosen label back to a
        # task title through a dict, so an unrecognised *label* raises
        # KeyError rather than falling through. The fall-through here belongs
        # to the task titles instead, and it is a maintenance hazard -- adding
        # a title to update_task_choices without adding a dispatch arm makes
        # that menu entry do nothing at all, silently.
        app = self.make_app()
        choices = ["Notifications", "Cancel and go back"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "update_task_choices", return_value=[("Notifications", "")]):
            with patch.object(app, "format_task_choice", side_effect=lambda title, _status: title):
                with patch.object(app, "edit_description") as edit:
                    with patch.object(app, "offer_brew_if_applicable") as brew:
                        with redirect_stdout(io.StringIO()):
                            self.assertFalse(app.update_menu())
        edit.assert_not_called()
        brew.assert_not_called()

    def test_manage_packages_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux"]
        choices = ["Delete packages", "Back"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "search_packages") as search:
            with patch.object(app, "choose_to_remove") as remove:
                with redirect_stdout(io.StringIO()):
                    app.manage_packages()
        search.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(app.config.packages, ["tmux"])

    def test_manage_copr_repos_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        choices = ["Drop a COPR repository", "Back"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "add_copr") as add:
            with patch.object(app, "choose_to_remove") as remove:
                with redirect_stdout(io.StringIO()):
                    app.manage_copr_repos()
        add.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(app.config.copr_repos, ["foo/bar"])

    def test_manage_services_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        app.config.services = ["sshd.service"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Disable services"]
        app.gum = stub
        with patch.object(app, "add_services") as add:
            with patch.object(app, "choose_to_remove") as remove:
                with redirect_stdout(io.StringIO()):
                    app.manage_services()
        add.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(app.config.services, ["sshd.service"])

    def test_manage_removed_packages_ignores_an_unrecognised_selection(self) -> None:
        app = self.make_app()
        app.config.removed_packages = ["firefox"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Purge base packages"]
        app.gum = stub
        with patch.object(app, "choose_to_remove") as remove:
            with redirect_stdout(io.StringIO()):
                app.manage_removed_packages()
        remove.assert_not_called()
        self.assertEqual(app.config.removed_packages, ["firefox"])

    def test_manage_copr_repos_back_exits_loop(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Back"]
        app.gum = stub
        app.manage_copr_repos()
        self.assertEqual(app.config.copr_repos, ["foo/bar"])

    # ── edit_description update flow ───────────────────────────────────

    def test_edit_description_updates_config(self) -> None:
        app = self.make_app()
        app.config.image_desc = "Old description"
        stub = GumStub()
        stub.input = lambda **_kwargs: "New description"
        app.gum = stub
        app.edit_description()
        self.assertEqual(app.config.image_desc, "New description")

    def test_edit_description_empty_keeps_current(self) -> None:
        app = self.make_app()
        app.config.image_desc = "Keep this"
        stub = GumStub()
        stub.input = lambda **_kwargs: ""
        app.gum = stub
        app.edit_description()
        self.assertEqual(app.config.image_desc, "Keep this")

    # ── update_menu full flow ──────────────────────────────────────────

    def test_update_menu_save_returns_true(self) -> None:
        """Choosing 'Save and push changes' returns True."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Save and push changes"]
        app.gum = stub
        self.assertTrue(app.update_menu())

    def test_update_menu_cancel_returns_false(self) -> None:
        """Choosing 'Cancel and go back' returns False."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Cancel and go back"]
        app.gum = stub
        self.assertFalse(app.update_menu())

    def test_update_menu_dispatches_to_edit_description(self) -> None:
        """Selecting 'Description' from the menu dispatches to edit_description."""
        app = self.make_app()
        call_count = [0]
        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Find the option containing "Description"
                for opt in options:
                    if "Description" in opt:
                        return [opt]
            return ["Cancel and go back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "edit_description") as mock:
            app.update_menu()
        mock.assert_called_once()

    def test_update_menu_dispatches_to_manage_services(self) -> None:
        """Selecting 'Services' from the menu dispatches to manage_services."""
        app = self.make_app()
        call_count = [0]
        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for opt in options:
                    if "Services" in opt:
                        return [opt]
            return ["Cancel and go back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "manage_services") as mock:
            app.update_menu()
        mock.assert_called_once()

    def test_update_menu_dispatches_to_manage_removed_packages(self) -> None:
        """Selecting 'Removed base packages' dispatches to manage_removed_packages."""
        app = self.make_app()
        call_count = [0]
        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for opt in options:
                    if "Removed base packages" in opt:
                        return [opt]
            return ["Cancel and go back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "manage_removed_packages") as mock:
            app.update_menu()
        mock.assert_called_once()

    def test_update_menu_dispatches_to_manage_copr_repos(self) -> None:
        """Selecting 'COPR repositories' dispatches to manage_copr_repos."""
        app = self.make_app()
        call_count = [0]
        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for opt in options:
                    if "COPR repositories" in opt:
                        return [opt]
            return ["Cancel and go back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "manage_copr_repos") as mock:
            app.update_menu()
        mock.assert_called_once()

    def test_update_menu_dispatches_to_offer_brew_if_applicable(self) -> None:
        """Selecting 'Homebrew' from the menu dispatches to offer_brew_if_applicable.

        The task only appears on non-Universal-Blue bases (is_universal_blue_base()
        is False), so the fixture's ublue-os base is swapped for a plain bootc one.
        """
        app = self.make_app()
        app.config.base_image_uri = "quay.io/fedora/fedora-bootc:41"
        app.config.base_image_name = "Fedora bootc"
        call_count = [0]
        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for opt in options:
                    if "Homebrew" in opt:
                        return [opt]
            return ["Cancel and go back"]
        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "offer_brew_if_applicable") as mock:
            app.update_menu()
        mock.assert_called_once()

    def test_update_menu_esc_returns_false(self) -> None:
        """ScreenBack from choose returns False (cancel)."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        self.assertFalse(app.update_menu())

    def menu_stub_choosing(self, first_label: str) -> GumStub:
        """A GumStub whose choose picks first_label once, then cancels."""
        calls = [0]

        def fake_choose(options, **_kwargs):
            calls[0] += 1
            if calls[0] == 1:
                self.assertIn(first_label, options)
                return [first_label]
            return ["Cancel and go back"]

        stub = GumStub()
        stub.choose = fake_choose
        return stub

    def test_update_menu_review_shows_full_summary(self) -> None:
        app = self.make_app()
        app.gum = self.menu_stub_choosing("Review current configuration")
        with patch.object(app, "show_summary") as summary_mock:
            self.assertFalse(app.update_menu())
        summary_mock.assert_called_once()

    def test_update_menu_runs_local_test_build(self) -> None:
        app = self.make_app()
        app.gum = self.menu_stub_choosing("Test build locally (podman)")
        with patch.object(app, "test_build_locally") as build_mock:
            self.assertFalse(app.update_menu())
        build_mock.assert_called_once()

    def test_update_menu_hides_local_build_for_bluebuild(self) -> None:
        # The local test build is Containerfile-only, so the BlueBuild menu
        # must not offer it at all.
        app = self.make_app()
        app.config.method = "bluebuild"
        seen_options: list[str] = []

        def fake_choose(options, **_kwargs):
            seen_options.extend(options)
            return ["Cancel and go back"]

        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        self.assertFalse(app.update_menu())
        self.assertNotIn("Test build locally (podman)", seen_options)
        self.assertIn("Rotate signing key (cosign)", seen_options)

    def test_update_menu_rotates_signing_key_with_repo_dir(self) -> None:
        app = self.make_app()
        app.gum = self.menu_stub_choosing("Rotate signing key (cosign)")
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch.object(app, "rotate_signing_key") as rotate_mock:
                self.assertFalse(app.update_menu(repo_dir))
        rotate_mock.assert_called_once_with(repo_dir)

    def test_update_menu_survives_command_error_from_screen_action(self) -> None:
        # run_screen_action must contain a CommandError from the launched
        # action so the update session is not lost to the main menu.
        app = self.make_app()
        stub = self.menu_stub_choosing("Rotate signing key (cosign)")
        app.gum = stub
        with patch.object(app, "rotate_signing_key", side_effect=CommandError("rotation boom")):
            self.assertFalse(app.update_menu())
        self.assertIn(("error", "rotation boom"), stub.messages)
        self.assertIn("Press Enter to return to the update menu...", stub.prompts)

    def test_update_menu_dispatches_to_manage_packages(self) -> None:
        """Selecting 'Packages' from the menu dispatches to manage_packages."""
        app = self.make_app()
        call_count = [0]

        def fake_choose(options, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for opt in options:
                    if "Packages" in opt and "Removed" not in opt:
                        return [opt]
            return ["Cancel and go back"]

        stub = GumStub()
        stub.choose = fake_choose
        app.gum = stub
        with patch.object(app, "manage_packages") as mock:
            app.update_menu()
        mock.assert_called_once()

    # ── require_github auth gate ───────────────────────────────────────

    def test_require_github_errors_when_gh_is_missing(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=False):
            with patch("atomic_image_builder.run") as run_mock:
                self.assertFalse(app.require_github())

        run_mock.assert_not_called()
        self.assertFalse(app.github_available)
        self.assertIn(("error", "GitHub CLI is required for this action."), stub.messages)
        self.assertTrue(any(level == "hint" and "brew install gh" in message for level, message in stub.messages))

    def test_require_github_runs_setup_guide_when_not_logged_in(self) -> None:
        app = self.make_app()
        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            self.assertEqual(list(args), ["gh", "auth", "status"])
            return subprocess.CompletedProcess(list(args), 1, "", "not logged in")

        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch("atomic_image_builder.run", side_effect=fake_run):
                with patch.object(app, "github_setup_guide") as guide_mock:
                    with patch.object(app, "github_login_name", return_value="octocat"):
                        self.assertTrue(app.require_github())

        guide_mock.assert_called_once()
        self.assertTrue(app.github_available)
        self.assertEqual(app.github_user, "octocat")

    def test_require_github_returns_false_when_setup_guide_is_escaped(self) -> None:
        # Esc from the login guide raises ScreenBack; require_github must turn
        # that into a plain False so callers back out instead of crashing.
        app = self.make_app()
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", ""),
            ):
                with patch.object(app, "github_setup_guide", side_effect=ScreenBack):
                    self.assertFalse(app.require_github())

        self.assertFalse(app.github_available)

    def test_require_github_errors_when_login_name_lookup_fails(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 0, "", ""),
            ):
                with patch.object(app, "github_login_name", side_effect=CommandError("api down")):
                    self.assertFalse(app.require_github())

        self.assertFalse(app.github_available)
        self.assertIn(("error", "Unable to determine GitHub username after login."), stub.messages)

    def test_require_github_records_user_on_success(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists", return_value=True):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 0, "", ""),
            ):
                with patch.object(app, "github_setup_guide") as guide_mock:
                    with patch.object(app, "github_login_name", return_value="octocat"):
                        self.assertTrue(app.require_github())

        guide_mock.assert_not_called()
        self.assertTrue(app.github_available)
        self.assertEqual(app.github_user, "octocat")
        self.assertEqual(app.config.github_user, "octocat")
        self.assertIn(("success", "GitHub ready: octocat"), stub.messages)

    def test_require_github_short_circuits_when_already_ready(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "octocat"
        app.gum = GumStub()
        with patch("atomic_image_builder.run") as run_mock:
            self.assertTrue(app.require_github())
        run_mock.assert_not_called()

    # ── github_setup_guide interactive login flow ──────────────────────

    def test_github_setup_guide_quit_option_exits(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Quit"]
        app.gum = stub
        with self.assertRaises(SystemExit):
            app.github_setup_guide()

    def test_github_setup_guide_login_success(self) -> None:
        """Choosing 'I already have a GitHub account' proceeds to gh auth login."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["I already have a GitHub account - log me in"]
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "login"], 0, "", "")):
            app.github_setup_guide()

    def test_github_setup_guide_login_failure_exits(self) -> None:
        """A failed gh auth login raises SystemExit."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["I already have a GitHub account - log me in"]
        stub.confirm = lambda _prompt, **_kwargs: True
        app.gum = stub
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "login"], 1, "", "")):
            with self.assertRaises(SystemExit):
                app.github_setup_guide()

    def test_github_setup_guide_create_account_then_login(self) -> None:
        """Choosing 'I need to create a GitHub account first' shows signup info,
        then proceeds to login."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["I need to create a GitHub account first"]
        stub.confirm = lambda _prompt, **_kwargs: False  # Don't open browser, don't log in
        app.gum = stub
        with self.assertRaises(SystemExit):
            # confirm("Ready to log in?") returns False → SystemExit(0)
            app.github_setup_guide()

    # ── batch_check_state_files GraphQL path ───────────────────────────

    def test_batch_check_state_files_graphql_success(self) -> None:
        """When the GraphQL query succeeds, repos with state files are identified."""
        app = self.make_app()
        repos = [{"name": "repo-a"}, {"name": "repo-b"}, {"name": "repo-c"}]
        graphql_response = json.dumps({
            "data": {
                "r0": {"object": {"id": "abc123"}},
                "r1": None,
                "r2": {"object": {"id": "def456"}},
            }
        })
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, graphql_response, ""
        )):
            found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-a", "repo-c"})

    def test_batch_check_state_files_graphql_empty_object(self) -> None:
        """A repo where 'object' is null is not included in results."""
        app = self.make_app()
        repos = [{"name": "repo-a"}]
        graphql_response = json.dumps({
            "data": {
                "r0": {"object": None},
            }
        })
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, graphql_response, ""
        )):
            found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, set())

    def test_batch_check_state_files_empty_repos(self) -> None:
        """An empty repo list returns an empty set without making any calls."""
        app = self.make_app()
        with patch("atomic_image_builder.run") as run_mock:
            found = app.batch_check_state_files("testuser", [])
        run_mock.assert_not_called()
        self.assertEqual(found, set())

    def test_batch_check_state_files_graphql_failure_falls_back_to_rest(self) -> None:
        """When GraphQL fails, the method falls back to serial REST calls."""
        app = self.make_app()
        repos = [{"name": "repo-a"}, {"name": "repo-b"}]
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 1, "", "GraphQL error"
        )):
            with patch.object(app, "repo_has_state_file", side_effect=[True, False]) as rest_mock:
                found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-a"})
        self.assertEqual(rest_mock.call_count, 2)

    def test_batch_check_state_files_graphql_bad_json_falls_back(self) -> None:
        """When GraphQL returns non-JSON, the method falls back to REST."""
        app = self.make_app()
        repos = [{"name": "repo-a"}]
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, "not json", ""
        )):
            with patch.object(app, "repo_has_state_file", return_value=True) as rest_mock:
                found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-a"})
        rest_mock.assert_called_once()

    def test_batch_check_state_files_null_data_falls_back(self) -> None:
        """gh exits 0 for GraphQL-level errors, returning an explicit null data."""
        app = self.make_app()
        repos = [{"name": "repo-a"}, {"name": "repo-b"}]
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, '{"data": null, "errors": [{"message": "Bad credentials"}]}', ""
        )):
            with patch.object(app, "repo_has_state_file", side_effect=[True, False]) as rest_mock:
                found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-a"})
        self.assertEqual(rest_mock.call_count, 2)

    def test_batch_check_state_files_non_dict_json_falls_back(self) -> None:
        """Valid JSON that is not an object must fall back, not raise."""
        app = self.make_app()
        repos = [{"name": "repo-a"}]
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, "[]", ""
        )):
            with patch.object(app, "repo_has_state_file", return_value=True) as rest_mock:
                found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-a"})
        rest_mock.assert_called_once()

    def test_batch_check_state_files_null_alias_entry_is_skipped(self) -> None:
        """A per-alias null (repo vanished mid-query) must not raise."""
        app = self.make_app()
        repos = [{"name": "repo-a"}, {"name": "repo-b"}]
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, '{"data": {"r0": null, "r1": {"object": {"id": "x"}}}}', ""
        )):
            found = app.batch_check_state_files("testuser", repos)
        self.assertEqual(found, {"repo-b"})

    # ── render_build_status timestamp handling ──────────────────────────

    def run_build_status(self, created_at: object) -> GumStub:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        payload = json.dumps([{
            "databaseId": 1,
            "displayTitle": "Build",
            "status": "completed",
            "conclusion": "success",
            "createdAt": created_at,
            "url": "https://example.invalid/run/1",
            "workflowName": "build",
        }])
        with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(
            ["gh"], 0, payload, ""
        )):
            app.render_build_status("testuser", "test-image")
        return stub

    def test_render_build_status_handles_naive_timestamp(self) -> None:
        # No Z and no offset: parses to a naive datetime, which previously
        # raised TypeError on the subtraction against an aware `now`.
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
        stub = self.run_build_status(recent.isoformat())
        row = next(m for _, m in stub.messages if "example.invalid" in m)
        self.assertIn("2h ago", row)

    def test_render_build_status_handles_unparseable_timestamp(self) -> None:
        stub = self.run_build_status("not a timestamp")
        row = next(m for _, m in stub.messages if "example.invalid" in m)
        self.assertIn("unknown", row)

    def test_render_build_status_handles_non_string_timestamp(self) -> None:
        stub = self.run_build_status(12345)
        row = next(m for _, m in stub.messages if "example.invalid" in m)
        self.assertIn("unknown", row)

    # ── Justfile / image-template.env restore-from-snapshot ─────────────

    def test_patch_container_justfile_no_ops_image_name_patch_on_new_upstream_syntax(self) -> None:
        # The current bundled Justfile sources image name via image-template.env
        # (env_var(...), no inline default) rather than the older env("IMAGE_NAME",
        # "default") form the image-name patch targets. That specific patch must
        # silently leave this line untouched; patch_image_template_env is what
        # wires the repo name into the newer template instead. (The rechunk
        # ARG_MAX fix inside the same patch_container_justfile call is a
        # separate, always-applicable patch -- see the rechunk-config-arg tests
        # above, including test_patch_container_justfile_also_fixes_rechunk_config_arg.)
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        result = app.patch_container_justfile(existing)
        self.assertIn('export image_name := env_var("IMAGE_NAME")', result)

    def test_write_project_files_restores_missing_justfile_and_env_from_snapshot(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            (repo_dir / "Justfile").unlink()
            (repo_dir / "image-template.env").unlink()
            app.write_project_files(repo_dir, include_workflow=True)
            justfile = (repo_dir / "Justfile").read_text()
            env_text = (repo_dir / "image-template.env").read_text()
        # A restored-then-written Justfile is the bundled snapshot after the
        # same patching every Justfile goes through (patch_container_justfile),
        # not the raw snapshot byte-for-byte.
        expected_justfile = app.patch_container_justfile((CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text())
        self.assertEqual(justfile, expected_justfile)
        self.assertIn("IMAGE_NAME=test-image", env_text)
        self.assertIn('REPO_ORGANIZATION="example"', env_text)
        self.assertIn('IMAGE_DESC="Test image"', env_text)

    def test_write_project_files_patches_env_without_restoring_existing_justfile(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            justfile_before = (repo_dir / "Justfile").read_text()
            app.write_project_files(repo_dir, include_workflow=True)
            justfile_after = (repo_dir / "Justfile").read_text()
            env_text = (repo_dir / "image-template.env").read_text()
        # Not restored from the snapshot (it already existed) -- but every
        # Justfile still goes through patch_container_justfile unconditionally.
        self.assertEqual(justfile_after, app.patch_container_justfile(justfile_before))
        self.assertIn("IMAGE_NAME=test-image", env_text)

    def test_write_project_files_does_not_add_env_file_to_old_shape_repos(self) -> None:
        # Repos generated from the older template have a Justfile that never
        # dotenv-loads image-template.env; updating them must not introduce an
        # inert copy of that file.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            (repo_dir / "image-template.env").unlink()
            (repo_dir / "Justfile").write_text(
                'export image_name := env("IMAGE_NAME", "old-name")\n'
                'export default_tag := env("DEFAULT_TAG", "latest")\n'
                "\n"
                "build:\n"
                "    podman build .\n"
            )
            app.write_project_files(repo_dir, include_workflow=True)
            env_exists = (repo_dir / "image-template.env").exists()
            justfile = (repo_dir / "Justfile").read_text()
        self.assertFalse(env_exists)
        self.assertIn('image_name := env("IMAGE_NAME", "test-image")', justfile)

    def test_write_project_files_restores_missing_env_file_when_justfile_is_present(self) -> None:
        # image-template.env can go missing on its own (e.g. a partial manual
        # edit) while the Justfile stays intact; it must be restored
        # independently of whether the Justfile itself needed restoring.
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            justfile_before = (repo_dir / "Justfile").read_text()
            (repo_dir / "image-template.env").unlink()
            app.write_project_files(repo_dir, include_workflow=True)
            justfile_after = (repo_dir / "Justfile").read_text()
            env_text = (repo_dir / "image-template.env").read_text()
        # Justfile isn't restored (it already existed), but it still goes
        # through patch_container_justfile unconditionally, same as above.
        self.assertEqual(justfile_after, app.patch_container_justfile(justfile_before))
        self.assertIn("IMAGE_NAME=test-image", env_text)
        self.assertIn('REPO_ORGANIZATION="example"', env_text)
        self.assertIn('IMAGE_DESC="Test image"', env_text)

    # ── patch_image_template_env unit tests ──────────────────────────────

    def test_patch_image_template_env_updates_owned_fields(self) -> None:
        app = self.make_app()
        app.config.repo_name = "my-custom-image"
        app.config.github_user = "octocat"
        app.config.image_desc = "My totally custom image"
        existing = textwrap.dedent("""\
            IMAGE_NAME=image-template
            REPO_ORGANIZATION="alice-and-bob"
            IMAGE_DESC="My Customized Bootc Image"
            IMAGE_KEYWORDS="bootc,oci,linux"
        """)
        result = app.patch_image_template_env(existing)
        self.assertIn("IMAGE_NAME=my-custom-image", result)
        self.assertIn('REPO_ORGANIZATION="octocat"', result)
        self.assertIn('IMAGE_DESC="My totally custom image"', result)
        self.assertIn('IMAGE_KEYWORDS="bootc,oci,linux"', result)

    def test_patch_image_template_env_preserves_other_lines(self) -> None:
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "image-template.env").read_text()
        result = app.patch_image_template_env(existing)
        self.assertIn("BIB_IMAGE=", result)
        self.assertIn("# Put your own image here", result)

    def test_patch_image_template_env_sanitizes_dangerous_characters(self) -> None:
        app = self.make_app()
        app.config.image_desc = 'Says "hi" `whoami` $(rm -rf /) \\'
        existing = 'IMAGE_NAME=x\nREPO_ORGANIZATION="x"\nIMAGE_DESC="old"\n'
        result = app.patch_image_template_env(existing)
        self.assertIn('IMAGE_DESC="Says hi whoami (rm -rf /) "', result)
        desc_value = result.splitlines()[2].removeprefix('IMAGE_DESC="').removesuffix('"')
        for char in ('"', "\\", "$", "`"):
            self.assertNotIn(char, desc_value)

    def test_patch_image_template_env_strips_embedded_newlines(self) -> None:
        # A newline embedded in image_desc (reachable via a hand-edited or
        # migrated state file) would otherwise split IMAGE_DESC across two
        # physical lines, and the per-line ^IMAGE_DESC="...$" regex would then
        # silently stop matching on every subsequent patch attempt.
        app = self.make_app()
        app.config.image_desc = "Line one\nLine two\r\nLine three"
        existing = 'IMAGE_NAME=x\nREPO_ORGANIZATION="x"\nIMAGE_DESC="old"\n'
        once = app.patch_image_template_env(existing)
        self.assertEqual(len(once.splitlines()), 3)
        self.assertIn('IMAGE_DESC="Line oneLine twoLine three"', once)
        twice = app.patch_image_template_env(once)
        self.assertEqual(twice, once)

    def test_patch_image_template_env_ensures_trailing_newline(self) -> None:
        app = self.make_app()
        existing = 'IMAGE_NAME=x\nREPO_ORGANIZATION="x"\nIMAGE_DESC="x"'
        result = app.patch_image_template_env(existing)
        self.assertTrue(result.endswith("\n"))
        self.assertFalse(result.endswith("\n\n"))

    # ── patch_container_justfile unit test ──────────────────────────────

    def test_patch_container_justfile_updates_image_name(self) -> None:
        app = self.make_app()
        app.config.repo_name = "my-custom-image"
        existing = textwrap.dedent("""\
            export image_name := env("IMAGE_NAME", "image-template")
            export default_tag := env("DEFAULT_TAG", "latest")

            build:
                podman build .
        """)
        result = app.patch_container_justfile(existing)
        self.assertIn('image_name := env("IMAGE_NAME", "my-custom-image")', result)
        self.assertNotIn("image-template", result)

    def test_patch_container_justfile_preserves_other_content(self) -> None:
        app = self.make_app()
        existing = textwrap.dedent("""\
            export image_name := env("IMAGE_NAME", "old-name")
            export default_tag := env("DEFAULT_TAG", "latest")

            build:
                podman build .

            custom-target:
                echo "custom"
        """)
        result = app.patch_container_justfile(existing)
        self.assertIn("custom-target:", result)
        self.assertIn('echo "custom"', result)

    def test_patch_container_justfile_ensures_trailing_newline(self) -> None:
        app = self.make_app()
        existing = 'export image_name := env("IMAGE_NAME", "old")'
        result = app.patch_container_justfile(existing)
        self.assertTrue(result.endswith("\n"))

    # ── rechunk recipe ARG_MAX fix (chunkah --config file, not env var) ─

    def test_patch_container_rechunk_config_arg_is_noop_for_current_snapshot(self) -> None:
        # The refreshed upstream snapshot already uses a config file, an OCI
        # output directory, and an EXIT trap for both temporary resources.
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        result = app.patch_container_rechunk_config_arg(existing)
        self.assertEqual(existing, result)
        self.assertNotIn("CHUNKAH_CONFIG_STR", result)
        self.assertIn('CHUNKAH_CONFIG_FILE="$(mktemp)"', result)
        self.assertIn('CHUNKAH_OUTPUT_DIR="$(mktemp -d ./aib_chunkah_XXXXXX)"', result)
        self.assertNotIn('mktemp -d ./"${target_image}"_chunkah_', result)
        self.assertIn('trap \'rm -f "${CHUNKAH_CONFIG_FILE}"; rm -rf "${CHUNKAH_OUTPUT_DIR}"\' EXIT', result)
        self.assertIn('src="${target_image}:${tag}"', result)
        # Upstream removed SOURCE_DATE_EPOCH=0 in image-template 94e9423.
        # Pinning it to the epoch clamps file mtimes but also wipes the package
        # stability data chunkah uses to plan layers, which costs users delta
        # update quality (coreos/chunkah#160). Assert it stays gone so a future
        # refresh cannot quietly reintroduce it.
        self.assertNotIn("SOURCE_DATE_EPOCH", result)
        self.assertIn("--output oci:/run/out/chunked", result)
        self.assertIn("podman pull \"oci:${CHUNKAH_OUTPUT_DIR}/chunked\"", result)

    def test_patch_container_rechunk_config_arg_keeps_legacy_justfile_safe(self) -> None:
        app = self.make_app()
        existing = textwrap.dedent(
            '''\
            rechunk $target_image=image_name $tag=default_tag:
                #!/usr/bin/env bash
                set -xeuo pipefail
                export CHUNKAH_CONFIG_STR=$(podman inspect "${target_image}")
                podman run --rm --mount=type=image,src="${target_image}",target=/chunkah \\
                -e CHUNKAH_CONFIG_STR quay.io/coreos/chunkah:latest \\
                build \\
                --verbose \\
                --compressed \\
                --max-layers 128 \\
                --prune /sysroot/ \\
                --label ostree.commit- --label ostree.final-diffid- \\
                --tag "${target_image}:${tag}" | podman load
            '''
        )
        result = app.patch_container_rechunk_config_arg(existing)
        self.assertNotEqual(existing, result)
        self.assertNotIn("-e CHUNKAH_CONFIG_STR", result)
        self.assertIn("CHUNKAH_CONFIG_FILE=$(mktemp)", result)
        # SELinux-enforcing hosts (Fedora/bootc runners) need the mount
        # relabeled or chunkah gets "Permission denied" reading the file —
        # also verified live, not just plausible.
        self.assertIn('/chunkah-config.json:ro,Z"', result)
        self.assertIn("--config /chunkah-config.json", result)
        # Cleanup is via trap, not a bare trailing `rm -f` -- a bare `rm -f`
        # after the podman pipeline only runs on the success path under
        # `set -e`, leaving the temp file behind on failure. The trap
        # guarantees cleanup on every exit path.
        self.assertIn('trap \'rm -f "${CHUNKAH_CONFIG_FILE}"\' EXIT', result)
        self.assertNotIn('podman load\n    rm -f "${CHUNKAH_CONFIG_FILE}"', result)

    def test_patch_container_rechunk_config_arg_repairs_qualified_image_mktemp_template(self) -> None:
        app = self.make_app()
        vulnerable = '    CHUNKAH_OUTPUT_DIR="$(mktemp -d ./"${target_image}"_chunkah_XXXXXX)"\n'
        result = app.patch_container_rechunk_config_arg(vulnerable)
        self.assertEqual(result, '    CHUNKAH_OUTPUT_DIR="$(mktemp -d ./aib_chunkah_XXXXXX)"\n')

    def test_patch_container_rechunk_config_arg_is_idempotent(self) -> None:
        app = self.make_app()
        snapshot_text = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        once = app.patch_container_rechunk_config_arg(snapshot_text)
        twice = app.patch_container_rechunk_config_arg(once)
        self.assertEqual(once, twice)

    @unittest.skipUnless(shutil.which("bash"), "requires a real bash to exercise trap semantics")
    def test_patch_container_rechunk_config_arg_temp_file_survives_failed_run_without_trap(self) -> None:
        # Control case, proving the bug renner0e flagged is real: with a bare
        # trailing `rm -f` (no trap) and `set -e`, a failing `podman run`
        # aborts the script before cleanup ever runs, leaking the temp file.
        leftover = self._run_rechunk_recipe_body(
            recipe_body=(
                'CHUNKAH_CONFIG_FILE=$(mktemp)\n'
                'podman inspect "${target_image}" > "${CHUNKAH_CONFIG_FILE}"\n'
                'podman run --rm quay.io/coreos/chunkah:latest build | podman load\n'
                'rm -f "${CHUNKAH_CONFIG_FILE}"\n'
            ),
        )
        self.assertEqual(len(leftover), 1)

    @unittest.skipUnless(shutil.which("bash"), "requires a real bash to exercise trap semantics")
    def test_patch_container_rechunk_config_arg_cleans_up_temp_file_on_failure(self) -> None:
        # Runs the actual production-generated recipe body (not a hand
        # duplicate) under a real bash, with a podman stub whose `run` call
        # always fails, and asserts the trap still removes the temp file --
        # proving the fix, not just asserting the text contains "trap".
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        patched = app.patch_container_rechunk_config_arg(existing)

        lines = patched.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("rechunk $target_image"))
        body_lines: list[str] = []
        for line in lines[start + 1 :]:
            if line == "" or line.startswith("    "):
                body_lines.append(line[4:] if line.startswith("    ") else line)
            else:
                break
        recipe_body = "\n".join(body_lines)

        leftover = self._run_rechunk_recipe_body(
            recipe_body=recipe_body,
            target_image="ghcr.io/acme/image",
        )
        self.assertEqual(leftover, [])

    def _run_rechunk_recipe_body(self, *, recipe_body: str, target_image: str = "dummy-image") -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            podman_stub = fake_bin / "podman"
            podman_stub.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "inspect" ]; then echo "{}"; exit 0; fi\n'
                'if [ "$1" = "run" ]; then echo "boom" >&2; exit 1; fi\n'
                "exit 0\n"
            )
            podman_stub.chmod(0o755)
            temp_file_dir = tmp_path / "tmpdir"
            temp_file_dir.mkdir()

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["TMPDIR"] = str(temp_file_dir)
            env["target_image"] = target_image
            env["tag"] = "latest"
            script = "set -euo pipefail\n" + recipe_body
            proc = subprocess.run(
                ["bash", "-c", script],
                env=env,
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0, f"expected the recipe body to fail; stderr={proc.stderr!r}")
            leftovers = [f"tmpdir/{item.name}" for item in temp_file_dir.iterdir()]
            leftovers.extend(item.name for item in tmp_path.glob("*_chunkah_*"))
            return leftovers

    def test_patch_container_rechunk_config_arg_no_ops_on_unmatched_text(self) -> None:
        app = self.make_app()
        justfile = textwrap.dedent(
            """\
            build:
                podman build .

            custom-target:
                echo "custom"
            """
        )
        result = app.patch_container_rechunk_config_arg(justfile)
        self.assertEqual(justfile, result)

    def test_patch_container_justfile_also_fixes_rechunk_config_arg(self) -> None:
        # patch_container_justfile is the actual production entry point (used
        # for both new repos and updates to existing ones) — confirm the
        # ARG_MAX fix is wired into it, not just reachable as a standalone
        # method nothing calls.
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
        existing = existing.replace(
            'CHUNKAH_OUTPUT_DIR="$(mktemp -d ./aib_chunkah_XXXXXX)"',
            'CHUNKAH_OUTPUT_DIR="$(mktemp -d ./"${target_image}"_chunkah_XXXXXX)"',
        )
        result = app.patch_container_justfile(existing)
        self.assertNotIn("-e CHUNKAH_CONFIG_STR", result)
        self.assertIn("--config /chunkah-config.json", result)
        self.assertIn('CHUNKAH_OUTPUT_DIR="$(mktemp -d ./aib_chunkah_XXXXXX)"', result)
        self.assertNotIn('mktemp -d ./"${target_image}"_chunkah_', result)

    # ── write_installer_configs missing template error path ────────────

    def test_write_installer_configs_raises_on_missing_template(self) -> None:
        """When the selected installer config doesn't exist in the repo or the
        template snapshot, CommandError is raised."""
        app = self.make_app()
        # Use a base image that maps to KDE profile
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite:stable"
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            disk_dir = repo_dir / "disk_config"
            disk_dir.mkdir()
            # Don't create iso-kde.toml or iso-gnome.toml — simulating missing files
            # The method needs the selected config to exist somewhere
            with patch.object(app, "installer_config_name", return_value="nonexistent.toml"):
                with patch.object(type(CONTAINERFILE_TEMPLATE_DIR / "disk_config" / "nonexistent.toml"), "is_file", return_value=False):
                    with self.assertRaisesRegex(CommandError, "Bundled installer config not found"):
                        app.write_installer_configs(repo_dir)

    def test_patch_installer_config_leaves_a_file_with_no_switch_line_alone(self) -> None:
        # The patcher rewrites the single bootc switch line and stops. A
        # disk_config TOML that has no such line -- a hand-trimmed one, or a
        # newer template shape -- must come back byte-identical rather than
        # gaining anything on the way through.
        app = self.make_app()
        original = "[customizations.installer.kickstart]\ncontents = \"\"\"\ntext --non-interactive\n\"\"\"\n"
        self.assertEqual(app.patch_installer_config(original), original)

    def test_write_installer_configs_skips_when_no_disk_dir(self) -> None:
        """When disk_config/ doesn't exist, write_installer_configs is a no-op."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            # No disk_config/ directory
            app.write_installer_configs(repo_dir)  # Should not raise

    def test_write_installer_configs_falls_back_to_bundled_template(self) -> None:
        """When the selected installer config isn't in the repo yet, it is
        read from the bundled template snapshot rather than raising."""
        app = self.make_app()  # base_image_uri maps to the "kde" profile
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            disk_dir = repo_dir / "disk_config"
            disk_dir.mkdir()
            # iso-kde.toml is absent from the repo but present in the bundled
            # template snapshot.
            app.write_installer_configs(repo_dir)
            iso_text = (disk_dir / "iso.toml").read_text()
        self.assertIn("bootc switch --mutate-in-place --transport registry ghcr.io/example/test-image:latest", iso_text)

    # ── patch_container_disk_workflow unit test ─────────────────────────

    def test_patch_container_disk_workflow_pins_actions(self) -> None:
        """Actions in build-disk.yml are pinned by patch_container_disk_workflow."""
        app = self.make_app()
        # Use realistic step format: name on one line, uses on the next (indented)
        workflow_text = (
            "name: Build disk images\n"
            "on:\n"
            "  pull_request:\n"
            "    branches:\n"
            "      - main\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Checkout\n"
            "        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6\n"
            "      - name: Build disk images\n"
            "        uses: osbuild/bootc-image-builder-action@main\n"
            "        with:\n"
            "          image: ghcr.io/example/test:latest\n"
        )
        result = app.patch_container_disk_workflow(workflow_text)
        # osbuild action should be pinned (the main test target)
        self.assertIn(ACTION_REF_PINS["osbuild/bootc-image-builder-action@main"][0], result)
        self.assertNotIn("osbuild/bootc-image-builder-action@main", result)
        # actions/checkout was already pinned, should stay pinned
        self.assertIn(ACTION_PINS["actions/checkout"][0], result)

    def test_patch_container_disk_workflow_patches_branch_filters(self) -> None:
        """Branch filters are updated to the specified default branch."""
        app = self.make_app()
        workflow_text = (
            "name: Build Disk\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "  pull_request:\n"
            "    branches:\n"
            "      - main\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Checkout\n"
            "        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6\n"
        )
        result = app.patch_container_disk_workflow(workflow_text, default_branch="develop")
        self.assertIn("- develop", result)

    # ── search_packages deselection-only path ──────────────────────────

    def test_search_packages_deselection_only_when_all_already_selected(self) -> None:
        """When all search results are already in config.packages and the user
        deselects some, only removals happen (no add_packages_to_config call)."""
        app = self.make_app()
        app.config.packages = ["fish", "tmux"]
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"
        # User deselects "fish" but keeps "tmux" (tmux wasn't in results though)
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with patch.object(
            app, "search_host_packages",
            return_value=([("fish", "Friendly shell")], False, None)
        ):
            with patch.object(app, "add_packages_to_config") as add_mock:
                app.search_packages()
        # fish was deselected
        self.assertNotIn("fish", app.config.packages)
        # tmux was not in search results so it's untouched
        self.assertIn("tmux", app.config.packages)
        # add_packages_to_config should not have been called (no new packages)
        add_mock.assert_not_called()
        self.assertEqual(stub.prompts, ["Removed 1 package(s). Press Enter to return to the package menu..."])

    def test_search_packages_no_changes_message(self) -> None:
        """When no packages are added or removed, the 'no changes' message shows."""
        app = self.make_app()
        app.config.packages = ["fish"]
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"
        # User picks the same packages already selected
        stub.choose = lambda _options, **_kwargs: ["fish"]
        app.gum = stub
        with patch.object(
            app, "search_host_packages",
            return_value=([("fish", "Friendly shell")], False, None)
        ):
            with patch.object(app, "add_packages_to_config", return_value=False):
                app.search_packages()
        self.assertEqual(stub.prompts, ["No package changes were made. Press Enter to return to the package menu..."])

    def test_ansi_color_code_returns_none_for_none_and_bool(self) -> None:
        gum = Gum()
        self.assertIsNone(gum.ansi_color_code(None, background=False))
        self.assertIsNone(gum.ansi_color_code(True, background=False))
        self.assertIsNone(gum.ansi_color_code(False, background=True))

    def test_ansi_color_code_formats_int_as_256_color(self) -> None:
        gum = Gum()
        self.assertEqual(gum.ansi_color_code(117, background=False), "38;5;117")
        self.assertEqual(gum.ansi_color_code(117, background=True), "48;5;117")

    def test_ansi_color_code_formats_numeric_string_and_strips_whitespace(self) -> None:
        gum = Gum()
        self.assertEqual(gum.ansi_color_code(" 214 ", background=False), "38;5;214")
        self.assertEqual(gum.ansi_color_code("9", background=True), "48;5;9")

    def test_ansi_color_code_returns_none_for_non_numeric_or_empty_string(self) -> None:
        gum = Gum()
        self.assertIsNone(gum.ansi_color_code("red", background=False))
        self.assertIsNone(gum.ansi_color_code("", background=False))
        self.assertIsNone(gum.ansi_color_code("   ", background=False))

    def test_apply_ansi_fallback_returns_plain_text_when_not_a_tty(self) -> None:
        gum = Gum()
        with patch("sys.stdout.isatty", return_value=False):
            with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                self.assertEqual(gum.apply_ansi_fallback("hello", bold=True), "hello")

    def test_apply_ansi_fallback_returns_plain_text_without_term_env(self) -> None:
        gum = Gum()
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(gum.apply_ansi_fallback("hello", bold=True), "hello")

    def test_apply_ansi_fallback_returns_plain_text_when_no_style_opts_given(self) -> None:
        gum = Gum()
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                self.assertEqual(gum.apply_ansi_fallback("hello", foreground=None), "hello")

    def test_apply_ansi_fallback_wraps_text_with_combined_style_codes(self) -> None:
        gum = Gum()
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                result = gum.apply_ansi_fallback(
                    "hello",
                    bold=True,
                    faint=True,
                    italic=True,
                    underline=True,
                    strikethrough=True,
                    foreground=117,
                    background=234,
                )
        self.assertEqual(result, "\x1b[1;2;3;4;9;38;5;117;48;5;234mhello\x1b[0m")

    def test_apply_ansi_fallback_ignores_unknown_and_falsy_opts(self) -> None:
        gum = Gum()
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                result = gum.apply_ansi_fallback("hello", bold=False, align="center", width=40)
        self.assertEqual(result, "hello")

    # ── Gum terminal sizing and widget plumbing ─────────────────────────

    def test_terminal_width_reads_shutil_terminal_size_columns(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.shutil.get_terminal_size", return_value=os.terminal_size((132, 40))):
            self.assertEqual(gum.terminal_width(), 132)

    def test_content_width_clamps_between_min_and_max(self) -> None:
        gum = Gum()
        with patch.object(Gum, "terminal_width", return_value=200):
            self.assertEqual(gum.content_width(max_width=120, min_width=40, reserve=4), 120)
        with patch.object(Gum, "terminal_width", return_value=10):
            self.assertEqual(gum.content_width(max_width=120, min_width=40, reserve=4), 40)

    def test_form_width_clamps_between_min_and_max(self) -> None:
        gum = Gum()
        with patch.object(Gum, "terminal_width", return_value=200):
            self.assertEqual(gum.form_width(max_width=96, min_width=40, reserve=6), 96)
        with patch.object(Gum, "terminal_width", return_value=10):
            self.assertEqual(gum.form_width(max_width=96, min_width=40, reserve=6), 40)

    def test_table_widths_reserves_left_column_and_floors_right_column(self) -> None:
        gum = Gum()
        with patch.object(Gum, "content_width", return_value=80):
            self.assertEqual(gum.table_widths(50, min_right=24), "50,26")
            self.assertEqual(gum.table_widths(70, min_right=24), "70,24")

    def test_clear_runs_clear_command_only_when_interactive_tty(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run") as run_mock:
            with patch("sys.stdout.isatty", return_value=True):
                with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                    gum.clear()
        run_mock.assert_called_once_with(["clear"], capture=False, check=False)

    def test_clear_does_nothing_when_not_a_tty(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.run") as run_mock:
            with patch("sys.stdout.isatty", return_value=False):
                with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                    gum.clear()
        run_mock.assert_not_called()

    def test_interactive_stdout_pipes_stdin_and_captures_stdout_only(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 0, "answer\n", "")
        with patch("atomic_image_builder.subprocess.run", return_value=completed) as run_mock:
            result = gum.interactive_stdout(["gum", "input"], stdin="typed text")
        self.assertEqual(result.stdout, "answer\n")
        run_mock.assert_called_once_with(
            ["gum", "input"],
            input="typed text",
            text=True,
            stdout=subprocess.PIPE,
            stderr=None,
            check=False,
        )

    def test_ensure_available_raises_system_exit_when_gum_missing(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.command_exists", return_value=False):
            with self.assertRaisesRegex(SystemExit, "gum is required"):
                gum.ensure_available()

    def test_ensure_available_is_a_noop_when_gum_present(self) -> None:
        gum = Gum()
        with patch("atomic_image_builder.command_exists", return_value=True):
            gum.ensure_available()

    def test_style_applies_ansi_fallback_to_output_without_ansi_codes(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "style"], 0, "plain output\n", "")
        with patch("atomic_image_builder.run", return_value=completed):
            with patch.object(Gum, "apply_ansi_fallback", return_value="styled") as fallback_mock:
                result = gum.style("plain output", bold=True)
        fallback_mock.assert_called_once_with("plain output", bold=True)
        self.assertEqual(result, "styled")

    def test_style_skips_ansi_fallback_when_output_already_has_ansi_codes(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "style"], 0, "\x1b[1mstyled\x1b[0m\n", "")
        with patch("atomic_image_builder.run", return_value=completed):
            with patch.object(Gum, "apply_ansi_fallback") as fallback_mock:
                result = gum.style("styled")
        fallback_mock.assert_not_called()
        self.assertEqual(result, "\x1b[1mstyled\x1b[0m")

    def test_success_logs_at_info_level(self) -> None:
        gum = Gum()
        with patch.object(Gum, "log") as log_mock:
            gum.success("all done")
        log_mock.assert_called_once_with("info", "all done")

    def test_warn_logs_at_warn_level(self) -> None:
        gum = Gum()
        with patch.object(Gum, "log") as log_mock:
            gum.warn("careful now")
        log_mock.assert_called_once_with("warn", "careful now")

    def test_hint_prints_styled_message_at_content_width(self) -> None:
        gum = Gum()
        with patch.object(Gum, "content_width", return_value=60):
            with patch.object(Gum, "style", return_value="styled hint") as style_mock:
                with redirect_stdout(io.StringIO()) as out:
                    gum.hint("a helpful hint")
        style_mock.assert_called_once_with("a helpful hint", width=60)
        self.assertEqual(out.getvalue(), "styled hint\n")

    def test_instruction_prints_styled_message_with_accent_and_bold(self) -> None:
        gum = Gum()
        with patch.object(Gum, "content_width", return_value=60):
            with patch.object(Gum, "style", return_value="styled instruction") as style_mock:
                with redirect_stdout(io.StringIO()) as out:
                    gum.instruction("do this next")
        style_mock.assert_called_once_with("do this next", foreground=ACCENT_COLOR, bold=True, width=60)
        self.assertEqual(out.getvalue(), "styled instruction\n")

    def test_controls_prints_pipe_joined_parts_after_styled_keys_label(self) -> None:
        gum = Gum()
        with patch.object(Gum, "style", return_value="Keys:") as style_mock:
            with redirect_stdout(io.StringIO()) as out:
                gum.controls("enter: select", "esc: back")
        style_mock.assert_called_once_with("Keys:", foreground=CONTROLS_COLOR, bold=True)
        self.assertEqual(out.getvalue(), "Keys: enter: select | esc: back\n\n")

    def test_input_passes_value_placeholder_and_width_flags_through(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 0, "typed\n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed) as interactive_mock:
            result = gum.input(prompt="Name: ", value="preset", placeholder="e.g. my-image", width=50)
        self.assertEqual(result, "typed")
        args = interactive_mock.call_args[0][0]
        self.assertIn("--value", args)
        self.assertEqual(args[args.index("--value") + 1], "preset")
        self.assertIn("--placeholder", args)
        self.assertEqual(args[args.index("--placeholder") + 1], "e.g. my-image")
        self.assertIn("--placeholder.foreground", args)
        self.assertIn("--width", args)
        self.assertEqual(args[args.index("--width") + 1], "50")

    def test_write_passes_placeholder_height_and_width_and_strips_trailing_newline(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "write"], 0, "typed text\n", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed) as interactive_mock:
            result = gum.write(placeholder="Describe your image", height=6, width=60)
        self.assertEqual(result, "typed text")
        args = interactive_mock.call_args[0][0]
        self.assertIn("--placeholder", args)
        self.assertEqual(args[args.index("--placeholder") + 1], "Describe your image")
        self.assertIn("--height", args)
        self.assertEqual(args[args.index("--height") + 1], "6")
        self.assertIn("--width", args)
        self.assertEqual(args[args.index("--width") + 1], "60")

    # ── select_packages wizard-step dispatch ───────────────────────────

    def pick_from_menu(self, stub: "GumStub", labels: list[str]):
        """Answer gum.choose with `labels`, one per call, checking each is real.

        A stub that returns hard-coded labels regardless of what was displayed
        would keep passing if a menu label were renamed while its if/elif arm
        kept the old spelling — exactly the break these dispatch tests exist to
        catch. Asserting the label is in the options passed to choose() keeps
        the test coupled to the menu the user actually sees.
        """
        remaining = iter(labels)

        def choose(options, **_kwargs) -> list[str]:
            label = next(remaining)
            self.assertIn(label, list(options))
            return [label]

        stub.choose = choose
        return stub

    def test_select_packages_dispatches_each_editing_task(self) -> None:
        # The wizard's Software step is a plain if/elif chain keyed on the
        # exact menu labels passed to gum.choose a few lines above. A typo or
        # a reordering would silently route the user to the wrong editor, so
        # walk every arm once and assert it reached the right handler.
        app = self.make_app()
        stub = self.pick_from_menu(
            GumStub(),
            [
                "Search package names",
                "Type exact package names",
                "Add a COPR repository",
                "Add systemd services to enable",
                "Removed base packages",
                "Review current selections",
                "Continue to review",
            ],
        )
        app.gum = stub
        with patch.object(app, "search_packages") as search:
            with patch.object(app, "manual_packages") as manual:
                with patch.object(app, "add_copr") as copr:
                    with patch.object(app, "add_services") as services:
                        with patch.object(app, "manage_removed_packages") as removed:
                            with patch.object(app, "view_selections") as review:
                                with redirect_stdout(io.StringIO()):
                                    app.select_packages()

        search.assert_called_once()
        manual.assert_called_once()
        copr.assert_called_once()
        services.assert_called_once()
        removed.assert_called_once_with(return_to="package menu")
        review.assert_called_once()

    def test_select_packages_inner_escape_returns_to_the_step_menu(self) -> None:
        # Esc inside an editing task means "back to the Software menu", not
        # "abandon the wizard step" — the ScreenBack must not escape.
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Search package names", "Continue to review"])
        app.gum = stub
        with patch.object(app, "search_packages", side_effect=ScreenBack()) as search:
            with redirect_stdout(io.StringIO()):
                app.select_packages()

        search.assert_called_once()

    # ── search_packages result-screen branches ─────────────────────────

    def test_search_packages_escape_at_results_returns(self) -> None:
        # Esc on the results chooser backs out of search without touching the
        # current selection.
        app = self.make_app()
        app.config.packages = ["fish"]
        stub = GumStub()
        stub.input = lambda **_kwargs: "fish"
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([("fish", "Friendly shell")], False, None)):
            with patch.object(app, "add_packages_to_config") as add_mock:
                with redirect_stdout(io.StringIO()):
                    app.search_packages()

        self.assertEqual(app.config.packages, ["fish"])
        add_mock.assert_not_called()
        self.assertEqual(stub.prompts, [])

    def test_search_packages_reports_added_and_removed_counts(self) -> None:
        # Selecting a new match while deselecting an existing one has to report
        # both halves; the counts come from measuring config.packages around
        # add_packages_to_config, not from the picked list.
        app = self.make_app()
        app.config.packages = ["fish"]
        stub = GumStub()
        stub.input = lambda **_kwargs: "sh"
        stub.choose = lambda _options, **_kwargs: ["tmux"]
        app.gum = stub

        def fake_add(names: list[str], *, source_label: str) -> bool:
            app.config.packages.extend(names)
            return True

        results = [("fish", "Friendly shell"), ("tmux", "Terminal multiplexer")]
        with patch.object(app, "search_host_packages", return_value=(results, False, None)):
            with patch.object(app, "add_packages_to_config", side_effect=fake_add) as add_mock:
                with redirect_stdout(io.StringIO()):
                    app.search_packages()

        add_mock.assert_called_once_with(["tmux"], source_label="search 'sh'")
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(
            stub.prompts,
            ["Added 1 and removed 1 package(s). Press Enter to return to the package menu..."],
        )

    def test_search_packages_reports_added_count_only(self) -> None:
        app = self.make_app()
        app.config.packages = []
        stub = GumStub()
        stub.input = lambda **_kwargs: "tmux"
        stub.choose = lambda _options, **_kwargs: ["tmux"]
        app.gum = stub

        def fake_add(names: list[str], *, source_label: str) -> bool:
            app.config.packages.extend(names)
            return True

        with patch.object(app, "search_host_packages", return_value=([("tmux", "Terminal multiplexer")], False, None)):
            with patch.object(app, "add_packages_to_config", side_effect=fake_add):
                with redirect_stdout(io.StringIO()):
                    app.search_packages()

        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(stub.prompts, ["Added 1 package(s). Press Enter to return to the package menu..."])

    def test_search_packages_empty_term_returns_to_the_package_menu(self) -> None:
        # An empty search term is how the user backs out of the prompt, so it
        # must return before any lookup happens.
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda **_kwargs: "   "
        app.gum = stub
        with patch.object(app, "search_host_packages") as search_mock:
            with redirect_stdout(io.StringIO()):
                app.search_packages()

        search_mock.assert_not_called()
        self.assertEqual(stub.prompts, [])

    def test_search_packages_reports_unavailable_search_and_returns(self) -> None:
        # When search metadata is unavailable the message has to reach the user
        # and the flow has to return rather than loop on a search that cannot
        # work.
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda **_kwargs: "tmux"
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([], False, "dnf5 makecache first")):
            with redirect_stdout(io.StringIO()):
                app.search_packages()

        self.assertIn(("warn", "dnf5 makecache first"), stub.messages)
        self.assertEqual(stub.prompts, ["Press Enter to return to the package menu..."])

    def test_search_packages_no_matches_loops_for_another_term(self) -> None:
        # No matches is not an error: the user gets a hint and another prompt,
        # and only an empty term ends the loop.
        app = self.make_app()
        terms = iter(["doesnotexist", ""])
        stub = GumStub()
        stub.input = lambda **_kwargs: next(terms)
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([], False, None)) as search_mock:
            with redirect_stdout(io.StringIO()):
                app.search_packages()

        search_mock.assert_called_once_with("doesnotexist")
        self.assertIn(("warn", "No package names matched 'doesnotexist'."), stub.messages)
        self.assertEqual(stub.prompts, ["Press Enter to search again..."])

    def test_search_packages_hints_when_results_are_truncated(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.input = lambda **_kwargs: "py"
        stub.choose = lambda _options, **_kwargs: []
        app.gum = stub
        with patch.object(app, "search_host_packages", return_value=([("python3", "")], True, None)):
            with redirect_stdout(io.StringIO()):
                app.search_packages()

        self.assertTrue(
            any(level == "hint" and str(PACKAGE_SEARCH_LIMIT) in message for level, message in stub.messages)
        )

    def test_run_main_returns_after_preflight_when_gum_is_missing(self) -> None:
        # preflight() may report and return rather than exit (for example when
        # it only has warnings). run_main must still stop there instead of
        # falling through into a menu that needs gum.
        app = self.make_app()

        def fake_exists(name: str) -> bool:
            return name != "gum"

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch.object(app, "preflight") as preflight_mock:
                with patch.object(app, "clear") as clear_mock:
                    with patch.object(app, "banner") as banner_mock:
                        with patch.object(app, "startup_requirements") as startup_mock:
                            with patch.object(app, "main_menu") as menu_mock:
                                self.assertIsNone(app.run_main())

        preflight_mock.assert_called_once_with()
        clear_mock.assert_not_called()
        banner_mock.assert_not_called()
        startup_mock.assert_not_called()
        menu_mock.assert_not_called()

    # ── manage_removed_packages entry outcomes ─────────────────────────

    def test_manage_removed_packages_empty_entry_returns(self) -> None:
        # An empty write box is the documented way to back out of the add
        # screen, so it must return before anything is validated or added.
        app = self.make_app()
        app.config.removed_packages = ["vim-enhanced"]
        stub = self.pick_from_menu(GumStub(), ["Add package names to remove"])
        stub.write = lambda **_kwargs: "   \n  "
        app.gum = stub
        with patch.object(app, "add_removed_packages_to_config") as add_mock:
            with redirect_stdout(io.StringIO()):
                app.manage_removed_packages()

        add_mock.assert_not_called()
        self.assertEqual(app.config.removed_packages, ["vim-enhanced"])
        self.assertEqual(stub.prompts, [])

    def test_manage_removed_packages_reports_partially_checked_entry(self) -> None:
        # Some names added, others unresolvable on this host: the closing
        # message must not claim a clean count, because the per-package
        # warnings the user just saw are the real result.
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Add package names to remove"])
        stub.write = lambda **_kwargs: "vim-enhanced nosuchpkg"
        app.gum = stub

        def fake_add(packages: list[str], *, source_label: str) -> bool:
            app.config.removed_packages.append("vim-enhanced")
            app.last_manual_removed_package_check_had_missing = True
            return True

        with patch.object(app, "add_removed_packages_to_config", side_effect=fake_add) as add_mock:
            with redirect_stdout(io.StringIO()):
                app.manage_removed_packages(return_to="package menu")

        # The "manual entry" label is what gates
        # filter_available_manual_removed_packages inside the adder. Any other
        # label would let unresolved removals through, so the fake must not be
        # allowed to stand in for a call that never passed it.
        add_mock.assert_called_once_with(["vim-enhanced", "nosuchpkg"], source_label="manual entry")
        self.assertEqual(
            stub.prompts,
            ["Finished checking package removals. Press Enter to return to the package menu..."],
        )

    def test_manage_removed_packages_reports_nothing_added(self) -> None:
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Add package names to remove"])
        stub.write = lambda **_kwargs: "nosuchpkg"
        app.gum = stub
        with patch.object(app, "add_removed_packages_to_config", return_value=False) as add_mock:
            with redirect_stdout(io.StringIO()):
                app.manage_removed_packages()

        add_mock.assert_called_once_with(["nosuchpkg"], source_label="manual entry")
        self.assertEqual(app.config.removed_packages, [])
        self.assertEqual(
            stub.prompts,
            ["No package removals were added. Press Enter to return to the update menu..."],
        )

    # ── manage_copr_repos escape handling ──────────────────────────────

    def test_manage_copr_repos_escape_at_menu_returns(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["owner/project"]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.manage_copr_repos()  # must return, not raise

        self.assertEqual(app.config.copr_repos, ["owner/project"])

    def test_manage_copr_repos_inner_escape_returns_to_menu(self) -> None:
        # Esc inside the add screen means "back to the COPR menu", not "leave
        # the COPR menu", so the loop has to redraw rather than unwind.
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Add a COPR repository", "Back"])
        app.gum = stub
        with patch.object(app, "add_copr", side_effect=ScreenBack()) as add_mock:
            with redirect_stdout(io.StringIO()):
                app.manage_copr_repos()

        add_mock.assert_called_once()

    # ── empty-candidate guards on the config adders ────────────────────

    def test_add_packages_to_config_rejects_empty_candidates(self) -> None:
        # Both adders are reached from several entry points; an empty list has
        # to be refused before validation, or a "Added 0 package(s)" success
        # message would be reported for doing nothing.
        app = self.make_app()
        app.gum = GumStub()
        self.assertFalse(app.add_packages_to_config([], source_label="manual entry"))
        self.assertEqual(app.config.packages, [])

    def test_add_removed_packages_to_config_rejects_empty_candidates(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        self.assertFalse(app.add_removed_packages_to_config([], source_label="manual entry"))
        self.assertEqual(app.config.removed_packages, [])

    # ── search_host_packages preconditions and failure reporting ───────

    def test_search_host_packages_blank_term_returns_no_results(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with patch("atomic_image_builder.command_exists") as exists_mock:
            results, truncated, message = app.search_host_packages("   ")

        self.assertEqual((results, truncated, message), ([], False, None))
        exists_mock.assert_not_called()

    def test_search_host_packages_without_dnf5_reports_unavailable(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", return_value=False):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertEqual(message, "dnf5 is not installed, so package search is unavailable on this system.")

    def test_search_host_packages_reports_unrecognized_failure(self) -> None:
        # A nonzero exit that matches neither the missing-cache nor the
        # no-matches shape is a real failure, and must be reported as one
        # rather than silently looking like an empty result set.
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            ["dnf5", "repoquery"], 1, "", "Error: something else went wrong"
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertEqual(message, "Package search is unavailable right now. Use exact-name entry instead.")

    def test_search_host_packages_skips_blank_output_lines(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            ["dnf5", "repoquery"], 0, "tmux\tMultiplexer\n\n   \nhtop\tViewer\n", ""
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("t")

        self.assertIsNone(message)
        self.assertFalse(truncated)
        self.assertEqual(results, [("tmux", "Multiplexer"), ("htop", "Viewer")])

    # ── select_packages as a numbered wizard step ──────────────────────

    def test_select_packages_shows_step_header_inside_the_wizard(self) -> None:
        # The same method is both a wizard step and a standalone menu. Only the
        # wizard call renders the "Step N of M" header.
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Continue to review"])
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.select_packages(step=4, total_steps=6)

        self.assertIn(("hint", "Step 4 of 6."), stub.messages)

    def test_select_packages_omits_step_header_outside_the_wizard(self) -> None:
        app = self.make_app()
        stub = self.pick_from_menu(GumStub(), ["Continue to review"])
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.select_packages()

        self.assertFalse(any(message.startswith("Step ") for _level, message in stub.messages))

    def test_clear_delegates_to_gum(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        app.clear()
        self.assertEqual(stub.clear_calls, 1)

    def test_show_step_header_prints_next_hint_before_the_step_count(self) -> None:
        # next_hint is optional context about what the step is for; when present
        # it must read before the generic "Step N of M." so the hint isn't lost
        # below the boilerplate.
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.show_step_header("Choose a base image", step=2, total_steps=5, next_hint="Pick the OS you want to build on.")

        hints = [message for level, message in stub.messages if level == "hint"]
        self.assertEqual(hints, ["Pick the OS you want to build on.", "Step 2 of 5."])

    def test_show_step_header_omits_hint_line_when_no_next_hint_given(self) -> None:
        app = self.make_app()
        stub = GumStub()
        app.gum = stub
        with redirect_stdout(io.StringIO()):
            app.show_step_header("Choose a base image", step=2, total_steps=5)

        hints = [message for level, message in stub.messages if level == "hint"]
        self.assertEqual(hints, ["Step 2 of 5."])

    def test_show_managed_repo_warning_hints_bluebuild_docs_for_bluebuild_method(self) -> None:
        app = self.make_bluebuild_app()
        stub = GumStub()
        app.gum = stub
        app.show_managed_repo_warning()

        hints = [message for level, message in stub.messages if level == "hint"]
        self.assertEqual(hints, [MANAGED_REPO_HINT_BLUEBUILD])

    def test_create_new_image_scanned_reuses_the_detected_github_user(self) -> None:
        # scanned=True is the "resume after a repo scan" entry point: it must
        # carry the already-detected github_user into the fresh wizard config
        # instead of dropping it, which is why the assignment happens before
        # fresh_config() would otherwise wipe it.
        app = self.make_app()
        app.github_user = "detected-user"
        with patch.object(app, "choose_method", side_effect=ScreenBack):
            app.create_new_image(scanned=True)

        self.assertEqual(app.config.github_user, "detected-user")

    # ── menu summary formatting helpers ────────────────────────────────

    def test_truncate_label_collapses_whitespace_and_elides(self) -> None:
        app = self.make_app()
        self.assertEqual(app.truncate_label("  tmux   terminal  ", limit=36), "tmux terminal")
        self.assertEqual(app.truncate_label("abcdefghij", limit=8), "abcde...")
        self.assertEqual(len(app.truncate_label("abcdefghij", limit=8)), 8)

    def test_preview_values_is_empty_for_no_values(self) -> None:
        app = self.make_app()
        self.assertEqual(app.preview_values([]), "")

    def test_preview_values_counts_the_overflow(self) -> None:
        app = self.make_app()
        self.assertEqual(app.preview_values(["tmux", "htop", "fish", "vim"], limit=2), "tmux, htop, 2 more")

    def test_summarize_selection_prefixes_a_count_past_the_limit(self) -> None:
        app = self.make_app()
        self.assertEqual(
            app.summarize_selection(["tmux", "htop", "fish"], empty="No packages yet", verb="packages", limit=2),
            "3 packages: tmux, htop, 1 more",
        )
        self.assertEqual(
            app.summarize_selection(["tmux", "htop"], empty="No packages yet", verb="packages", limit=2),
            "tmux, htop",
        )
        self.assertEqual(
            app.summarize_selection([], empty="No packages yet", verb="packages"),
            "No packages yet",
        )

    def test_software_status_counts_each_kind_of_change(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        app.config.packages = ["tmux", "htop"]
        app.config.copr_repos = ["owner/project"]
        app.config.services = ["sshd"]
        app.config.removed_packages = ["nano"]
        self.assertEqual(app.software_status(), "2 pkg, 1 COPR, 1 svc, 1 removed")

    def test_software_status_reports_an_untouched_config(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        self.assertEqual(app.software_status(), "No software changes yet")


if __name__ == "__main__":
    unittest.main()

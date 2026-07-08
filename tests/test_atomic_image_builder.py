import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import atomic_image_builder
from atomic_image_builder import (
    ACTION_PINS,
    ACTION_REF_PINS,
    BASE_IMAGES,
    BLUEBUILD_RECIPE_SCHEMA,
    COMMON_SERVICES,
    CONTAINERFILE_TEMPLATE_DIR,
    DEFAULT_GITHUB_BUILD_CRON,
    FEDORA_ATOMIC_DEFAULT_TAG,
    FEDORA_ATOMIC_FALLBACK_TAG,
    MANAGED_REPO_WARNING,
    METHOD_DISPLAY,
    PACKAGE_SEARCH_LIMIT,
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
    format_daily_rebuild_note,
    normalize_container_image_reference,
)


class GumStub:
    """Shared test double for Gum — override only what you need per test."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.prompts: list[str] = []

    def header(self, *_args, **_kwargs) -> None:
        pass

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

    def test_normalize_container_image_reference_handles_remote_registry_prefix(self) -> None:
        self.assertEqual(
            normalize_container_image_reference("ostree-remote-registry:fedora:quay.io/fedora-ostree-desktops/kinoite:43"),
            "quay.io/fedora-ostree-desktops/kinoite:43",
        )

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

    def test_patch_container_workflow_golden(self) -> None:
        expected_path = Path(__file__).parent / "fixtures/workflows/container_expected.yml"
        input_path = Path(__file__).parent / "fixtures/workflows/container_input.yml"
        app = self.make_app()
        result = app.patch_container_workflow(input_path.read_text(), default_branch="main")
        self.assertEqual(result, expected_path.read_text())

    def test_patch_container_workflow_matches_current_upstream_snapshot_shape(self) -> None:
        # Coverage for the "rewrite in just" upstream refresh (image-template @
        # f9a9e4f8): the current build.yml has no job-level env: block and no
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
        # commented-out Chunkah alternative block in the snapshot is untouched.
        self.assertIn("- name: Rechunk with Chunkah", result)
        self.assertIn("command -v just) rechunk", result)
        self.assertNotIn("- name: Rechunk with rpm-ostree", result)
        self.assertNotIn("command -v just) ostree-rechunk", result)
        self.assertIn("#- name: Rechunk with Chunkah", result)

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

    def test_determine_fedora_atomic_default_tag_keeps_fallback_for_non_fedora_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text('ID="ubuntu"\nVERSION_ID="24.04"\n')
            self.assertEqual(determine_fedora_atomic_default_tag(os_release_path=os_release), FEDORA_ATOMIC_FALLBACK_TAG)

    def test_validate_config_rejects_unsupported_base_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite-deck:stable"
        app.config.base_image_name = "Bazzite Deck"
        with self.assertRaisesRegex(CommandError, "supported base images"):
            app.validate_config()

    def test_validate_config_rejects_invalid_repo_name(self) -> None:
        app = self.make_app()
        app.config.repo_name = ".git"
        with self.assertRaisesRegex(CommandError, "Repository name is invalid"):
            app.validate_config()

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
                        with patch("atomic_image_builder.shutil.copy2", side_effect=OSError("disk full")):
                            app.rotate_signing_key(repo_dir)

        self.assertTrue(any(level == "warn" and "half-complete" in message for level, message in app.gum.messages))
        self.assertFalse(any(call[:2] == ["git", "push"] for call in calls))

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

    def test_add_packages_to_config_accepts_valid_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", side_effect=[True, True]):
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
        with patch.object(app, "lookup_host_package", return_value=False):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.packages, [])
        self.assertTrue(any(level == "error" and "not found" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_keeps_checked_manual_packages_only(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", side_effect=[True, False]):
            added = app.add_packages_to_config(["tmux", "nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "error" and "nethock" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_warns_when_manual_check_is_unavailable(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=None):
            added = app.add_packages_to_config(["tmux"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "warn" for level, _message in app.gum.messages))

    def test_add_packages_to_config_keeps_missing_manual_packages_when_copr_is_configured(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=False):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["nethock"])
        self.assertTrue(any(level == "warn" and "host repos" in message for level, message in app.gum.messages))

    def test_add_removed_packages_to_config_accepts_valid_tokens(self) -> None:
        app = self.make_app()
        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", side_effect=[True, True]):
            added = app.add_removed_packages_to_config(["vim-enhanced", "nano"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.removed_packages, ["vim-enhanced", "nano"])
        self.assertTrue(any(level == "success" for level, _message in app.gum.messages))

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
        with patch.object(app, "lookup_host_package", return_value=False):
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

    def test_search_host_packages_reports_missing_cache(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.spinner_result = lambda _title, _command, *, cwd=None: subprocess.CompletedProcess(
            ["dnf5", "repoquery"],
            1,
            "",
            'Cache-only enabled but no cache for repository "fedora"',
        )
        app.gum = stub
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertIn("dnf5 makecache", message or "")

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
        with patch.object(app, "lookup_host_package", return_value=True):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(app.gum.prompts, ["Added 1 package(s). Press Enter to return to the package menu..."])

    def test_manual_packages_pauses_after_failed_add(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "nethock"
        app.gum = stub
        with patch.object(app, "lookup_host_package", return_value=False):
            app.manual_packages()
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, ["No packages were added. Press Enter to return to the package menu..."])

    def test_manual_packages_accepts_comma_separated_entry(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.write = lambda **_kwargs: "tmux,htop, vim"
        app.gum = stub
        with patch.object(app, "lookup_host_package", return_value=True):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux", "htop", "vim"])

    def test_select_common_services_replaces_curated_selection_only(self) -> None:
        app = self.make_app()
        app.config.services = ["custom.service", COMMON_SERVICES[0][1]]
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: [f"{COMMON_SERVICES[1][0]} ({COMMON_SERVICES[1][1]})"]
        app.gum = stub
        app.select_common_services()
        self.assertEqual(app.config.services, ["custom.service", COMMON_SERVICES[1][1]])

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
            if list(args) == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
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

    def test_main_menu_recovers_from_command_error(self) -> None:
        # A CommandError raised by any dispatched action must be reported and
        # return to the main menu instead of propagating out of the app.
        app = self.make_app()
        stub = GumStub()
        choices = ["Create New Image", "Quit"]
        stub.choose = lambda _options, **_kwargs: [choices.pop(0)]
        app.gum = stub
        with patch.object(app, "create_new_image", side_effect=CommandError("menu boom")):
            with self.assertRaises(SystemExit):
                app.main_menu()

        self.assertIn(("error", "menu boom"), stub.messages)
        self.assertTrue(stub.prompts)

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

        self.assertTrue(result)
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

        self.assertTrue(result)
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:testing")
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")
        # Verify the warning was shown
        self.assertTrue(any("testing" in msg and "stable" in msg for _, msg in gum.messages))

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

        self.assertFalse(result)
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

        self.assertFalse(result)
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

        self.assertTrue(result)
        self.assertEqual(app.config.base_image_uri, "quay.io/fedora-ostree-desktops/kinoite:43")
        self.assertEqual(app.config.base_image_name, "Fedora Kinoite")

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

        self.assertTrue(result)
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

        self.assertFalse(result)
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

        self.assertFalse(result)
        self.assertTrue(
            any(level == "error" and "rpm-ostree" in message for level, message in stub.messages)
        )

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

        self.assertFalse(result)
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

    def test_write_project_files_updates_template_workflow_branch_filters(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            app.clone_container_template(repo_dir)
            app.write_project_files(repo_dir, include_workflow=True, default_branch="master")
            workflow = (repo_dir / ".github/workflows/build.yml").read_text()
        self.assertIn("  pull_request:\n    branches:\n      - master", workflow)
        self.assertIn("  push:\n    branches:\n      - master", workflow)

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
        self.assertIn("          cosign-release: 'v2.6.3'", workflow)

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

    def test_split_image_ref_does_not_split_digest_pinned_ref(self) -> None:
        # A digest ref has no tag; its colon belongs to the digest and must not
        # be treated as the image-version separator.
        app = self.make_app()
        digest = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        self.assertEqual(app._split_image_ref(digest), (digest, "latest"))

    def test_generate_recipe_keeps_digest_pinned_base_image_whole(self) -> None:
        app = self.make_bluebuild_app()
        digest = "ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64
        app.config.base_image_uri = digest
        recipe = app.generate_recipe()
        self.assertIn(f"base-image: {digest}", recipe)
        self.assertIn('image-version: "latest"', recipe)

    def test_generate_recipe_basic(self) -> None:
        app = self.make_bluebuild_app()
        recipe = app.generate_recipe()
        self.assertIn(f"$schema={BLUEBUILD_RECIPE_SCHEMA}", recipe)
        self.assertIn("name: test-bb-image", recipe)
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
        self.assertIn("        - htop", recipe)
        self.assertIn("        - tmux", recipe)

    def test_generate_recipe_includes_copr_repos(self) -> None:
        app = self.make_bluebuild_app()
        app.config.copr_repos = ["kylegospo/bazzite"]
        recipe = app.generate_recipe()
        self.assertIn("copr:", recipe)
        self.assertIn("        - kylegospo/bazzite", recipe)

    def test_generate_recipe_includes_removed_packages(self) -> None:
        app = self.make_bluebuild_app()
        app.config.removed_packages = ["firefox"]
        recipe = app.generate_recipe()
        self.assertIn("remove:", recipe)
        self.assertIn("        - firefox", recipe)

    def test_generate_recipe_includes_services(self) -> None:
        app = self.make_bluebuild_app()
        app.config.services = ["tailscaled.service"]
        recipe = app.generate_recipe()
        self.assertIn("- type: systemd", recipe)
        self.assertIn("        - tailscaled.service", recipe)

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
            self.assertIn("name: test-bb-image", recipe)
            self.assertIn("- type: dnf", recipe)
            self.assertIn("        - htop", recipe)

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
        with patch.object(app, "lookup_host_package", return_value=True):
            app.manage_removed_packages()
        self.assertIn("vim-enhanced", app.config.removed_packages)

    def test_manage_removed_packages_add_flow_accepts_comma_separated_entry(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: ["Add package names to remove"]
        stub.write = lambda **_kwargs: "vim-enhanced,nano"
        app.gum = stub
        with patch.object(app, "lookup_host_package", return_value=True):
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
        with patch.object(app, "lookup_host_package", return_value=True):
            app.add_copr()
        self.assertEqual(app.config.copr_repos, ["kwizart/fedy"])
        self.assertEqual(app.config.packages, ["tmux", "htop"])

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

    def test_update_menu_esc_returns_false(self) -> None:
        """ScreenBack from choose returns False (cancel)."""
        app = self.make_app()
        stub = GumStub()
        stub.choose = lambda _options, **_kwargs: (_ for _ in ()).throw(ScreenBack())
        app.gum = stub
        self.assertFalse(app.update_menu())

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

    def test_patch_container_rechunk_config_arg_fixes_real_snapshot(self) -> None:
        # The bundled Justfile's rechunk recipe exports the full `podman
        # inspect` output as an env var, which exceeds the kernel's
        # argument/environment size limit for already-chunked base images
        # (i.e. most Universal Blue images) and crashes every subsequent
        # podman call in that shell with "Argument list too long". Verified
        # live against a real ghcr.io/ublue-os/bluefin:stable pull: the
        # unpatched recipe reproduces that exact failure, and this patched
        # recipe completes a real chunkah build against the same image.
        app = self.make_app()
        existing = (CONTAINERFILE_TEMPLATE_DIR / "Justfile").read_text()
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

        leftover = self._run_rechunk_recipe_body(recipe_body=recipe_body)
        self.assertEqual(leftover, [])

    def _run_rechunk_recipe_body(self, *, recipe_body: str) -> list[str]:
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
            env["target_image"] = "dummy-image"
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
            return [item.name for item in temp_file_dir.iterdir()]

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
        result = app.patch_container_justfile(existing)
        self.assertNotIn("-e CHUNKAH_CONFIG_STR", result)
        self.assertIn("--config /chunkah-config.json", result)

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

    def test_write_installer_configs_skips_when_no_disk_dir(self) -> None:
        """When disk_config/ doesn't exist, write_installer_configs is a no-op."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            # No disk_config/ directory
            app.write_installer_configs(repo_dir)  # Should not raise

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


if __name__ == "__main__":
    unittest.main()

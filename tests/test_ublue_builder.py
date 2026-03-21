import io
import json
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from atomic_image_builder import (
    ACTION_PINS,
    App,
    BASE_IMAGES,
    COMMON_SERVICES,
    CommandError,
    CONTAINERFILE_TEMPLATE_DIR,
    Config,
    FEDORA_ATOMIC_DEFAULT_TAG,
    Gum,
    MANAGED_REPO_WARNING,
    PACKAGE_SEARCH_LIMIT,
    ScreenBack,
    STATE_FILE,
    TOOL_NAME,
    TOOL_SLUG,
    VERSION,
    config_from_state_payload,
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
        self.assertIn(".ublue-builder.json", patched)
        self.assertIn(ACTION_PINS["actions/checkout"][0], patched)
        self.assertIn(ACTION_PINS["ublue-os/remove-unwanted-software"][0], patched)
        self.assertIn(ACTION_PINS["sigstore/cosign-installer"][0], patched)
        self.assertIn("env.COSIGN_PRIVATE_KEY != ''", patched)

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
        self.assertIn("paths-ignore: ['**/README.md', '.ublue-builder.json']", patched)

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

    def test_preflight_requires_github_login(self) -> None:
        app = self.make_app()
        stub = GumStub()
        stub.ensure_available = lambda: None
        app.gum = stub

        def fake_exists(name: str) -> bool:
            return name in {"gum", "git", "gh", "cosign", "dnf5", "rpm-ostree"}

        with patch("atomic_image_builder.command_exists", side_effect=fake_exists):
            with patch("atomic_image_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", "")):
                with self.assertRaises(SystemExit):
                    app.preflight()
        self.assertTrue(any("gh auth login" in message for level, message in app.gum.messages if level == "hint"))
        self.assertEqual(stub.prompts, ["Press Enter to exit to the terminal..."])

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

        with patch("atomic_image_builder.run", side_effect=fake_run):
            with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                with self.assertRaisesRegex(CommandError, "signing failed"):
                    app.do_build()

        self.assertTrue(any("delete_repo scope" in message for level, message in app.gum.messages if level == "hint"))

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

        def fake_choose_base_image(**_kwargs):
            seen["packages"] = list(app.config.packages)
            seen["services"] = list(app.config.services)
            seen["repo_name"] = app.config.repo_name
            seen["github_user"] = app.config.github_user
            raise ScreenBack()

        with patch.object(app, "choose_base_image", side_effect=fake_choose_base_image):
            app.create_new_image()

        self.assertEqual(seen["packages"], [])
        self.assertEqual(seen["services"], [])
        self.assertEqual(seen["repo_name"], "")
        self.assertEqual(seen["github_user"], "example")

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
        app.gum = GumStub()
        with patch("atomic_image_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "atomic_image_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, status_payload, ""),
            ):
                result = app.scan_os()

        self.assertTrue(result)
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:testing")
        self.assertEqual(app.config.base_image_name, "Bazzite (KDE)")

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

    def test_generate_container_workflow_uses_default_branch_and_pins_cosign_release(self) -> None:
        app = self.make_app()
        app.config.signing_enabled = True
        workflow = app.generate_container_workflow(default_branch="master")
        self.assertIn("  pull_request:\n    branches:\n      - master", workflow)
        self.assertIn("  push:\n    branches:\n      - master", workflow)
        self.assertIn("          cosign-release: 'v2.6.1'", workflow)

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
        self.assertIn("      FOO: bar", patched)

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


if __name__ == "__main__":
    unittest.main()

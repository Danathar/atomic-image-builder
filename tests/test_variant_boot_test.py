import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atomic_image_builder import (
    BASE_IMAGES,
    BLUEBUILD_TEMPLATE_DIR,
    CONTAINERFILE_TEMPLATE_DIR,
)
from variant_boot_test import (
    ALLOWED_FAILED_UNITS,
    BLUEBUILD,
    BOOT_TIMEOUT_SECONDS,
    BUILD_TIMEOUT_SECONDS,
    CONTAINERFILE,
    GUEST_SCRIPT_MOUNT,
    HOST_ISSUE_TITLE,
    NIGHTLY_SLOTS,
    ROOTHOME_UNIT,
    WORST_CASE_SLOT_SECONDS,
    Boot,
    Result,
    Slot,
    StageFailure,
    base_by_key,
    base_reference,
    bluebuild_base,
    boot,
    build,
    compare_with_upstream,
    generate,
    guest_script,
    host_failure,
    image_digest,
    issue_title,
    judge,
    load_result,
    main,
    parse_guest_output,
    plan,
    render_issue_body,
    run_slot,
    sync_issue,
    write_units,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The shape gh returns for an issue this workflow opened: GraphQL spells the
# app actor with the "app/" prefix. Spelled out rather than imported so a
# change to the accepted authors has to face a test naming the real value.
BOT_AUTHOR = {"login": "app/github-actions"}
MONDAY = dt.date(2026, 10, 19)


def booted(*, reached=True, state="degraded", checks=(), units=(), console=()) -> Boot:
    return Boot(reached, state, list(checks), list(units), list(console), "guest output")


def passing_checks() -> list[dict[str, str]]:
    return [{"name": "package:tmux", "status": "pass", "detail": ""}, {"name": "service:podman.socket", "status": "pass", "detail": ""}]


class ScheduleTests(unittest.TestCase):
    def test_every_base_is_scheduled_exactly_once_a_week(self) -> None:
        # A base added to BASE_IMAGES and not given a night would never be
        # booted, with nothing failing; one scheduled twice spends a slot.
        week = [key for night in NIGHTLY_SLOTS for key in night if key != BLUEBUILD]
        self.assertEqual(sorted(week), sorted(base.key for base in BASE_IMAGES))

    def test_each_night_runs_two_different_slots(self) -> None:
        for offset in range(7):
            day = MONDAY + dt.timedelta(days=offset)
            with self.subTest(day=day.isoformat()):
                slots = plan(day)
                self.assertEqual(len(slots), 2)
                self.assertEqual(len({slot.label for slot in slots}), 2)

    def test_the_week_follows_the_published_table(self) -> None:
        # MAINTAINER.md and #756 promise these pairs; the rotation is the
        # contract the per-base issues and the runbook lean on.
        self.assertEqual([slot.label for slot in plan(MONDAY)], ["bazzite", "silverblue"])
        sunday = plan(MONDAY + dt.timedelta(days=6))
        self.assertEqual(sunday[0].label, "bazzite-dx-gnome")
        self.assertEqual(sunday[1].method, BLUEBUILD)

    def test_the_bluebuild_slot_reaches_every_base_in_as_many_weeks(self) -> None:
        sundays = [MONDAY + dt.timedelta(days=6 + 7 * week) for week in range(len(BASE_IMAGES))]
        self.assertEqual({bluebuild_base(day).key for day in sundays}, {base.key for base in BASE_IMAGES})

    def test_an_unknown_base_is_refused_with_the_known_ones(self) -> None:
        with self.assertRaises(ValueError) as caught:
            base_by_key("bazzite-typo")
        self.assertIn("sway-atomic", str(caught.exception))


class PlanCommandTests(unittest.TestCase):
    def test_the_matrix_is_json_the_workflow_can_feed_to_fromJSON(self) -> None:
        with patch("builtins.print") as printed:
            self.assertEqual(main(["plan", "--date", MONDAY.isoformat()]), 0)
        self.assertEqual(
            json.loads(printed.call_args.args[0]),
            [{"base": "bazzite", "method": CONTAINERFILE}, {"base": "silverblue", "method": CONTAINERFILE}],
        )

    def test_a_dispatched_base_replaces_the_nights_pair(self) -> None:
        with patch("builtins.print") as printed:
            main(["plan", "--base", "aurora", "--method", BLUEBUILD])
        self.assertEqual(json.loads(printed.call_args.args[0]), [{"base": "aurora", "method": BLUEBUILD}])

    def test_a_dispatched_unknown_base_fails_the_plan(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main(["plan", "--base", "nope"])
        self.assertEqual(caught.exception.code, 2)


class GenerateTests(unittest.TestCase):
    def test_the_generated_project_asks_for_the_test_package_and_service(self) -> None:
        # The checks are only meaningful if the project the tool writes
        # actually requests what the guest is then asked about.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / "project"
            generate(Slot(base_by_key("sway-atomic"), CONTAINERFILE), context)
            build_sh = (context / "build_files" / "build.sh").read_text()
            self.assertIn("tmux", build_sh)
            self.assertIn("systemctl enable podman.socket", build_sh)
            self.assertIn("FROM quay.io/fedora-ostree-desktops/sway-atomic", (context / "Containerfile").read_text())

    def test_the_project_is_written_by_the_same_method_as_the_menus_local_build(self) -> None:
        # The test is only worth running if it builds what "Test build
        # locally" builds. A hand copy of that sequence here drifts the first
        # time the menu's side changes, and nothing would say so.
        with tempfile.TemporaryDirectory() as tmp, patch(
            "variant_boot_test.App.write_local_build_context", autospec=True, return_value=True
        ) as write:
            context = Path(tmp) / "project"
            generate(Slot(base_by_key("bluefin"), BLUEBUILD), context)
        write.assert_called_once()
        self.assertEqual(write.call_args.args[1:], (context, None))
        self.assertEqual(write.call_args.args[0].config.method, BLUEBUILD)

    def test_the_project_starts_from_the_bundled_template(self) -> None:
        # The build reads files that only the template snapshot ships: the
        # Containerfile copies system_files into its ctx stage, and a BlueBuild
        # recipe's files module reads files/system. write_project_files() alone
        # writes neither, so a context that skips the seed fails to build.
        cases = (
            ("sway-atomic", CONTAINERFILE, CONTAINERFILE_TEMPLATE_DIR),
            ("bluefin", BLUEBUILD, BLUEBUILD_TEMPLATE_DIR),
        )
        for key, method, template in cases:
            with self.subTest(base=key), tempfile.TemporaryDirectory() as tmp, patch(
                "variant_boot_test.App.render_bluebuild_containerfile", return_value=True
            ):
                context = Path(tmp) / "project"
                generate(Slot(base_by_key(key), method), context)
                shipped = {
                    path.relative_to(template)
                    for path in template.rglob("*")
                    if path.is_file() and path.name not in {".template-source", "renovate.json5"}
                }
                written = {path.relative_to(context) for path in context.rglob("*") if path.is_file()}
                self.assertTrue(shipped)
                self.assertLessEqual(shipped, written)

    def test_every_path_the_containerfile_copies_is_in_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / "project"
            generate(Slot(base_by_key("sway-atomic"), CONTAINERFILE), context)
            sources = re.findall(r"^COPY (\S+) ", (context / "Containerfile").read_text(), re.MULTILINE)
            self.assertIn("system_files", sources)
            for source in sources:
                with self.subTest(source=source):
                    self.assertTrue((context / source).is_dir())

    def test_a_render_failure_is_reported_as_the_generate_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("variant_boot_test.App.render_bluebuild_containerfile", return_value=False):
            with self.assertRaises(StageFailure) as caught:
                generate(Slot(base_by_key("bluefin"), BLUEBUILD), Path(tmp) / "project")
        self.assertEqual(caught.exception.stage, "generate")


class BuildTests(unittest.TestCase):
    def test_a_failed_build_names_the_step_it_failed_at(self) -> None:
        log = "[1/2] STEP 1/2: FROM scratch AS ctx\n[2/2] STEP 2/4: RUN /ctx/build.sh\nNo match for argument: tmux\nError: building at STEP\n"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 1, log, "")
        ):
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("bazzite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
            self.assertEqual((Path(tmp) / "build.log").read_text(), log)
        self.assertEqual(caught.exception.stage, "build")
        self.assertIn("RUN /ctx/build.sh", caught.exception.summary)
        self.assertIn("No match for argument: tmux", caught.exception.log_tail)

    def test_a_timed_out_build_names_the_step_it_stopped_at(self) -> None:
        # subprocess keeps a timed-out command's output as bytes even with
        # text=True; formatting them wrote b'...' with escaped newlines.
        timeout = subprocess.TimeoutExpired(["podman"], 1, output=b"[2/2] STEP 2/4: RUN /ctx/build.sh\nInstalling tmux\n")
        with tempfile.TemporaryDirectory() as tmp, patch("variant_boot_test.subprocess.run", side_effect=timeout):
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("bazzite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
            self.assertTrue((Path(tmp) / "build.log").read_text().startswith("[2/2] STEP 2/4: RUN /ctx/build.sh\nInstalling tmux\n"))
        self.assertIn("at `[2/2] STEP 2/4: RUN /ctx/build.sh`", caught.exception.summary)
        self.assertIn("timed out after", caught.exception.log_tail)

    def test_a_timed_out_build_keeps_its_stderr_and_bytes_that_are_not_utf8(self) -> None:
        # podman writes its errors to stderr, after what the step printed, and
        # a hung step can print bytes that are not UTF-8. The log keeps both
        # streams in the order a finished build writes them, and a bad byte
        # becomes a replacement character instead of crashing the slot.
        timeout = subprocess.TimeoutExpired(
            ["podman"], 1, output=b"[2/2] STEP 2/4: RUN /ctx/build.sh\n", stderr=b"Error: \xff stalled\n"
        )
        with tempfile.TemporaryDirectory() as tmp, patch("variant_boot_test.subprocess.run", side_effect=timeout):
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("bazzite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
            log = (Path(tmp) / "build.log").read_text()
        self.assertTrue(log.startswith("[2/2] STEP 2/4: RUN /ctx/build.sh\nError: \ufffd stalled\n"))
        self.assertIn("Error: \ufffd stalled", caught.exception.log_tail)

    def test_a_base_pull_the_registry_dropped_is_retried_once_and_a_good_retry_passes(self) -> None:
        uri = base_by_key("kinoite").image_uri
        dropped = (
            f"[2/2] STEP 1/3: FROM {uri}\n"
            f'Error: creating build container: unable to copy from source docker://{uri}: '
            'copying system image from manifest list: writing blob: happened during read: unexpected EOF '
            '(while reconnecting: Get "https://cdn01.quay.io/...": EOF)\n'
        )
        good = f"[2/2] STEP 1/3: FROM {uri}\n"
        runs = [subprocess.CompletedProcess([], 125, "", dropped), subprocess.CompletedProcess([], 0, good, "")]
        with tempfile.TemporaryDirectory() as tmp, patch("variant_boot_test.subprocess.run", side_effect=runs) as run, patch(
            "variant_boot_test.time.sleep"
        ) as sleep:
            build(Slot(base_by_key("kinoite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
            log = (Path(tmp) / "build.log").read_text()
            reference = base_reference(base_by_key("kinoite"), Path(tmp) / "build.log")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()
        # The first try gets the whole budget; the retry only what is left of it.
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], BUILD_TIMEOUT_SECONDS)
        self.assertLess(run.call_args_list[1].kwargs["timeout"], BUILD_TIMEOUT_SECONDS)
        self.assertIn("unexpected EOF", log)
        self.assertTrue(log.endswith(good))
        self.assertEqual(reference, uri)

    def test_a_base_pull_dropped_twice_fails_with_the_retrys_output(self) -> None:
        uri = base_by_key("kinoite").image_uri
        first = f"[2/2] STEP 1/3: FROM {uri}\nunexpected EOF\n"
        second = f"[2/2] STEP 1/3: FROM {uri}\nread tcp: connection reset by peer\n"
        runs = [subprocess.CompletedProcess([], 125, "", first), subprocess.CompletedProcess([], 125, "", second)]
        with tempfile.TemporaryDirectory() as tmp, patch("variant_boot_test.subprocess.run", side_effect=runs) as run, patch(
            "variant_boot_test.time.sleep"
        ):
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("kinoite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
            log = (Path(tmp) / "build.log").read_text()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(caught.exception.stage, "build")
        self.assertIn("exit 125", caught.exception.summary)
        self.assertIn("connection reset by peer", caught.exception.log_tail)
        self.assertNotIn("unexpected EOF", caught.exception.log_tail)
        self.assertIn("unexpected EOF", log)
        self.assertIn("connection reset by peer", log)

    def test_a_transport_error_after_the_pull_is_not_retried(self) -> None:
        # A RUN step that reaches a flaky mirror is a failure of the image.
        log = "[2/2] STEP 1/3: FROM x\n[2/2] STEP 2/3: RUN /ctx/build.sh\nCurl error: i/o timeout\n"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 1, log, "")
        ) as run, patch("variant_boot_test.time.sleep") as sleep:
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("kinoite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("RUN /ctx/build.sh", caught.exception.summary)

    def test_a_base_pull_refused_by_the_registry_is_not_retried(self) -> None:
        uri = base_by_key("kinoite").image_uri
        log = f"[2/2] STEP 1/3: FROM {uri}\nError: manifest unknown\n"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 125, "", log)
        ) as run, patch("variant_boot_test.time.sleep") as sleep:
            with self.assertRaises(StageFailure) as caught:
                build(Slot(base_by_key("kinoite"), CONTAINERFILE), Path(tmp), Path(tmp) / "build.log")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("manifest unknown", caught.exception.log_tail)

    def test_the_base_digest_is_read_from_the_reference_the_build_pulled(self) -> None:
        # The BlueBuild case is the first lines of budgie-atomic's build log
        # from the first hosted run: the rendered Containerfile says
        # FROM "${BASE_IMAGE}", and the step shows the digest it expanded to.
        # The tag is not in local storage after that build, so it cannot be
        # what the digest is read from.
        pinned = "quay.io/fedora-ostree-desktops/budgie-atomic@sha256:5be0a50e0a987e11e37a5f55e5310acc11f376a94489272ff70ea10199e41b87"
        cases = (
            ("budgie-atomic", f"[1/6] STEP 1/1: FROM {pinned} AS variant-budgie-atomic\n[2/6] STEP 1/2: FROM scratch AS stage-files\n", pinned),
            ("bazzite", "[1/2] STEP 1/2: FROM scratch AS ctx\n[2/2] STEP 1/4: FROM ghcr.io/ublue-os/bazzite:stable\n", "ghcr.io/ublue-os/bazzite:stable"),
        )
        for key, log, expected in cases:
            with self.subTest(base=key), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "build.log").write_text(log)
                self.assertEqual(base_reference(base_by_key(key), Path(tmp) / "build.log"), expected)

    def test_a_quoted_from_step_is_read_without_its_quotes(self) -> None:
        pinned = "quay.io/fedora-ostree-desktops/budgie-atomic@sha256:5be0a50e0a987e11e37a5f55e5310acc11f376a94489272ff70ea10199e41b87"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build.log").write_text(f'[1/6] STEP 1/1: FROM "{pinned}" AS variant-budgie-atomic\n')
            self.assertEqual(base_reference(base_by_key("budgie-atomic"), Path(tmp) / "build.log"), pinned)

    def test_a_log_that_never_pulled_the_base_falls_back_to_its_tag(self) -> None:
        # Another base's FROM step is not this one's; the tag is still the
        # best reference to inspect, and an empty one would inspect nothing.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build.log").write_text("[1/2] STEP 1/2: FROM scratch AS ctx\n[2/2] STEP 1/4: FROM ghcr.io/ublue-os/aurora:stable\n")
            self.assertEqual(base_reference(base_by_key("bazzite"), Path(tmp) / "build.log"), "ghcr.io/ublue-os/bazzite:stable")

    def test_a_build_pulls_tonights_base_and_skips_the_layer_cache_within_its_limit(self) -> None:
        # --no-cache: podman keyed the build.sh RUN step on its command line,
        # not the bind-mounted script, and reused a stale layer.
        with tempfile.TemporaryDirectory() as tmp, patch(
            "variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "STEP 1/1: FROM x\n", "")
        ) as run:
            slot = Slot(base_by_key("bazzite"), CONTAINERFILE)
            build(slot, Path(tmp), Path(tmp) / "build.log")
            self.assertEqual((Path(tmp) / "build.log").read_text(), "STEP 1/1: FROM x\n")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["podman", "build"])
        self.assertIn("--pull=always", command)
        self.assertIn("--no-cache", command)
        self.assertEqual(command[-3:], ["-t", slot.tag, tmp])
        self.assertEqual(run.call_args.kwargs["timeout"], BUILD_TIMEOUT_SECONDS)

    def test_a_reference_podman_cannot_inspect_has_no_digest(self) -> None:
        with patch("variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "sha256:abc\n", "")):
            self.assertEqual(image_digest("ghcr.io/ublue-os/bazzite:stable"), "sha256:abc")
        # The failed inspect's stdout is not a digest, even when it is not empty.
        with patch("variant_boot_test.subprocess.run", return_value=subprocess.CompletedProcess([], 125, "Error: no such image\n", "")):
            self.assertEqual(image_digest("ghcr.io/ublue-os/bazzite:stable"), "")


@unittest.skipIf(shutil.which("bash") is None, "bash is not installed")
class GuestScriptTests(unittest.TestCase):
    """The script the guest runs, against stand-ins for systemctl and rpm.

    parse_guest_output() only understands what this script prints, so the two
    are tested together: a quoting slip in the script shows up as a check the
    parser never sees.
    """

    def run_script(self, *, state: str, installed: str, enabled: str, active: str, failed: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            stubs = {
                "systemctl": f"""#!/bin/bash
case "$1" in
  is-system-running) echo {state} ;;
  is-enabled) echo {enabled} ;;
  is-active) echo {active} ;;
  --failed) printf '%b' '{failed}' ;;
esac
""",
                "rpm": f"""#!/bin/bash
for name in {installed}; do [ "${{@: -1}}" = "$name" ] && exit 0; done
exit 1
""",
            }
            for name, body in stubs.items():
                (bin_dir / name).write_text(body)
                (bin_dir / name).chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            proc = subprocess.run(
                ["bash", "-c", guest_script(["tmux", "vim"], ["podman.socket"])], capture_output=True, text=True, env=env, check=False
            )
            return proc.stdout

    def test_a_healthy_guest_reports_every_check_and_its_failed_units(self) -> None:
        output = self.run_script(
            state="degraded", installed="tmux vim", enabled="enabled", active="active", failed="mcelog.service loaded failed failed x\\n"
        )
        done, state, checks, units = parse_guest_output(output)
        self.assertTrue(done)
        self.assertEqual(state, "degraded")
        self.assertEqual([(c["name"], c["status"]) for c in checks], [("package:tmux", "pass"), ("package:vim", "pass"), ("service:podman.socket", "pass")])
        self.assertEqual(units, ["mcelog.service"])

    def test_a_missing_package_and_a_disabled_service_fail_their_checks(self) -> None:
        output = self.run_script(state="running", installed="tmux", enabled="disabled", active="inactive", failed="")
        _, _, checks, units = parse_guest_output(output)
        statuses = {c["name"]: (c["status"], c["detail"]) for c in checks}
        self.assertEqual(statuses["package:vim"], ("fail", "not-installed"))
        self.assertEqual(statuses["service:podman.socket"], ("fail", "enabled=disabled,active=inactive"))
        self.assertEqual(units, [])


class ParseTests(unittest.TestCase):
    def test_terminal_queries_ahead_of_the_first_marker_do_not_hide_it(self) -> None:
        # The first bytes of bazzite's real output from the hosted run: its
        # login shell asks for the terminal colours and cursor position on
        # the same line as the first marker, which was then missed and the
        # boot reported as never settling.
        output = "\x1b]10;?\x1b\\\x1b[6n\x1b]11;?\x1b\\\x1b[6nAIB-STATE degraded\nAIB-DONE\n"
        done, state, _, _ = parse_guest_output(output)
        self.assertTrue(done)
        self.assertEqual(state, "degraded")

    def test_login_noise_is_ignored_and_a_missing_done_marker_means_not_reached(self) -> None:
        done, state, checks, _ = parse_guest_output("Welcome to Bazzite\nAIB-STATE running\nAIB-CHECK package:tmux pass\n")
        self.assertFalse(done)
        self.assertEqual(state, "running")
        self.assertEqual(checks, [{"name": "package:tmux", "status": "pass", "detail": ""}])

    def test_a_blank_failed_unit_line_is_not_a_failed_unit(self) -> None:
        # judge() would count an empty name as an unexpected unit and fail a
        # base that has none.
        _, _, _, units = parse_guest_output("AIB-FAILED-UNIT \nAIB-FAILED-UNIT sddm.service\nAIB-DONE\n")
        self.assertEqual(units, ["sddm.service"])


class JudgeTests(unittest.TestCase):
    def test_degraded_by_allowlisted_units_alone_passes(self) -> None:
        stage, _, unexpected = judge(booted(checks=passing_checks(), units=sorted(ALLOWED_FAILED_UNITS)))
        self.assertEqual((stage, unexpected), ("passed", []))

    def test_an_unlisted_failed_unit_fails_the_checks(self) -> None:
        stage, summary, unexpected = judge(booted(checks=passing_checks(), units=["mcelog.service", "sddm.service"]))
        self.assertEqual((stage, unexpected), ("checks", ["sddm.service"]))
        self.assertIn("sddm.service", summary)

    def test_a_failed_check_fails_even_when_systemd_says_running(self) -> None:
        checks = [{"name": "service:podman.socket", "status": "fail", "detail": "enabled=disabled,active=inactive"}]
        self.assertEqual(judge(booted(state="running", checks=checks))[0], "checks")

    def test_a_boot_that_never_settles_is_a_boot_failure(self) -> None:
        for boot_result in (
            booted(reached=False),
            booted(state="starting", checks=passing_checks()),
            booted(state="running", checks=passing_checks(), console=["Kernel panic"]),
        ):
            with self.subTest(state=boot_result.state, reached=boot_result.reached, console=boot_result.console_errors):
                self.assertEqual(judge(boot_result)[0], "boot")

    def test_every_allowlisted_unit_says_why(self) -> None:
        for unit, reason in ALLOWED_FAILED_UNITS.items():
            with self.subTest(unit=unit):
                self.assertGreater(len(reason.split()), 5)


class BlameTests(unittest.TestCase):
    def test_units_failing_in_the_unmodified_base_too_are_upstream(self) -> None:
        ours = booted(checks=passing_checks(), units=["sddm.service"])
        self.assertIn("Upstream", compare_with_upstream(ours, booted(units=["sddm.service"])))

    def test_a_failed_package_or_service_check_is_never_upstreams(self) -> None:
        # The unmodified base was never asked for the test package or service,
        # so sharing a failed unit with it cannot excuse a failed check.
        ours = booted(checks=[{"name": "package:tmux", "status": "fail", "detail": "not-installed"}], units=["sddm.service"])
        self.assertIn("atomic-image-builder", compare_with_upstream(ours, booted(units=["sddm.service"])))

    def test_a_unit_failing_only_in_the_generated_image_is_the_tools(self) -> None:
        ours = booted(checks=passing_checks(), units=["sddm.service", "x.service"])
        self.assertIn("atomic-image-builder", compare_with_upstream(ours, booted(units=["sddm.service"])))

    def test_a_boot_failure_is_upstreams_only_when_the_base_fails_the_same_way(self) -> None:
        unreachable = booted(reached=False)
        panicking = booted(checks=passing_checks(), console=["Kernel panic"])
        self.assertIn("Upstream", compare_with_upstream(unreachable, booted(reached=False)))
        self.assertIn("Upstream", compare_with_upstream(panicking, booted(console=["Kernel panic"])))
        # Both are "boot" failures, but not the same one: an image that never
        # answers SSH is not explained by a base that answers and panics.
        self.assertIn("atomic-image-builder", compare_with_upstream(unreachable, panicking))
        self.assertIn("atomic-image-builder", compare_with_upstream(unreachable, booted(checks=passing_checks())))

    def test_a_base_vm_that_never_started_blames_no_one(self) -> None:
        # The unmodified base was not compared at all, so neither "the same
        # way" nor "does not fail this way" is known.
        dead = Boot(False, "", [], [], [], "virtiofsd failed to start", "virtiofsd failed to start")
        for ours in (booted(reached=False), booted(checks=passing_checks(), units=["sddm.service"])):
            with self.subTest(stage=judge(ours)[0]):
                self.assertIn("Unknown", compare_with_upstream(ours, dead))

    def test_two_boot_failures_with_different_signs_are_not_the_same_way(self) -> None:
        # Both judged "boot", but a panic is not explained by an oops, and a
        # boot stuck starting is not explained by one in maintenance mode.
        panicking = booted(checks=passing_checks(), console=["Kernel panic"])
        self.assertIn("atomic-image-builder", compare_with_upstream(panicking, booted(console=["Oops"])))
        self.assertIn("atomic-image-builder", compare_with_upstream(panicking, booted(state="degraded")))
        starting = booted(state="starting")
        self.assertIn("Upstream", compare_with_upstream(starting, booted(state="starting")))
        self.assertIn("atomic-image-builder", compare_with_upstream(starting, booted(state="maintenance")))

    def test_a_base_that_never_answered_cannot_excuse_a_failed_check(self) -> None:
        # The base booted but never reached the checks, so its failed units
        # are unknown: neither "upstream too" nor "only ours" is known.
        ours = booted(checks=passing_checks(), units=["sddm.service"])
        self.assertIn("Unknown", compare_with_upstream(ours, booted(reached=False)))


class BootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def boot(self, output: str, *, console: str | None = None, journal: str | None = None, log: str = "boot-1"):
        log_dir = self.work / log

        def run(command, **kwargs):
            # bcvk writes the console and journal into --log-dir.
            if console is not None:
                (log_dir / "console.txt").write_text(console)
            if journal is not None:
                (log_dir / "journal.json").write_text(journal)
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch("variant_boot_test.subprocess.run", side_effect=run) as bcvk:
            result = boot("localhost/aib-test:x", log_dir, packages=("tmux",), services=("podman.socket",))
        return result, bcvk

    def test_a_timed_out_boot_keeps_what_the_guest_reported(self) -> None:
        # subprocess keeps a timed-out command's output as bytes even with
        # text=True; formatting them hid the guest's report in one b'...' line.
        timeout = subprocess.TimeoutExpired(["bcvk"], 1, output=b"AIB-STATE running\nAIB-CHECK package:tmux pass\nAIB-FAILED-UNIT sddm.service\n")
        with patch("variant_boot_test.subprocess.run", side_effect=timeout):
            result = boot("localhost/aib-test:x", self.work / "boot-1", packages=("tmux",), services=())
        self.assertFalse(result.reached)
        self.assertEqual((result.state, result.failed_units), ("running", ["sddm.service"]))
        self.assertEqual(result.checks, [{"name": "package:tmux", "status": "pass", "detail": ""}])

    def test_every_boot_injects_the_roothome_unit_and_sshd(self) -> None:
        units = write_units(self.work / "units")
        wants = units / "system" / "default.target.wants"
        self.assertEqual((units / "system" / "aib-roothome.service").read_text(), ROOTHOME_UNIT)
        self.assertEqual(os.readlink(wants / "aib-roothome.service"), "../aib-roothome.service")
        # The desktops ship sshd disabled, and bcvk reaches the guest over SSH.
        self.assertEqual(os.readlink(wants / "sshd.service"), "/usr/lib/systemd/system/sshd.service")
        # The retry and the unmodified base share the directory.
        self.assertEqual(write_units(self.work / "units"), units)

    def test_bcvk_runs_the_check_script_from_a_read_only_mount(self) -> None:
        result, bcvk = self.boot("AIB-STATE running\nAIB-CHECK package:tmux pass\nAIB-DONE\n", journal="{}\n")
        log_dir = self.work / "boot-1"
        script = (log_dir / "checks" / "checks.sh").read_text()
        self.assertEqual(script, guest_script(("tmux",), ("podman.socket",)) + "\n")
        command = bcvk.call_args.args[0]
        self.assertEqual(command[:3], ["bcvk", "ephemeral", "run-ssh"])
        self.assertEqual(command[command.index("--systemd-units") + 1], str(self.work / "units"))
        self.assertEqual(command[command.index("--ro-bind") + 1], f"{log_dir / 'checks'}:{GUEST_SCRIPT_MOUNT}")
        self.assertEqual(command[command.index("--") - 1], "localhost/aib-test:x")
        self.assertEqual(command[-1], f"bash /run/virtiofs-mnt-{GUEST_SCRIPT_MOUNT}/checks.sh")
        self.assertEqual(bcvk.call_args.kwargs["timeout"], BOOT_TIMEOUT_SECONDS)
        self.assertTrue((self.work / "units" / "system" / "default.target.wants" / "sshd.service").is_symlink())
        self.assertEqual((log_dir / "bcvk.log").read_text(), "AIB-STATE running\nAIB-CHECK package:tmux pass\nAIB-DONE\n")
        self.assertEqual((result.reached, result.state, result.host_error), (True, "running", ""))
        self.assertEqual(result.checks, [{"name": "package:tmux", "status": "pass", "detail": ""}])

    def test_the_console_is_read_for_kernel_failures(self) -> None:
        result, _ = self.boot("AIB-DONE\n", console="[ 1.0] Kernel panic - not syncing\n[ 2.0] Oops: 0000\n", journal="{}\n")
        self.assertEqual(result.console_errors, ["Kernel panic", "Oops"])
        self.assertIn("Kernel panic - not syncing", result.console)
        quiet, _ = self.boot("AIB-DONE\n", journal="{}\n", log="boot-2")
        self.assertEqual((quiet.console, quiet.console_errors), ("", []))

    def test_an_empty_journal_counts_as_none_written(self) -> None:
        # bcvk creates journal.json before the guest starts; an empty one means
        # the guest never wrote a line, so a named host failure is the runner's.
        output = "Error:\n   0: virtiofsd failed to start for socket /run/inner-shared/virtiofs.sock\n"
        empty, _ = self.boot(output, journal="")
        self.assertIn("virtiofsd failed to start", empty.host_error)
        written, _ = self.boot(output, journal="{}\n", log="boot-2")
        self.assertEqual(written.host_error, "")


class WorkflowBudgetTests(unittest.TestCase):
    def test_the_job_outlives_the_slowest_slot(self) -> None:
        # If the job's timeout-minutes cancels it first, the steps that upload
        # the logs and report the issue never run -- on exactly the slow
        # failures they exist for. Ten minutes covers the runner setup.
        workflow = (REPO_ROOT / ".github" / "workflows" / "variant-boot-test.yml").read_text()
        boot_job = workflow[workflow.index("\n  boot:\n") :]
        minutes = int(re.search(r"^    timeout-minutes: (\d+)$", boot_job, re.MULTILINE).group(1))
        self.assertGreaterEqual(minutes * 60, WORST_CASE_SLOT_SECONDS + 10 * 60)


class RunSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.slot = Slot(base_by_key("kinoite"), CONTAINERFILE)
        for target, value in (("generate", None), ("build", None), ("base_reference", "quay.io/x:1"), ("image_digest", "sha256:abc")):
            patcher = patch(f"variant_boot_test.{target}", return_value=value)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch("variant_boot_test.subprocess.run")
        self.podman = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_first_boot_is_retried_once_without_rebuilding(self) -> None:
        good = booted(checks=passing_checks(), units=["mcelog.service"])
        with patch("variant_boot_test.boot", side_effect=[booted(reached=False), good]) as boot:
            result = run_slot(self.slot, self.work)
        self.assertTrue(result.passed)
        self.assertEqual((result.attempts, boot.call_count, self.build.call_count), (2, 2, 1))
        self.assertEqual(result.base_digest, "sha256:abc")

    def test_two_failed_boots_compare_with_the_unmodified_base(self) -> None:
        bad = booted(checks=passing_checks(), units=["sddm.service"])
        with patch("variant_boot_test.boot", side_effect=[bad, bad, booted(units=["sddm.service"])]) as boot:
            result = run_slot(self.slot, self.work)
        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "checks")
        self.assertEqual(boot.call_args.args[0], "quay.io/fedora-ostree-desktops/kinoite:44")
        self.assertEqual(result.upstream_failed_units, ["sddm.service"])
        self.assertIn("Upstream", result.fault)

    def test_a_generate_failure_stops_before_anything_is_built(self) -> None:
        self.generate.side_effect = StageFailure("generate", "could not write", "atomic-image-builder: ...")
        with patch("variant_boot_test.boot") as boot:
            result = run_slot(self.slot, self.work)
        self.assertEqual(result.stage, "generate")
        self.build.assert_not_called()
        boot.assert_not_called()
        self.podman.assert_not_called()

    def test_the_built_image_is_removed_unless_kept(self) -> None:
        with patch("variant_boot_test.boot", return_value=booted(checks=passing_checks())):
            run_slot(self.slot, self.work)
            self.assertEqual(self.podman.call_args.args[0], ["podman", "rmi", "-f", self.slot.tag])
            self.podman.reset_mock()
            run_slot(self.slot, self.work, keep_image=True)
        self.podman.assert_not_called()

    def test_a_vm_that_never_started_is_not_blamed_on_either_image(self) -> None:
        # Seen on the hosted runner: virtiofsd exited before the guest wrote a
        # single journal line. Booting the unmodified base would fail the same
        # way and "prove" upstream broke.
        output = "Error:\n   0: virtiofsd failed to start for socket /run/inner-shared/virtiofs.sock\n"
        dead = Boot(False, "", [], [], [], output, host_failure(output, journal_written=False))
        with patch("variant_boot_test.boot", side_effect=[dead, dead]) as boot:
            result = run_slot(self.slot, self.work)
        self.assertEqual(result.stage, "host")
        self.assertEqual(boot.call_count, 2)
        self.assertNotIn("Upstream", result.fault)
        self.assertIn("virtiofsd failed to start", result.summary)

    def test_a_retry_that_never_started_keeps_the_first_attempts_failure(self) -> None:
        # The first boot reached a real failure of the image; filing the
        # retry's runner failure instead would move it to the runner issue,
        # which the next passing slot closes.
        output = "virtiofsd failed to start"
        dead = Boot(False, "", [], [], [], output, host_failure(output, journal_written=False))
        bad = booted(checks=passing_checks(), units=["sddm.service"])
        with patch("variant_boot_test.boot", side_effect=[bad, dead, booted(units=["sddm.service"])]):
            result = run_slot(self.slot, self.work)
        self.assertEqual((result.stage, result.attempts, result.unexpected_units), ("checks", 2, ["sddm.service"]))
        self.assertIn("Upstream", result.fault)

    def test_only_a_named_host_failure_with_no_guest_journal_is_the_runners(self) -> None:
        # A guest that dies before its journal reaches the host is silent too;
        # without a host-side error from bcvk it stays a failure of the base.
        self.assertEqual(host_failure("Timeout waiting for readiness after 240s", journal_written=False), "")
        self.assertEqual(host_failure("0: virtiofsd failed to start for socket x", journal_written=True), "")
        self.assertIn("OCI runtime error", host_failure("Error: OCI runtime error: crun: setns", journal_written=False))

    def test_the_issue_log_carries_the_guest_console_as_well_as_bcvk(self) -> None:
        failing = Boot(True, "degraded", passing_checks(), ["sddm.service"], [], "AIB-DONE", console="sddm: no display\n")
        with patch("variant_boot_test.boot", side_effect=[failing, failing, booted()]):
            result = run_slot(self.slot, self.work)
        self.assertIn("sddm: no display", result.log_tail)
        self.assertIn("AIB-DONE", result.log_tail)

    def test_run_refuses_a_work_directory_that_already_holds_a_project(self) -> None:
        (self.work / "project").mkdir()
        with patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main(["run", "--base", "kinoite", "--work", str(self.work)])
        self.assertEqual(caught.exception.code, 2)
        self.generate.assert_not_called()

    def test_a_base_that_never_answered_records_no_failed_units(self) -> None:
        # "none" would claim the base booted clean; it never reached the checks.
        bad = booted(checks=passing_checks(), units=["sddm.service"])
        with patch("variant_boot_test.boot", side_effect=[bad, bad, booted(reached=False)]):
            result = run_slot(self.slot, self.work)
        self.assertIsNone(result.upstream_failed_units)
        self.assertIn("Unknown", result.fault)

    def test_run_writes_the_result_the_report_step_reads_and_fails_the_step_on_a_failure(self) -> None:
        # The run step's exit status is what turns the nightly job red, and
        # result.json is all the report step knows about the slot.
        failed = Result(base="kinoite", method=CONTAINERFILE, image_uri="quay.io/x:1", stage="checks", summary="Booted, but failed units: x.service.", fault="atomic-image-builder: ...", upstream_failed_units=[])
        passed = Result(base="kinoite", method=CONTAINERFILE, image_uri="quay.io/x:1", stage="passed", passed=True)
        for result, code in ((failed, 1), (passed, 0)):
            # run_slot, patched out here, is what creates the directory.
            work = self.work / result.stage
            work.mkdir()
            with self.subTest(stage=result.stage), patch("variant_boot_test.run_slot", return_value=result), patch("builtins.print") as printed:
                self.assertEqual(main(["run", "--base", "kinoite", "--work", str(work)]), code)
                self.assertEqual(load_result(work / "result.json", self.slot), result)
                said = "\n".join(call.args[0] for call in printed.call_args_list)
                self.assertEqual("Whose fault: atomic-image-builder" in said, not result.passed)


class IssueTests(unittest.TestCase):
    def failed(self, method: str = CONTAINERFILE) -> Result:
        return Result(base="bazzite", method=method, image_uri="ghcr.io/ublue-os/bazzite:stable", stage="build", summary="podman build failed.", fault="Unclear")

    def test_a_first_failure_opens_the_bases_issue(self) -> None:
        with patch("variant_boot_test.run_gh", side_effect=["[]", "https://example/issues/9\n"]) as gh:
            message = sync_issue(self.failed(BLUEBUILD), repo="o/r", run_url="https://run")
        create = gh.call_args.args[0]
        self.assertEqual(create[:2], ["issue", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Nightly boot test failing: bazzite (BlueBuild)")
        body = create[create.index("--body") + 1]
        self.assertIn("--method bluebuild", body)
        self.assertIn("https://run", body)
        self.assertIn("opened https://example/issues/9", message)

    def test_a_repeat_failure_comments_instead_of_filing_again(self) -> None:
        listed = json.dumps([{"number": 4, "title": issue_title("bazzite"), "author": BOT_AUTHOR}])
        with patch("variant_boot_test.run_gh", side_effect=[listed, ""]) as gh:
            sync_issue(self.failed(), repo="o/r", run_url=None)
        self.assertEqual(gh.call_args.args[0][:3], ["issue", "comment", "4"])

    def test_a_pass_closes_only_that_bases_issue_and_the_runner_issue(self) -> None:
        listed = json.dumps(
            [
                {"number": 4, "title": issue_title("bazzite"), "author": BOT_AUTHOR},
                {"number": 5, "title": issue_title("aurora"), "author": BOT_AUTHOR},
                {"number": 6, "title": HOST_ISSUE_TITLE, "author": BOT_AUTHOR},
            ]
        )
        passed = Result(base="aurora", method=CONTAINERFILE, image_uri="x", stage="passed", passed=True)
        with patch("variant_boot_test.run_gh", side_effect=[listed, "", "", listed, "", ""]) as gh:
            sync_issue(passed, repo="o/r", run_url="https://run")
        writes = [call.args[0][:3] for call in gh.call_args_list if call.args[0][1] != "list"]
        self.assertEqual(writes, [["issue", "comment", "5"], ["issue", "close", "5"], ["issue", "comment", "6"], ["issue", "close", "6"]])

    def test_a_pass_with_no_issue_writes_nothing(self) -> None:
        passed = Result(base="aurora", method=CONTAINERFILE, image_uri="x", stage="passed", passed=True)
        with patch("variant_boot_test.run_gh", return_value="[]") as gh:
            sync_issue(passed, repo=None, run_url=None)
        self.assertEqual({call.args[0][1] for call in gh.call_args_list}, {"list"})

    def test_an_issue_with_the_title_but_another_author_is_not_ours(self) -> None:
        # The repository takes issues from anyone; a squatted title must not
        # be commented on, closed, or stop the real issue being filed.
        listed = json.dumps([{"number": 4, "title": issue_title("bazzite"), "author": {"login": "someone"}}])
        with patch("variant_boot_test.run_gh", side_effect=[listed, "https://example/issues/10"]) as gh:
            sync_issue(self.failed(), repo="o/r", run_url=None)
        self.assertEqual(gh.call_args.args[0][:2], ["issue", "create"])

    def test_a_failure_before_the_image_ran_goes_to_the_runner_issue_not_the_bases(self) -> None:
        # A job that died in setup wrote no result.json; a bcvk that never
        # started the VM wrote stage "host". Neither says anything about the
        # base, and both of a night's jobs would report it.
        with tempfile.TemporaryDirectory() as tmp:
            setup = load_result(Path(tmp) / "result.json", Slot(base_by_key("aurora"), CONTAINERFILE))
        host = Result(base="bazzite", method=CONTAINERFILE, image_uri="x", stage="host", summary="bcvk never started the VM")
        for result in (setup, host):
            with self.subTest(stage=result.stage), patch("variant_boot_test.run_gh", side_effect=["[]", "url"]) as gh:
                sync_issue(result, repo="o/r", run_url=None)
                create = gh.call_args.args[0]
                self.assertEqual(create[create.index("--title") + 1], HOST_ISSUE_TITLE)
                # A workstation command would not reproduce a runner failure.
                body = create[create.index("--body") + 1]
                self.assertNotIn("variant_boot_test.py run", body)
                self.assertIn("Prepare the host", body)

    def test_the_body_names_the_failed_units_in_both_images(self) -> None:
        result = self.failed()
        result.stage, result.unexpected_units = "checks", ["sddm.service", "x.service"]
        body = render_issue_body(result, run_url=None)
        self.assertIn("Failed units: `sddm.service`, `x.service`", body)
        self.assertNotIn("unmodified base, booted the same way", body)
        for upstream, shown in ((["sddm.service"], "`sddm.service`"), ([], "none")):
            result.upstream_failed_units = upstream
            with self.subTest(upstream=upstream):
                self.assertIn(f"Failed units in the unmodified base, booted the same way: {shown}\n", render_issue_body(result, run_url=None))

    def test_an_unreadable_issue_list_stops_the_report_instead_of_filing_again(self) -> None:
        # Reading a gh hiccup as "no issue yet" would open a second issue for
        # a base that already has one.
        for listed in ("not json", json.dumps({"number": 4})):
            with self.subTest(listed=listed), patch("variant_boot_test.run_gh", return_value=listed) as gh:
                with self.assertRaises(RuntimeError):
                    sync_issue(self.failed(), repo="o/r", run_url=None)
                self.assertEqual(gh.call_count, 1)

    def test_a_matching_issue_without_a_number_is_skipped(self) -> None:
        listed = json.dumps([{"number": "4", "title": issue_title("bazzite"), "author": BOT_AUTHOR}])
        with patch("variant_boot_test.run_gh", side_effect=[listed, "https://example/issues/11"]) as gh:
            sync_issue(self.failed(), repo="o/r", run_url=None)
        self.assertEqual(gh.call_args.args[0][:2], ["issue", "create"])

    def test_a_gh_failure_does_not_fail_the_report_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(json.dumps(self.failed().__dict__))
            with patch("variant_boot_test.run_gh", side_effect=RuntimeError("gh timed out")), patch("builtins.print") as printed:
                self.assertEqual(main(["report", "--base", "bazzite", "--result", str(path)]), 0)
        self.assertIn("gh timed out", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

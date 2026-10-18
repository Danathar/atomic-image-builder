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

from atomic_image_builder import BASE_IMAGES
from variant_boot_test import (
    ALLOWED_FAILED_UNITS,
    BLUEBUILD,
    CONTAINERFILE,
    HOST_ISSUE_TITLE,
    NIGHTLY_SLOTS,
    WORST_CASE_SLOT_SECONDS,
    Boot,
    Result,
    Slot,
    StageFailure,
    base_by_key,
    bluebuild_base,
    build,
    compare_with_upstream,
    generate,
    guest_script,
    host_failure,
    issue_title,
    judge,
    load_result,
    main,
    parse_guest_output,
    plan,
    run_slot,
    sync_issue,
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
        for target, value in (("generate", None), ("build", None), ("image_digest", "sha256:abc")):
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

    def test_a_gh_failure_does_not_fail_the_report_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(json.dumps(self.failed().__dict__))
            with patch("variant_boot_test.run_gh", side_effect=RuntimeError("gh timed out")), patch("builtins.print") as printed:
                self.assertEqual(main(["report", "--base", "bazzite", "--result", str(path)]), 0)
        self.assertIn("gh timed out", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

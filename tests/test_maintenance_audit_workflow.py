"""Execute the `run:` bodies of .github/workflows/maintenance-audit.yml.

Nothing in the suite ran a line of this workflow's shell. Four test modules
name the file or the config it uses -- tests/test_workflow_dependencies.py
compares its `pip install` line and its action pins against the other
workflows, tests/test_maintenance_audit.py and tests/test_homebrew_formula.py
mention it in comments explaining why a branch is only reachable in a live run
-- but reading a workflow is not running it. This is the only job that runs
the maintainer scripts at all, and its shell is what decides whether a failure
it finds is reported, swallowed, or never reached.

Every decision in it fails quietly:

* *Run maintenance audit* pipes the audit into `tee`, which always exits 0.
  `set -o pipefail` is the only thing that lets a failing audit fail the job;
  without it the audit reports an inconsistency into the log, the step exits
  0, and the weekly job stays green.
* *Check the Homebrew formula pin* is measured with `coverage run -a`. The
  `-a` is what appends to the data the audit step wrote, so one report covers
  both scripts. Drop it and the artifact still uploads, the job still passes,
  and the reported maintenance-audit coverage silently becomes "the formula
  check only". Its `|| true` is what keeps this advisory check from failing
  the job, and its `tee -a` is what keeps the audit's own output in the log.
* *Track snapshot drift as an issue* composes the run URL recorded in the
  tracking issue out of three runner variables. A rename on any side leaves a
  body pointing at a URL that does not resolve, and the script exits 0 either
  way, so nothing goes red. This step is also the only place the real `gh`
  binary is invoked: tests/test_snapshot_drift_issue.py patches
  `subprocess.run` out of every case.
* *Package maintenance-audit coverage* names the artifact's data file. That
  name is a cross-suite contract -- CONTRIBUTING.md documents merging these
  artifacts, and `coverage combine` only discovers files called `.coverage.*`
  in the directory it is given. A copy to the wrong name or the wrong
  directory leaves the artifact uploaded and unmergeable.
* *Summarize* and *Report maintenance-audit coverage* both carry `if:
  always()` and both have a fallback branch, because the states they exist to
  report are exactly the states where the thing they read may be missing.

Each step's shell is extracted from the workflow rather than copied here, so
editing maintenance-audit.yml re-runs these assertions against the edit. The
steps run against a throwaway project: `coverage` itself is the real one, so
the data files, the report and the documented merge are the real ones, while
the two maintainer scripts the audit measures are stand-ins that exit on
demand -- the seam under test is the step driving them, not the scripts, which
tests/test_maintenance_audit.py and tests/test_homebrew_formula.py already
cover. The drift step runs the real snapshot_drift_issue.py against a local
bare repository standing in for the template upstream and a recording `gh`
stub, so no case reaches the network.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from _workflow_steps import step_command, step_env, step_if, step_run_body, step_with

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"
RCFILE = ".coveragerc.maintenance-audit"

AUDIT_STEP = "Run maintenance audit"
FORMULA_STEP = "Check the Homebrew formula pin"
DRIFT_STEP = "Track snapshot drift as an issue"
SUMMARIZE_STEP = "Summarize"
REPORT_STEP = "Report maintenance-audit coverage"
PACKAGE_STEP = "Package maintenance-audit coverage"
UPLOAD_STEP = "Upload maintenance-audit coverage"

EXECUTED_STEPS = (AUDIT_STEP, FORMULA_STEP, DRIFT_STEP, SUMMARIZE_STEP, REPORT_STEP, PACKAGE_STEP)

HAS_COVERAGE = find_spec("coverage") is not None

# `python3` on the runner is the setup-python interpreter `pip install
# coverage` installed into, so resolving it to the interpreter running this
# suite is what makes the steps' `python3 -m coverage` calls real here. A
# symlink would break a virtualenv -- a venv is found relative to the invoked
# path -- so exec through the real path instead.
PYTHON3_SHIM = f"""#!/usr/bin/env bash
exec "{sys.executable}" "$@"
"""

# Stands in for maintenance_audit.py and homebrew_formula.py. Records the argv
# the step measured it with, prints a line the log assertions can find, and
# exits with whatever the case asked for. Both names are what
# .coveragerc.maintenance-audit scopes, so the real config measures these.
SCRIPT_STUB = """\
import sys
from pathlib import Path

Path(sys.argv[0] + ".argv").write_text("\\n".join(sys.argv[1:]) + "\\n")
print({message!r})
raise SystemExit({code})
"""

# Records every invocation as NUL-separated fields -- the issue body is
# multi-line, so a line-oriented log could not be parsed back into argv --
# and answers the two subcommands the script makes.
GH_STUB = """#!/usr/bin/env bash
{ printf 'CALL\\0'; printf '%s\\0' "$@"; } >> "$STUB_LOG"
case "$1 $2" in
  "issue list") printf '%s\\n' "${GH_ISSUE_LIST:-[]}" ;;
  "issue create") printf '%s\\n' "https://github.com/owner/repo/issues/7" ;;
esac
exit "${GH_EXIT:-0}"
"""

# A revision no local bare repository will ever have at HEAD, so the fixture
# snapshot always trails its upstream.
STALE_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _script_stub(message: str, code: int) -> str:
    return SCRIPT_STUB.format(message=message, code=code)


class _StepHarness(unittest.TestCase):
    """A throwaway project the steps can be run in, plus a `python3` shim."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

        self.bin_dir = self.tmp / "stub-bin"
        self.bin_dir.mkdir()
        python3 = self.bin_dir / "python3"
        python3.write_text(PYTHON3_SHIM)
        python3.chmod(0o755)

        self.log = self.tmp / "stub.log"
        self.project = self.tmp / "project"
        self.project.mkdir()
        # The real config, so the source it scopes, its branch mode and its
        # relative_files setting are the ones under test.
        shutil.copy(ROOT / RCFILE, self.project / RCFILE)

    def step_env_for(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["STUB_LOG"] = str(self.log)
        # The steps must not join this process's own coverage run, if any.
        env.pop("COVERAGE_PROCESS_START", None)
        env.pop("COVERAGE_FILE", None)
        env.update(overrides)
        return env

    def run_body(self, body: str, *, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", body],
            cwd=str(cwd or self.project),
            env=self.step_env_for(**env),
            capture_output=True,
            text=True,
        )

    def write_script(self, name: str, *, message: str, code: int = 0) -> None:
        (self.project / f"{name}.py").write_text(_script_stub(message, code))

    def run_audit_step(self, *, message: str = "Maintenance audit passed.", code: int = 0) -> subprocess.CompletedProcess:
        self.write_script("maintenance_audit", message=message, code=code)
        return self.run_body(step_run_body(WORKFLOW, AUDIT_STEP))

    def run_formula_step(self, *, message: str = "Formula pin is current.", code: int = 0) -> subprocess.CompletedProcess:
        self.write_script("homebrew_formula", message=message, code=code)
        return self.run_body(step_command(WORKFLOW, FORMULA_STEP))

    def audit_log(self) -> str:
        path = self.project / "audit.log"
        return path.read_text() if path.exists() else ""

    def coverage_report(self, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "coverage", "report", f"--rcfile={RCFILE}"],
            cwd=str(cwd or self.project),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout


class HarnessContractTests(unittest.TestCase):
    """What the steps take from the runner, pinned so the harness stays honest."""

    def test_the_executed_bodies_contain_no_workflow_expressions(self) -> None:
        # `${{ }}` is substituted by Actions before the shell sees it, so a
        # body containing one is a body this harness cannot faithfully run.
        # These steps take their inputs through the environment instead.
        for step in EXECUTED_STEPS:
            with self.subTest(step=step):
                self.assertNotIn("${{", step_command(WORKFLOW, step))

    def test_the_reporting_steps_run_even_when_the_audit_fails(self) -> None:
        # A failing audit is exactly when the drift state, the log and the
        # coverage of the run are worth having. Without `always()` every one
        # of them is skipped on the runs that matter most.
        for step in (FORMULA_STEP, DRIFT_STEP, SUMMARIZE_STEP, REPORT_STEP, PACKAGE_STEP, UPLOAD_STEP):
            with self.subTest(step=step):
                self.assertEqual(step_if(WORKFLOW, step), "always()")

    def test_the_audit_is_the_only_step_that_can_fail_the_job(self) -> None:
        # The audit reports repo inconsistencies and is the job's signal; the
        # checks after it are advisory by design (see the comments in the
        # workflow and CONTRIBUTING.md's Coverage section). This holds them to
        # that: each swallows its own failure, and only the audit does not.
        self.assertIsNone(step_if(WORKFLOW, AUDIT_STEP))
        self.assertNotIn("|| true", step_command(WORKFLOW, AUDIT_STEP))
        self.assertIn("|| true", step_command(WORKFLOW, FORMULA_STEP))

    def test_both_github_api_steps_are_given_a_token(self) -> None:
        # The audit queries the API for upstream template HEADs and action
        # tags, and the drift step drives `gh`. Unauthenticated, both meet the
        # 60-requests-an-hour limit and report "unable to check" as though
        # nothing were wrong.
        for step in (AUDIT_STEP, DRIFT_STEP):
            with self.subTest(step=step):
                self.assertEqual(step_env(WORKFLOW, step), {"GH_TOKEN": "${{ github.token }}"})

    def test_the_measured_scripts_are_the_ones_the_config_scopes(self) -> None:
        # `coverage run` measures whatever it is pointed at, but the report is
        # restricted to `source`. A step that measured a script the config
        # does not name would report nothing and say so only as a warning.
        body = step_command(WORKFLOW, AUDIT_STEP) + step_command(WORKFLOW, FORMULA_STEP)
        config = (ROOT / RCFILE).read_text()
        for module in ("maintenance_audit", "homebrew_formula"):
            with self.subTest(module=module):
                self.assertIn(f"{module}.py", body)
                self.assertIn(f"\n    {module}\n", config)

    def test_the_flags_the_steps_pass_are_the_flags_the_scripts_accept(self) -> None:
        # An option renamed in the script leaves this job passing the old one,
        # and argparse exits 2 -- which the audit step turns into a failed
        # weekly job and the formula step swallows entirely.
        for script, flag, step in (
            ("maintenance_audit.py", "--check-action-updates", AUDIT_STEP),
            ("homebrew_formula.py", "--check", FORMULA_STEP),
        ):
            with self.subTest(script=script):
                self.assertIn(flag, step_command(WORKFLOW, step))
                proc = subprocess.run(
                    [sys.executable, str(ROOT / script), "--help"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn(flag, proc.stdout)


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class RunMaintenanceAuditStepTests(_StepHarness):
    """The job's only gate, and the log everything after it appends to."""

    def test_a_passing_audit_is_logged_and_shown(self) -> None:
        proc = self.run_audit_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # `tee`, not `>`: the log is the artifact the summary step renders and
        # the step's own console output is what a maintainer reads first.
        self.assertIn("Maintenance audit passed.", proc.stdout)
        self.assertIn("Maintenance audit passed.", self.audit_log())

    def test_a_failing_audit_fails_the_step_and_is_still_logged(self) -> None:
        # `tee` exits 0 whatever it was piped, so without `set -o pipefail`
        # this is a green job reporting an inconsistency nobody is told about.
        proc = self.run_audit_step(message="Maintenance audit failed:", code=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Maintenance audit failed:", self.audit_log())

    def test_the_audit_starts_the_log_rather_than_appending_to_it(self) -> None:
        # A checkout carries no audit.log, but a re-run in a reused workspace
        # does: appending here would render last week's findings into this
        # week's summary as though they were current.
        (self.project / "audit.log").write_text("stale output from a previous run\n")
        self.run_audit_step()
        self.assertNotIn("stale output", self.audit_log())

    def test_the_audit_is_asked_for_the_action_pin_advisories(self) -> None:
        # --check-action-updates is opt-in and this job is the only thing that
        # opts in; without it pin drift goes back to being found by hand.
        self.run_audit_step()
        self.assertEqual(
            (self.project / "maintenance_audit.py.argv").read_text().split(),
            ["--check-action-updates"],
        )

    def test_the_audit_run_is_measured(self) -> None:
        self.run_audit_step()
        self.assertTrue((self.project / ".coverage").exists())
        self.assertIn("maintenance_audit.py", self.coverage_report())


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class FormulaPinStepTests(_StepHarness):
    """The advisory check that must neither fail the job nor lose the audit's data."""

    def test_the_formula_check_appends_to_the_audits_coverage_data(self) -> None:
        # Without `-a` this run replaces the audit's data file and the tier's
        # whole reason for existing -- one report covering both maintainer
        # scripts from one real run -- is quietly halved.
        self.run_audit_step()
        self.run_formula_step()
        report = self.coverage_report()
        self.assertIn("maintenance_audit.py", report)
        self.assertIn("homebrew_formula.py", report)

    def test_a_failing_formula_check_does_not_fail_the_job(self) -> None:
        self.run_audit_step()
        proc = self.run_formula_step(message="Formula pin is stale.", code=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Formula pin is stale.", self.audit_log())

    def test_the_formula_check_appends_to_the_audit_log(self) -> None:
        # `tee -a`. Overwriting would drop the audit's findings from the job
        # summary, which is the only place a scheduled run reports them.
        self.run_audit_step()
        self.run_formula_step()
        log = self.audit_log()
        self.assertIn("Maintenance audit passed.", log)
        self.assertLess(log.index("Maintenance audit passed."), log.index("Formula pin is current."))


class TrackSnapshotDriftStepTests(_StepHarness):
    """The advisory that reaches the issue list instead of a passing job's summary."""

    def setUp(self) -> None:
        super().setUp()
        gh = self.bin_dir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        # The real script, its import of the audit, and the audit's own import
        # of the pin tables: the step runs the whole chain or it runs nothing.
        for name in ("maintenance_audit.py", "snapshot_drift_issue.py", "atomic_image_builder.py"):
            shutil.copy(ROOT / name, self.project / name)
        self.upstream = self._bare_repo()
        for template in ("containerfile", "bluebuild"):
            source = self.project / "template_snapshots" / template / ".template-source"
            source.parent.mkdir(parents=True)
            source.write_text(f"repo={self.upstream}\nrevision={STALE_REVISION}\n")

    def _bare_repo(self) -> str:
        """An upstream `git ls-remote` can reach without a network."""
        origin = self.tmp / "upstream.git"
        work = self.tmp / "upstream"
        run = {"check": True, "capture_output": True, "text": True}
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], **run)
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], **run)
        (work / "README.md").write_text("upstream template\n")
        git = ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-C", str(work)]
        subprocess.run([*git, "add", "-A"], **run)
        subprocess.run([*git, "commit", "-q", "-m", "initial"], **run)
        subprocess.run([*git, "push", "-q", "origin", "HEAD"], **run)
        return f"file://{origin}"

    def run_drift_step(self, **env: str) -> subprocess.CompletedProcess:
        return self.run_body(
            step_run_body(WORKFLOW, DRIFT_STEP),
            GITHUB_SERVER_URL="https://github.example",
            GITHUB_REPOSITORY="owner/repo",
            GITHUB_RUN_ID="4242",
            **env,
        )

    def gh_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls = []
        for chunk in self.log.read_bytes().split(b"CALL\0"):
            fields = [field.decode() for field in chunk.split(b"\0") if field]
            if fields:
                calls.append(fields)
        return calls

    def test_the_drift_advisory_is_filed_as_an_issue(self) -> None:
        proc = self.run_drift_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.gh_calls()
        self.assertEqual([call[:2] for call in calls], [["issue", "list"], ["issue", "create"]])
        body = calls[1][calls[1].index("--body") + 1]
        self.assertIn("Bundled template snapshot trails upstream HEAD", body)

    def test_the_issue_records_the_run_that_checked(self) -> None:
        # The three runner variables are the whole provenance of the advisory:
        # a reader cannot tell a week-old body from a fresh one without it,
        # and a mistyped one resolves to a URL that 404s with nothing red.
        self.run_drift_step()
        body = self.gh_calls()[1][self.gh_calls()[1].index("--body") + 1]
        self.assertTrue(
            body.rstrip().endswith("Last checked: https://github.example/owner/repo/actions/runs/4242"),
            body,
        )

    def test_the_drift_output_is_appended_to_the_audit_log(self) -> None:
        (self.project / "audit.log").write_text("Maintenance audit passed.\n")
        self.run_drift_step()
        log = self.audit_log()
        self.assertIn("Maintenance audit passed.", log)
        self.assertIn("Opened snapshot drift tracking issue: https://github.com/owner/repo/issues/7", log)

    def test_a_broken_gh_cannot_turn_the_audit_red(self) -> None:
        # The step carries no `|| true`: it relies on the script returning 0
        # whatever GitHub does. That contract is what issue #129 was about --
        # a green audit must not be turned red by issue bookkeeping -- and it
        # is asserted here against the real binary boundary, which
        # tests/test_snapshot_drift_issue.py patches away.
        proc = self.run_drift_step(GH_EXIT="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Could not sync the snapshot drift issue:", self.audit_log())


class SummarizeStepTests(_StepHarness):
    """The only place a scheduled run reports anything to a human."""

    def run_summarize_step(self, summary_seed: str = "") -> tuple[subprocess.CompletedProcess, str]:
        summary = self.tmp / "github_step_summary"
        summary.write_text(summary_seed)
        proc = self.run_body(step_run_body(WORKFLOW, SUMMARIZE_STEP), GITHUB_STEP_SUMMARY=str(summary))
        return proc, summary.read_text()

    def test_the_log_is_rendered_into_the_job_summary(self) -> None:
        (self.project / "audit.log").write_text("Action pin advisories (not failures):\n- actions/checkout\n")
        proc, summary = self.run_summarize_step(summary_seed="## Earlier step\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Appended, not written: clobbering would delete whatever a step
        # before this one reported.
        self.assertTrue(summary.startswith("## Earlier step\n"), summary)
        self.assertIn("## Maintenance audit", summary)
        self.assertIn("- actions/checkout", summary)
        self.assertEqual(summary.count("```"), 2)

    def test_a_missing_log_is_reported_rather_than_summarized_as_nothing(self) -> None:
        # `cat` of a missing file writes to stderr and leaves an empty fenced
        # block, which reads as a clean audit. This step exists on always(),
        # so the step that writes the log may not have run at all.
        proc, summary = self.run_summarize_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(the audit produced no output)", summary)


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class ReportCoverageStepTests(_StepHarness):
    """The advisory tier's number, and the disclaimer that keeps it readable."""

    def run_report_step(self) -> tuple[subprocess.CompletedProcess, str]:
        summary = self.tmp / "github_step_summary"
        summary.touch()
        proc = self.run_body(step_run_body(WORKFLOW, REPORT_STEP), GITHUB_STEP_SUMMARY=str(summary))
        return proc, summary.read_text()

    def test_the_measured_report_reaches_the_job_summary(self) -> None:
        self.run_audit_step()
        self.run_formula_step()
        proc, summary = self.run_report_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## Maintenance-audit coverage", summary)
        for line in self.coverage_report().splitlines():
            with self.subTest(line=line):
                self.assertIn(line, summary)

    def test_the_summary_says_this_number_is_not_the_gated_one(self) -> None:
        # Four other coverage numbers are published by this repo and only the
        # unit one is gated (CONTRIBUTING.md, Coverage). A bare percentage
        # here reads as a coverage regression to anyone who finds it.
        self.run_audit_step()
        _, summary = self.run_report_step()
        self.assertIn("Not gated", summary)
        self.assertIn("real calls to the GitHub API", summary)

    def test_no_coverage_data_is_reported_rather_than_failing_the_step(self) -> None:
        # `coverage report` exits non-zero with no data, and this step runs on
        # always() -- including after an audit step that died before writing
        # any. Without the fallback the summary would end at the heading.
        proc, summary = self.run_report_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(no coverage data collected)", summary)


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class PackageCoverageStepTests(_StepHarness):
    """The artifact layout the documented cross-suite merge depends on."""

    def run_package_step(self) -> subprocess.CompletedProcess:
        return self.run_body(step_run_body(WORKFLOW, PACKAGE_STEP))

    def test_the_artifact_carries_the_report_and_the_raw_data(self) -> None:
        self.run_audit_step()
        self.run_formula_step()
        proc = self.run_package_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        artifact = self.project / "maintenance-audit-coverage"
        self.assertTrue((artifact / "coverage.xml").exists())
        self.assertTrue((artifact / "htmlcov" / "index.html").exists())
        self.assertTrue((artifact / "data" / ".coverage.maintenance-audit").exists())

    def test_the_packaged_data_merges_by_the_documented_recipe(self) -> None:
        # CONTRIBUTING.md's Coverage section documents combining these
        # artifacts from any clone. `coverage combine` only discovers files
        # named `.coverage.*`, and only in the directory it is handed, so both
        # the name and the `data/` directory are load-bearing -- a copy to
        # either wrong one uploads an artifact nobody can merge, with nothing
        # in the run to say so.
        self.run_audit_step()
        self.run_formula_step()
        self.run_package_step()

        merged = self.tmp / "merged"
        (merged / "downloaded").mkdir(parents=True)
        shutil.copytree(self.project / "maintenance-audit-coverage" / "data", merged / "downloaded" / "data")
        shutil.copy(self.project / RCFILE, merged / RCFILE)
        # relative_files = True is what lets the sources be found next to the
        # data rather than at the path the runner measured them at.
        for name in ("maintenance_audit.py", "homebrew_formula.py"):
            shutil.copy(self.project / name, merged / name)

        combine = subprocess.run(
            [sys.executable, "-m", "coverage", "combine", f"--rcfile={RCFILE}", "downloaded/data"],
            cwd=str(merged),
            env=self.step_env_for(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(combine.returncode, 0, combine.stdout + combine.stderr)
        report = self.coverage_report(cwd=merged)
        self.assertIn("maintenance_audit.py", report)
        self.assertIn("homebrew_formula.py", report)

    def test_packaging_survives_a_run_that_measured_nothing(self) -> None:
        # always() again: a workflow edit that broke the audit step outright
        # reaches here with no data file, and a failing package step would
        # replace that diagnosis with a second, unrelated failure.
        proc = self.run_package_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.project / "maintenance-audit-coverage" / "data").is_dir())

    def test_the_upload_takes_the_hidden_data_file_with_it(self) -> None:
        # The data file starts with a dot, and upload-artifact excludes hidden
        # files by default -- so without this input the artifact uploads, the
        # job passes, and only the raw data the merge needs is missing.
        self.assertEqual(
            step_with(WORKFLOW, UPLOAD_STEP),
            {
                "name": "coverage-maintenance-audit",
                "path": "maintenance-audit-coverage/",
                "include-hidden-files": "true",
                "retention-days": "30",
            },
        )

    def test_the_artifact_is_named_the_name_the_docs_tell_people_to_download(self) -> None:
        # The merge recipe is written in terms of the artifact name; a rename
        # here leaves CONTRIBUTING.md describing a download that does not
        # exist, and the workflow is the side that changes.
        name = step_with(WORKFLOW, UPLOAD_STEP)["name"]
        self.assertIn(f"`{name}`", (ROOT / "CONTRIBUTING.md").read_text())


if __name__ == "__main__":
    unittest.main()

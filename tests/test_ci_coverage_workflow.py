"""Execute the coverage-publication `run:` bodies of .github/workflows/ci.yml.

Nothing in the suite ran a line of ci.yml's shell. Several tests read the file
-- tests/test_lint_command_consistency.py compares its shellcheck invocation
with the prose copies, tests/test_workflow_dependencies.py compares its action
pins and tool pins, tests/test_atomic_image_builder.py greps its `pip install`
line and its threshold lookup -- but reading a workflow is not running it, and
the shell these steps contain is the gate every change to this repo passes
through.

Three steps form one chain, and this module executes all three:

* *Report coverage* is the gate. It reads the threshold out of
  `.coverage-thresholds.json` rather than spelling it, which is what keeps the
  gated number and the documented number from drifting -- but a lookup that
  silently produced nothing would gate at nothing, and a green run is what that
  failure looks like. It also writes the job summary *before* the gate, so a
  failing run still shows which lines are missing, and publishes the percentage
  as a step output.
* *Package unit coverage* lays out the artifact. The data file's name and
  directory are a cross-suite contract: CONTRIBUTING.md's documented merge runs
  `coverage combine --rcfile=.coveragerc.e2e coverage-unit/data coverage-e2e/data`,
  and `coverage combine` only discovers files named `.coverage.*`. A copy to the
  wrong name or the wrong directory leaves the artifact uploaded and unmergeable.
* *Publish coverage data* is the only step in this repo that pushes to a branch.
  It keeps the trend by reusing `coverage-data` when it exists and orphaning it
  only on the first publish; a regression that always took the orphan branch
  would discard the whole recorded history behind a green run.

The percentage travels between the first and last of those through a three-part
expression seam -- the step's `id`, the job's `outputs:` block, and the
consuming job's `env:` -- where a rename on any side resolves to the empty
string with no error. `CoveragePercentSeamTests` asserts the three agree, and
`test_an_empty_percentage_publishes_nothing` runs the publish step with the
empty string that a broken seam would deliver.

Each step's shell is extracted from the workflow rather than copied here, so
editing ci.yml re-runs these assertions against the edit. The steps run against
a throwaway coverage project and a local bare repository standing in for
`origin`: no case reaches the network, and `date` is a recording stub so the
trend row can be asserted exactly.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

import coverage_badge
from _workflow_steps import step_env, step_if, step_run_body

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
REPORT_STEP = "Report coverage"
PACKAGE_STEP = "Package unit coverage"
UPLOAD_STEP = "Upload unit coverage"
PUBLISH_STEP = "Publish coverage data"

HAS_COVERAGE = find_spec("coverage") is not None

# A module with a partly exercised branch, so the report the step prints has a
# real percentage and real missing lines rather than 0% or 100%.
SAMPLE_MODULE = '''\
def covered(n):
    if n > 0:
        return "positive"
    return "other"


def never_called(n):
    total = 0
    for i in range(n):
        total += i
    return total
'''

SAMPLE_DRIVER = "import sample\n\nsample.covered(1)\n"

# Mirrors the repo's own .coveragerc in the two settings these steps depend on
# (branch mode and precision), scoped to the throwaway module.
SAMPLE_COVERAGERC = """\
[run]
source =
    sample
relative_files = True
branch = True

[report]
show_missing = True
precision = 0
"""

# `python3` on the runner is the setup-python interpreter that `pip install
# coverage` installed into. Resolving it to the interpreter running this suite
# is what makes the steps' `python3 -m coverage` calls real here. A symlink
# would break a virtualenv -- the venv is found relative to the invoked path --
# so exec through the real path instead.
PYTHON3_SHIM = f"""#!/usr/bin/env bash
exec "{sys.executable}" "$@"
"""

# Records argv so a case can assert the step asked for a UTC date, and prints a
# fixed one so the trend row is exact.
DATE_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
echo "2020-01-02"
"""

STUB_DATE = "2020-01-02"


def _bin_dir(tmp: Path, *, stub_date: bool = False) -> Path:
    """A PATH entry holding the `python3` shim and, optionally, a `date` stub."""
    bin_dir = tmp / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    python3 = bin_dir / "python3"
    python3.write_text(PYTHON3_SHIM)
    python3.chmod(0o755)
    if stub_date:
        date = bin_dir / "date"
        date.write_text(DATE_STUB)
        date.chmod(0o755)
    return bin_dir


def _step_env(tmp: Path, *, stub_date: bool = False, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{_bin_dir(tmp, stub_date=stub_date)}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(tmp / "stub.log")
    # The steps must not pick up this process's own coverage run.
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_FILE", None)
    env.update(extra)
    return env


def _coverage_project(tmp: Path, thresholds: dict) -> Path:
    """A measured throwaway project the coverage steps can be run against."""
    project = tmp / "project"
    project.mkdir()
    (project / "sample.py").write_text(SAMPLE_MODULE)
    (project / "driver.py").write_text(SAMPLE_DRIVER)
    (project / ".coveragerc").write_text(SAMPLE_COVERAGERC)
    (project / ".coverage-thresholds.json").write_text(json.dumps(thresholds) + "\n")
    subprocess.run(
        [sys.executable, "-m", "coverage", "run", "driver.py"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=True,
    )
    return project


def _measured_total(project: Path) -> str:
    """The percentage coverage itself reports, so no case hardcodes one."""
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--format=total"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def run_report_step(
    tmp: Path,
    *,
    thresholds: dict,
    summary_seed: str = "",
) -> tuple[subprocess.CompletedProcess, dict[str, str], str, Path]:
    """Run *Report coverage* over a throwaway project.

    Returns the process, the parsed `$GITHUB_OUTPUT`, the text of
    `$GITHUB_STEP_SUMMARY`, and the project, so a case can compare the reported
    percentage against the one coverage measures rather than a literal.
    """
    project = _coverage_project(tmp, thresholds)
    output_file = tmp / "github_output"
    output_file.touch()
    summary_file = tmp / "github_step_summary"
    summary_file.write_text(summary_seed)

    proc = subprocess.run(
        ["bash", "-c", step_run_body(CI_WORKFLOW, REPORT_STEP)],
        cwd=str(project),
        env=_step_env(tmp, GITHUB_OUTPUT=str(output_file), GITHUB_STEP_SUMMARY=str(summary_file)),
        capture_output=True,
        text=True,
    )
    outputs = {}
    for line in output_file.read_text().splitlines():
        if line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return proc, outputs, summary_file.read_text(), project


def _init_origin(tmp: Path) -> tuple[Path, Path]:
    """A bare repository and a checkout of it holding the real badge script."""
    origin = tmp / "origin.git"
    run = {"check": True, "capture_output": True, "text": True}
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], **run)
    checkout = tmp / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], **run)
    (checkout / "coverage_badge.py").write_bytes((ROOT / "coverage_badge.py").read_bytes())
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-C", str(checkout)]
    subprocess.run([*git, "add", "coverage_badge.py"], **run)
    subprocess.run([*git, "commit", "-q", "-m", "initial"], **run)
    return origin, checkout


def run_publish_step(
    tmp: Path,
    checkout: Path,
    *,
    percent: str,
) -> subprocess.CompletedProcess:
    """Run *Publish coverage data* in a checkout whose `origin` is local."""
    sha = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return subprocess.run(
        ["bash", "-c", step_run_body(CI_WORKFLOW, PUBLISH_STEP)],
        cwd=str(checkout),
        env=_step_env(tmp, stub_date=True, COVERAGE_PERCENT=percent, GITHUB_SHA=sha),
        capture_output=True,
        text=True,
    )


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _published(origin: Path, path: str) -> str:
    return _git_out(origin, "show", f"coverage-data:{path}")


class HarnessContractTests(unittest.TestCase):
    """What the steps read from the runner, pinned so the harness stays honest."""

    def test_the_executed_bodies_contain_no_workflow_expressions(self) -> None:
        # `${{ }}` is substituted by Actions before the shell ever sees it, so a
        # body containing one is a body this harness cannot faithfully run. Each
        # of these steps takes its inputs through the environment instead.
        for step in (REPORT_STEP, PACKAGE_STEP, PUBLISH_STEP):
            with self.subTest(step=step):
                self.assertNotIn("${{", step_run_body(CI_WORKFLOW, step))

    def test_the_coverage_steps_declare_no_env_block(self) -> None:
        # Everything they read -- GITHUB_OUTPUT, GITHUB_STEP_SUMMARY -- comes
        # from the runner. A variable moved into an `env:` block would still be
        # picked up from this test's own environment and pass for the wrong
        # reason, so assert the block is absent rather than assuming it.
        self.assertEqual(step_env(CI_WORKFLOW, REPORT_STEP), {})
        self.assertEqual(step_env(CI_WORKFLOW, PACKAGE_STEP), {})

    def test_the_unit_artifact_survives_a_failed_gate(self) -> None:
        # The packaging and upload steps both carry `if: always()` on purpose:
        # the run that dropped below the threshold is the one whose line-level
        # data someone wants. Without it the gate failure would also delete the
        # evidence for it.
        self.assertEqual(step_if(CI_WORKFLOW, PACKAGE_STEP), "always()")
        self.assertEqual(step_if(CI_WORKFLOW, UPLOAD_STEP), "always()")


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class ReportCoverageStepTests(unittest.TestCase):
    """The gate, the summary, and the percentage it publishes."""

    def test_the_gate_passes_and_publishes_the_measured_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc, outputs, summary, project = run_report_step(tmp_path, thresholds={"gated": {"unit": 10}})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # Compared against what coverage itself reports: a hardcoded number
            # here would pass even if the step published a stale or empty value.
            self.assertEqual(outputs.get("percent"), _measured_total(project))
            self.assertIn("## Coverage", summary)
            self.assertIn("sample.py", summary)

    def test_the_gate_enforces_the_number_in_the_thresholds_file(self) -> None:
        # The threshold is read from .coverage-thresholds.json rather than
        # spelled in the workflow. A step that ignored the file -- or passed a
        # constant -- would pass this project at any setting, so run the same
        # measurement under a threshold above it and below it.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc, outputs, summary, _ = run_report_step(tmp_path, thresholds={"gated": {"unit": 99}})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("99", summary)
            # The percentage is the gate's verdict as much as the exit code is:
            # publishing it from a failing run would badge a failing commit.
            self.assertNotIn("percent", outputs)

    def test_a_failing_gate_still_reports_which_lines_are_missing(self) -> None:
        # The summary is written before the gate deliberately, so a red run is
        # diagnosable from the summary alone. Reordering the two would leave the
        # failing run with nothing but an exit code.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc, _, summary, _ = run_report_step(tmp_path, thresholds={"gated": {"unit": 99}})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("sample.py", summary)
            self.assertIn("Missing", summary)

    def test_a_thresholds_file_without_the_gated_key_fails_the_step(self) -> None:
        # `jq -er` is what makes a renamed or removed key loud. Without -e the
        # lookup yields "null", `--fail-under=null` is not a number coverage
        # accepts, and with a tolerant shell the gate would silently stop
        # gating. Nothing may be published either.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc, outputs, summary, _ = run_report_step(tmp_path, thresholds={"gated": {}})
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(outputs, {})
            self.assertEqual(summary, "")

    def test_the_summary_is_appended_to_whatever_earlier_steps_wrote(self) -> None:
        # $GITHUB_STEP_SUMMARY accumulates across a job. A truncating redirect
        # here would erase an earlier step's section, which no assertion on this
        # step's own output would notice.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc, _, summary, _ = run_report_step(
                tmp_path,
                thresholds={"gated": {"unit": 10}},
                summary_seed="## An earlier step\n",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(summary.startswith("## An earlier step\n"), summary)
            self.assertIn("## Coverage", summary)

    def test_the_summary_quotes_the_gate_and_where_it_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, _, summary, _ = run_report_step(tmp_path, thresholds={"gated": {"unit": 10}})
            self.assertIn("Gate: 10% (.coverage-thresholds.json)", summary)
            # Fenced, so the aligned report survives Markdown rendering.
            self.assertEqual(summary.count("```"), 2, summary)


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class PackageUnitCoverageStepTests(unittest.TestCase):
    """The artifact layout the documented cross-suite merge depends on."""

    def _package(self, tmp_path: Path) -> Path:
        project = _coverage_project(tmp_path, thresholds={"gated": {"unit": 10}})
        proc = subprocess.run(
            ["bash", "-c", step_run_body(CI_WORKFLOW, PACKAGE_STEP)],
            cwd=str(project),
            env=_step_env(tmp_path),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return project

    def test_it_writes_the_xml_the_html_and_the_raw_data_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._package(Path(tmp))
            self.assertTrue((project / "unit-coverage/coverage.xml").is_file())
            self.assertTrue((project / "unit-coverage/htmlcov/index.html").is_file())
            self.assertTrue((project / "unit-coverage/data/.coverage.unit").is_file())
            # Copied, not moved: the gate step and this one both read .coverage,
            # and `if: always()` means this can run either side of a failure.
            self.assertTrue((project / ".coverage").is_file())

    def test_the_packaged_data_file_still_carries_the_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._package(Path(tmp))
            packaged = project / "unit-coverage/data/.coverage.unit"
            proc = subprocess.run(
                [sys.executable, "-m", "coverage", "report", "--data-file", str(packaged), "--format=total"],
                cwd=str(project),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(proc.stdout.strip(), _measured_total(project))

    def test_the_packaged_data_file_is_one_coverage_combine_would_find(self) -> None:
        # CONTRIBUTING.md documents the merge as `coverage combine
        # --rcfile=.coveragerc.e2e coverage-unit/data coverage-e2e/data`, and
        # combine only picks up files named `.coverage.*` from a directory. A
        # copy named `coverage.unit` or `.coverage` would upload fine and merge
        # never.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._package(Path(tmp))
            data_dir = project / "unit-coverage/data"
            self.assertEqual([p.name for p in sorted(data_dir.iterdir())], [".coverage.unit"])
            self.assertTrue(
                any(p.name.startswith(".coverage.") for p in data_dir.glob(".coverage.*")),
                "combine discovers data files by the `.coverage.*` name",
            )

    def test_the_packaged_data_file_is_in_branch_mode(self) -> None:
        # coverage refuses outright to combine branch data with statement data,
        # so this artifact is only mergeable with the e2e one while both configs
        # keep `branch = True` -- a claim CONTRIBUTING.md makes and nothing else
        # checks against the file that actually gets uploaded.
        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as tmp:
            project = self._package(Path(tmp))
            data = CoverageData(basename=str(project / "unit-coverage/data/.coverage.unit"))
            data.read()
            self.assertTrue(data.has_arcs())

    def test_the_uploaded_directory_is_the_one_the_step_creates(self) -> None:
        # The artifact's name and path are the other half of the documented
        # download: `coverage-unit` unpacks to a directory holding `data/`. The
        # upload step is an action, so its `with:` block is read rather than run.
        lines = CI_WORKFLOW.read_text().splitlines()
        start = lines.index(f"      - name: {UPLOAD_STEP}")
        block = "\n".join(lines[start : start + 10])
        self.assertIn("name: coverage-unit", block)
        self.assertIn("path: unit-coverage/", block)
        # Hidden files are what the raw data file is; without this the artifact
        # would contain the XML and HTML and silently drop `.coverage.unit`.
        self.assertIn("include-hidden-files: true", block)
        self.assertIn("coverage-unit/data", (ROOT / "CONTRIBUTING.md").read_text())


class PublishCoverageDataStepTests(unittest.TestCase):
    """The only step here that writes to a branch."""

    def test_the_first_publish_orphans_the_branch_and_pushes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin, checkout = _init_origin(tmp_path)
            proc = run_publish_step(tmp_path, checkout, percent="87")
            self.assertEqual(proc.returncode, 0, proc.stderr)

            sha = _git_out(checkout, "rev-parse", "HEAD")
            # Orphaned: the branch carries coverage history only, not the
            # repository's, so it never grows the main history a second time.
            self.assertEqual(_git_out(origin, "rev-list", "--count", "coverage-data"), "1")
            self.assertEqual(_published(origin, "coverage-trend.csv"), f"{STUB_DATE},{sha},87")
            # The badge payload is coverage_badge.py's to define; compare against
            # it rather than restating the shape, so the step is asserted to pass
            # the percentage through rather than to produce some JSON.
            self.assertEqual(
                json.loads(_published(origin, "coverage-unit.json")),
                coverage_badge.badge_payload(87),
            )
            # 12 characters, via bash's `${VAR::12}` -- the message is the only
            # place the commit names the revision it measured.
            self.assertEqual(
                _git_out(origin, "log", "-1", "--format=%s", "coverage-data"),
                f"chore: record unit coverage for {sha[:12]}",
            )
            self.assertEqual(
                _git_out(origin, "log", "-1", "--format=%an <%ae>", "coverage-data"),
                "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
            )

    def test_it_asks_for_a_utc_date(self) -> None:
        # A local date would make the trend non-monotonic across runners. The
        # stub records argv so the `-u` cannot be dropped unnoticed.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, checkout = _init_origin(tmp_path)
            proc = run_publish_step(tmp_path, checkout, percent="87")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = [line.split("\t")[:-1] for line in (tmp_path / "stub.log").read_text().splitlines()]
            self.assertEqual(calls, [["-u", "+%Y-%m-%d"]])

    def test_a_second_publish_extends_the_branch_instead_of_replacing_it(self) -> None:
        # The fetch/orphan fork is the whole trend: taking the orphan path on a
        # repository that already has the branch would push a single-row CSV
        # over the entire recorded history, and the push would still be green.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin, checkout = _init_origin(tmp_path)
            self.assertEqual(run_publish_step(tmp_path, checkout, percent="87").returncode, 0)
            first = _git_out(origin, "rev-parse", "coverage-data")

            proc = run_publish_step(tmp_path, checkout, percent="95")
            self.assertEqual(proc.returncode, 0, proc.stderr)

            sha = _git_out(checkout, "rev-parse", "HEAD")
            self.assertEqual(
                _published(origin, "coverage-trend.csv").splitlines(),
                [f"{STUB_DATE},{sha},87", f"{STUB_DATE},{sha},95"],
            )
            self.assertEqual(_git_out(origin, "rev-parse", "coverage-data^"), first)
            self.assertEqual(
                json.loads(_published(origin, "coverage-unit.json")),
                coverage_badge.badge_payload(95),
            )

    def test_an_empty_percentage_publishes_nothing(self) -> None:
        # What a broken `steps.coverage.outputs.percent` seam delivers: the
        # expression resolves to the empty string rather than failing. The step
        # has to refuse it -- coverage_badge.py rejects a non-integer percentage
        # -- because a badge built from "" would read as a real measurement.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin, checkout = _init_origin(tmp_path)
            proc = run_publish_step(tmp_path, checkout, percent="")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(_git_out(origin, "branch", "--list", "coverage-data"), "")

    def test_the_worktree_is_removed_whether_the_step_succeeds_or_fails(self) -> None:
        # The trap is what keeps a second run in the same workspace from hitting
        # "already exists" on a path whose directory was removed with it -- and a
        # failed run is exactly when the worktree is left behind.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, checkout = _init_origin(tmp_path)
            for percent in ("87", ""):
                with self.subTest(percent=percent):
                    run_publish_step(tmp_path, checkout, percent=percent)
                    listed = _git_out(checkout, "worktree", "list", "--porcelain")
                    self.assertEqual(
                        [line for line in listed.splitlines() if line.startswith("worktree ")],
                        [f"worktree {checkout}"],
                    )

    def test_the_publishing_job_runs_only_for_a_push_to_main(self) -> None:
        # Execution cannot show this: the gate is on the job, and a PR branch
        # whose run published a badge would overwrite main's recorded trend with
        # an unmerged measurement.
        body = CI_WORKFLOW.read_text()
        job = body[body.index("  publish-coverage:") : body.index("  container-build:")]
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", job)
        # Serialised, because two pushes racing to the same branch make one of
        # the two pushes fail rather than queue.
        self.assertIn("group: coverage-data", job)
        self.assertIn("contents: write", job)


class CoveragePercentSeamTests(unittest.TestCase):
    """The three names the percentage passes through between the two jobs."""

    def test_the_step_writes_the_output_the_job_exports_and_the_job_consumes(self) -> None:
        body = CI_WORKFLOW.read_text()
        lines = body.splitlines()

        # 1. The shell's key.
        shell = step_run_body(CI_WORKFLOW, REPORT_STEP)
        written = re.findall(r'echo "([A-Za-z_]+)=.*" >> "\$GITHUB_OUTPUT"', shell)
        self.assertEqual(written, ["percent"])

        # 2. The step id the export names it by.
        start = lines.index(f"      - name: {REPORT_STEP}")
        self.assertEqual(lines[start + 1].strip(), "id: coverage")

        # 3. The job output, and the consuming job's env. A rename on any of the
        # three resolves to the empty string in every expression downstream,
        # with no error anywhere -- so they are asserted together.
        self.assertIn("unit_coverage: ${{ steps.coverage.outputs.percent }}", body)
        self.assertEqual(
            step_env(CI_WORKFLOW, PUBLISH_STEP),
            {"COVERAGE_PERCENT": "${{ needs.test.outputs.unit_coverage }}"},
        )
        publish_job = body[body.index("  publish-coverage:") : body.index("  container-build:")]
        self.assertIn("needs: test", publish_job)


if __name__ == "__main__":
    unittest.main()

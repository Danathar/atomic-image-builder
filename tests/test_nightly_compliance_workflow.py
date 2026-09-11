"""Execute the `run:` bodies of .github/workflows/nightly-compliance.yml.

This is the only job that rebuilds `main` from scratch on a schedule, so it is
the only thing in the repo that notices the world moving underneath a green
`main`. Nothing ran a line of its shell. Three tests read the file --
tests/test_workflow_dependencies.py compares its pins and its tool list with
the other workflows', tests/test_atomic_image_builder.py greps it for the
threshold lookup -- but reading a workflow is not running it, and a scheduled
job's failure mode is silence: a dropped or weakened run produces no red.

Three steps carry a `run: |` block and this module executes all three:

* *Install test tooling* is what makes this job's gate comparable to ci.yml's.
  The suite skips tests when `actionlint` or `just` are missing, and a skipped
  test is still counted in "Ran N tests" -- so a tarball that arrives corrupt,
  a `tar` member renamed, or a `$GITHUB_PATH` line that publishes the wrong
  directory all leave the job reporting the same gate under the same name
  while checking less. Two cases run the body: one proves the checksum stops a
  tampered download before `tar` ever sees it, the other proves that after the
  checksum passes both binaries land in the directory the step publishes.
* *Check the coverage gate* reads the threshold out of
  `.coverage-thresholds.json` rather than spelling it, and re-measures by
  running the suite rather than trusting a data file left behind by the step
  before it. A gate that read stale data would pass on a run that never
  measured anything.
* *Summarize* carries `if: always()`, so the one thing a failed nightly leaves
  behind is its explanation of what a failure here means.

`Run tests`, `Build the image from scratch`, `Run the end-to-end suite against
it` and `Audit local consistency` are single commands rather than blocks:
`UnitSuiteInvocationTests` pins the first against every other copy of it in the
repo, and the remaining three build and run a container, which is the nightly
job's own work rather than something this suite can stand in for.

Bodies run under a plain `bash -c`, not `bash -e`, so the `set -euo pipefail`
each body writes for itself is the thing under test rather than something the
harness supplies. Downloads are served by a `curl` stub from local payloads and
`pip` is a recording stub: no case reaches the network.
"""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from _workflow_steps import step_env, step_if, step_run_body

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
NIGHTLY = WORKFLOWS / "nightly-compliance.yml"
TOOLING_STEP = "Install test tooling"
GATE_STEP = "Check the coverage gate"
SUMMARIZE_STEP = "Summarize"

HAS_COVERAGE = find_spec("coverage") is not None

# The unit suite the nightly gate runs, as every copy of it in the repo writes
# it. Compared for equality rather than by substring: `-t .` appended in one
# place changes which package the suite imports without changing the substring
# tests/test_workflow_dependencies.py looks for.
UNIT_SUITE = "unittest discover -s tests"

# The skipUnless gates that decide whether this suite measures what it claims
# to: a tool name looked up with shutil.which. Written as a pattern rather than
# quoted in a comment so this file's own prose cannot match it.
_SKIP_GATE = re.compile(r"skipUnless\(\s*shutil\.which\(\s*\"([^\"]+)\"")

# Present on every GitHub runner and in every container this repo builds, so
# there is nothing for a workflow to install and nothing to assert.
ALWAYS_PRESENT = {"bash"}

PYTHON3_SHIM = f"""#!/usr/bin/env bash
exec "{sys.executable}" "$@"
"""

# Records argv and writes the payload staged for the requested `-o` path, so a
# case chooses what the download delivers without reaching the network.
CURL_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$0" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
dest=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then dest="$arg"; fi
  prev="$arg"
done
if [ -z "$dest" ]; then
  echo "curl stub: no -o destination in: $*" >&2
  exit 2
fi
payload="$STUB_PAYLOADS/$(basename "$dest")"
if [ ! -f "$payload" ]; then
  echo "curl stub: no payload staged for $dest" >&2
  exit 22
fi
cp "$payload" "$dest"
"""

PIP_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$0" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
"""

# Records the expected line the step pipes in and confirms the file it names is
# there, without comparing digests. Used only by the case that tests what
# happens *after* the checksum passes -- no file can be produced locally whose
# digest matches the pinned one, and the case below it uses the real
# `sha256sum` to prove the gate is load-bearing.
PERMISSIVE_SHA256SUM_STUB = """#!/usr/bin/env bash
line="$(cat)"
printf '%s\\n' "$line" >> "$SHA_LOG"
path="${line#*  }"
if [ ! -f "$path" ]; then
  echo "sha256sum stub: $path does not exist" >&2
  exit 1
fi
"""


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def _tool_tarball(path: Path, member: str) -> None:
    """A gzipped tarball holding one executable named *member* at its root."""
    with tempfile.TemporaryDirectory() as staging:
        binary = Path(staging) / member
        binary.write_text(f"#!/usr/bin/env bash\necho {member}\n")
        binary.chmod(0o755)
        with tarfile.open(path, "w:gz") as archive:
            archive.add(binary, arcname=member)


class _ToolingRun:
    """The result of executing *Install test tooling* against staged payloads."""

    def __init__(self, proc: subprocess.CompletedProcess, runner_temp: Path, github_path: Path, sha_log: Path, stub_log: Path) -> None:
        self.proc = proc
        self.bin_dir = runner_temp / "bin"
        self.published = github_path.read_text().splitlines()
        self.checksum_lines = sha_log.read_text().splitlines() if sha_log.exists() else []
        self.commands = [line.rstrip("\t").split("\t") for line in stub_log.read_text().splitlines()]


def run_tooling_step(tmp: Path, *, payloads: dict[str, bytes] | None = None, real_sha256sum: bool) -> _ToolingRun:
    """Run *Install test tooling* with `curl` serving local payloads.

    *payloads* maps a download's file name to the bytes the stub delivers for
    it; the default stages real tarballs holding the two binaries the step
    extracts. With *real_sha256sum* the pinned digests are checked for real, so
    any staged payload fails verification -- which is what the tampered-download
    case wants.
    """
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir()
    payload_dir = tmp / "payloads"
    payload_dir.mkdir()
    if payloads is None:
        _tool_tarball(payload_dir / "actionlint.tar.gz", "actionlint")
        _tool_tarball(payload_dir / "just.tar.gz", "just")
    else:
        for name, data in payloads.items():
            (payload_dir / name).write_bytes(data)

    bin_dir = tmp / "stub-bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "python3", PYTHON3_SHIM)
    _write_stub(bin_dir, "curl", CURL_STUB)
    _write_stub(bin_dir, "pip", PIP_STUB)
    sha_log = tmp / "sha.log"
    if not real_sha256sum:
        _write_stub(bin_dir, "sha256sum", PERMISSIVE_SHA256SUM_STUB)

    github_path = tmp / "github_path"
    github_path.touch()
    stub_log = tmp / "stub.log"
    stub_log.touch()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["RUNNER_TEMP"] = str(runner_temp)
    env["GITHUB_PATH"] = str(github_path)
    env["STUB_PAYLOADS"] = str(payload_dir)
    env["STUB_LOG"] = str(stub_log)
    env["SHA_LOG"] = str(sha_log)

    proc = subprocess.run(
        ["bash", "-c", step_run_body(NIGHTLY, TOOLING_STEP)],
        cwd=str(tmp),
        env=env,
        capture_output=True,
        text=True,
    )
    return _ToolingRun(proc, runner_temp, github_path, sha_log, stub_log)


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

# Exercises one branch of one function, so the project the gate measures has a
# real percentage rather than 0 or 100.
SAMPLE_TEST = '''\
import unittest

import sample


class SampleTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(sample.covered(1), "positive")
'''

# Everything the suite leaves out, so a `.coverage` seeded from this reads 100%
# and a gate that trusted it instead of re-measuring would pass.
FULL_DRIVER = '''\
import sample

sample.covered(1)
sample.covered(-1)
sample.never_called(2)
'''

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


def _gate_project(tmp: Path, *, threshold: object) -> Path:
    """A throwaway project the gate step can measure and gate."""
    project = tmp / "project"
    (project / "tests").mkdir(parents=True)
    (project / "sample.py").write_text(SAMPLE_MODULE)
    (project / "tests/test_sample.py").write_text(SAMPLE_TEST)
    (project / "driver.py").write_text(FULL_DRIVER)
    (project / ".coveragerc").write_text(SAMPLE_COVERAGERC)
    gated = {} if threshold is None else {"unit": threshold}
    (project / ".coverage-thresholds.json").write_text(json.dumps({"gated": gated}) + "\n")
    return project


def _gate_env(tmp: Path) -> dict[str, str]:
    bin_dir = tmp / "gate-bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir, "python3", PYTHON3_SHIM)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # The step must measure its own project, not inherit this suite's run.
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_FILE", None)
    return env


def run_gate_step(project: Path, tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", step_run_body(NIGHTLY, GATE_STEP)],
        cwd=str(project),
        env=_gate_env(tmp),
        capture_output=True,
        text=True,
    )


def _total(project: Path) -> int:
    """The percentage coverage reports for whatever `.coverage` now holds."""
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--format=total"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


class HarnessContractTests(unittest.TestCase):
    """What the executed steps take from the runner, pinned so the harness stays honest."""

    def test_the_executed_bodies_contain_no_workflow_expressions(self) -> None:
        # `${{ }}` is substituted by Actions before the shell sees it, so a body
        # containing one is a body this harness cannot faithfully run.
        for step in (TOOLING_STEP, GATE_STEP, SUMMARIZE_STEP):
            with self.subTest(step=step):
                self.assertNotIn("${{", step_run_body(NIGHTLY, step))

    def test_the_executed_steps_declare_no_env_block(self) -> None:
        # Everything they read -- RUNNER_TEMP, GITHUB_PATH, GITHUB_STEP_SUMMARY
        # -- comes from the runner. A value moved into an `env:` block would
        # still be picked up from this process's environment and pass for the
        # wrong reason, so assert the block is absent rather than assume it.
        for step in (TOOLING_STEP, GATE_STEP, SUMMARIZE_STEP):
            with self.subTest(step=step):
                self.assertEqual(step_env(NIGHTLY, step), {})

    def test_the_gating_bodies_abort_on_the_first_failure(self) -> None:
        # These run under a plain `bash -c` here, and on the runner under a
        # shell whose `-e` is a default someone can override at the job level.
        # The two bodies that gate something say so themselves, which is what
        # makes a failed checksum and a failed threshold stop the step in both
        # places.
        for step in (TOOLING_STEP, GATE_STEP):
            with self.subTest(step=step):
                self.assertTrue(
                    step_run_body(NIGHTLY, step).startswith("set -euo pipefail\n"),
                    f"{step} does not set -euo pipefail",
                )

    def test_the_summary_survives_a_failed_nightly(self) -> None:
        # A nightly failure means something outside this repository moved, and
        # the summary is the only place that says so. Without `always()` the
        # failing run -- the one worth explaining -- is the one that explains
        # nothing.
        self.assertEqual(step_if(NIGHTLY, SUMMARIZE_STEP), "always()")


class InstallTestToolingStepTests(unittest.TestCase):
    """The downloads, the checksums, and the directory the tools have to land in."""

    def test_a_tampered_download_is_never_unpacked(self) -> None:
        # The pinned digests are the only thing between this job and whatever a
        # release URL happens to serve tonight. With the real `sha256sum`, a
        # payload that is not the pinned tarball has to stop the step before
        # `tar` runs -- and nothing may reach `$GITHUB_PATH`, because a job that
        # published a directory holding an unverified binary and then ran the
        # suite through it is worse than one that failed.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(
                Path(tmp),
                payloads={"actionlint.tar.gz": b"not the pinned tarball\n"},
                real_sha256sum=True,
            )
            self.assertNotEqual(run.proc.returncode, 0, run.proc.stdout)
            self.assertFalse((run.bin_dir / "actionlint").exists())
            self.assertEqual(run.published, [])

    def test_both_tools_land_in_the_directory_the_step_publishes(self) -> None:
        # Past the checksum, three things have to agree: the `tar` member name,
        # the `-C` directory, and the line appended to `$GITHUB_PATH`. Any one
        # of them wrong leaves the binaries unreachable, the tests that need
        # them skipped, and the job green.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            self.assertEqual(run.published, [str(run.bin_dir)])
            for tool in ("actionlint", "just"):
                with self.subTest(tool=tool):
                    binary = Path(run.published[0]) / tool
                    self.assertTrue(binary.is_file(), f"{tool} is not on the published PATH entry")
                    self.assertTrue(os.access(binary, os.X_OK), f"{tool} is not executable")

    def test_each_checksum_is_piped_against_the_file_just_downloaded(self) -> None:
        # The realistic mistake is copying the block above and leaving the
        # previous file name in the `echo`. tests/test_workflow_dependencies.py
        # catches that by reading the text; this catches the other half, that
        # the path in the checksum line is one that exists by the time the line
        # runs. A check against a path that was never written reports success
        # on some `sha256sum` builds and nothing on others.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            downloaded = [
                argv[argv.index("-o") + 1]
                for argv in run.commands
                if argv[0].endswith("curl") and "-o" in argv
            ]
            self.assertEqual(len(downloaded), 2, run.commands)
            checked = []
            for line in run.checksum_lines:
                digest, _, path = line.partition("  ")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                checked.append(path)
            self.assertEqual(checked, downloaded)

    def test_nothing_is_fetched_over_plain_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            urls = [arg for argv in run.commands for arg in argv if arg.startswith("http")]
            self.assertTrue(urls)
            for url in urls:
                with self.subTest(url=url):
                    self.assertTrue(url.startswith("https://"), url)

    def test_the_download_fails_the_step_rather_than_saving_an_error_page(self) -> None:
        # Without `-f`, curl writes a 404 body to the output path and exits 0.
        # The checksum would catch it, but the failure would read as a corrupt
        # release rather than a moved URL.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            curls = [argv for argv in run.commands if argv[0].endswith("curl")]
            self.assertEqual(len(curls), 2, run.commands)
            for argv in curls:
                with self.subTest(argv=argv):
                    flags = "".join(arg for arg in argv if arg.startswith("-") and not arg.startswith("--"))
                    self.assertIn("f", flags, "curl may not save the body of a failed request")

    def test_the_step_installs_the_measurement_tool_before_it_is_used(self) -> None:
        # The gate step two steps later runs `python3 -m coverage`, which is not
        # on a runner by default. Nothing else in this job installs it.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            installs = [argv for argv in run.commands if argv[0].endswith("pip")]
            self.assertEqual(len(installs), 1, run.commands)
            self.assertEqual(installs[0][1], "install")
            self.assertTrue(
                any(arg.startswith("coverage==") for arg in installs[0]),
                installs[0],
            )


class SuiteToolingIsNotUnderstatedTests(unittest.TestCase):
    """Every tool the suite skips without has to be one this job installs.

    The job's comment says `actionlint` and `just` are there "because the unit
    suite skips tests without them, and a skipped test is still counted in `Ran
    N tests`". tests/test_workflow_dependencies.py enforces that against a
    hand-written set of two repositories -- so the day a test starts skipping
    on a third tool, the list does not grow, this job installs two of three,
    and it reports ci.yml's gate under ci.yml's name while running less of it.
    Nothing joins that list to the suite's own skip gates. This does, by
    reading the gates out of the suite and comparing them with what running the
    step actually puts on the PATH.
    """

    def _required_tools(self) -> set[str]:
        gates: set[str] = set()
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            gates.update(_SKIP_GATE.findall(path.read_text()))
        return gates - ALWAYS_PRESENT

    def test_the_suite_gates_on_at_least_one_installed_tool(self) -> None:
        # If the regex above ever stops matching, every assertion in this class
        # passes by finding nothing. Fail loudly instead.
        self.assertTrue(self._required_tools(), "no `skipUnless(shutil.which(...))` gate found")

    def test_the_nightly_job_installs_every_tool_the_suite_skips_without(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = run_tooling_step(Path(tmp), real_sha256sum=False)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            available = {path.name for path in Path(run.published[0]).iterdir()}
        missing = sorted(self._required_tools() - available)
        self.assertEqual(
            missing,
            [],
            "the unit suite skips tests without these, and this job installs none of them, "
            "so its run of the gate checks less than ci.yml's under the same name",
        )


@unittest.skipUnless(HAS_COVERAGE, "the step runs `python3 -m coverage`")
class CoverageGateStepTests(unittest.TestCase):
    """The gate: where the number comes from, and what it is measured against."""

    def test_the_gate_passes_a_project_that_meets_the_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _gate_project(Path(tmp), threshold=10)
            proc = run_gate_step(project, Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("sample.py", proc.stdout)

    def test_the_gate_fails_a_project_below_the_threshold(self) -> None:
        # Run against the same project at a threshold above what it measures. A
        # gate that passed a constant, or dropped `--fail-under`, would pass
        # both this and the case above.
        with tempfile.TemporaryDirectory() as tmp:
            project = _gate_project(Path(tmp), threshold=99)
            proc = run_gate_step(project, Path(tmp))
            self.assertNotEqual(proc.returncode, 0, proc.stdout)

    def test_a_thresholds_file_without_the_gated_key_fails_the_step(self) -> None:
        # `jq -er` is what makes a renamed or removed key loud, and it has to be
        # loud at the lookup. Without -e the lookup yields the string "null",
        # the step runs the whole suite, and only then does coverage reject
        # `--fail-under=null` -- a half-hour of nightly runtime spent to reach
        # an error the first line could have raised, and one that reads as a
        # coverage failure rather than a missing key. So the step must stop
        # before it measures anything: no data file may exist afterwards.
        with tempfile.TemporaryDirectory() as tmp:
            project = _gate_project(Path(tmp), threshold=None)
            proc = run_gate_step(project, Path(tmp))
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertFalse(
                (project / ".coverage").exists(),
                "the suite ran even though the threshold lookup had already failed",
            )

    def test_the_gate_measures_this_run_rather_than_a_leftover_data_file(self) -> None:
        # The whole point of the job is to re-measure `main` tonight. Seed a
        # `.coverage` that reads 100% -- what a previous run, or a checked-in
        # artifact, would look like -- and gate above what the suite actually
        # reaches. Trusting the file passes; running the suite fails.
        with tempfile.TemporaryDirectory() as tmp:
            project = _gate_project(Path(tmp), threshold=95)
            subprocess.run(
                [sys.executable, "-m", "coverage", "run", "driver.py"],
                cwd=str(project),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(_total(project), 100, "the seeded data file should read 100%")
            proc = run_gate_step(project, Path(tmp))
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertLess(_total(project), 95, "the step did not re-measure the project")

    def test_the_gate_reports_before_it_fails(self) -> None:
        # A scheduled job nobody is watching is diagnosed from its log alone. A
        # threshold failure that printed only an exit code would say the gate
        # dropped without saying which lines dropped it.
        with tempfile.TemporaryDirectory() as tmp:
            project = _gate_project(Path(tmp), threshold=99)
            proc = run_gate_step(project, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("sample.py", proc.stdout)
            self.assertIn("Missing", proc.stdout)


class UnitSuiteInvocationTests(unittest.TestCase):
    """The nightly gate is only ci.yml's gate while both discover the same tests."""

    def test_every_copy_of_the_unit_suite_command_is_written_identically(self) -> None:
        # `unittest discover` resolves imports relative to its top-level
        # directory, so `-t .` or a changed `-s` makes two invocations that
        # look alike collect different tests. The check in
        # tests/test_workflow_dependencies.py is a substring one and would not
        # see an argument appended to it, and CONTRIBUTING.md tells a
        # contributor to run the command that is supposed to be this one.
        pattern = re.compile(r"unittest discover[^\n\\\"]*")
        found: dict[str, list[str]] = {}
        sources = [*sorted(WORKFLOWS.glob("*.yml")), ROOT / "CONTRIBUTING.md"]
        for path in sources:
            for match in pattern.finditer(path.read_text()):
                found.setdefault(match.group(0).strip(), []).append(str(path.relative_to(ROOT)))
        self.assertEqual(
            sorted(found),
            [UNIT_SUITE],
            f"the unit suite is invoked in more than one way: {found}",
        )

    def test_the_nightly_gate_runs_the_unit_suite_under_coverage(self) -> None:
        body = step_run_body(NIGHTLY, GATE_STEP)
        self.assertIn(f"coverage run -m {UNIT_SUITE}", body)


class SummarizeStepTests(unittest.TestCase):
    """What a nightly leaves behind for whoever reads it in the morning."""

    def _summarize(self, tmp: Path, *, seed: str = "") -> tuple[subprocess.CompletedProcess, str]:
        summary = tmp / "github_step_summary"
        summary.write_text(seed)
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        proc = subprocess.run(
            ["bash", "-c", step_run_body(NIGHTLY, SUMMARIZE_STEP)],
            cwd=str(tmp),
            env=env,
            capture_output=True,
            text=True,
        )
        return proc, summary.read_text()

    def test_it_appends_rather_than_replacing_what_earlier_steps_wrote(self) -> None:
        # $GITHUB_STEP_SUMMARY accumulates across a job. A truncating redirect
        # would erase an earlier section, and no assertion on this step's own
        # output would notice.
        with tempfile.TemporaryDirectory() as tmp:
            proc, summary = self._summarize(Path(tmp), seed="## An earlier step\n")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(summary.startswith("## An earlier step\n"), summary)
            self.assertIn("## Nightly compliance", summary)

    def test_it_says_what_a_failure_here_means(self) -> None:
        # The summary is written on failure too, and the reader is someone who
        # pushed nothing. Saying so is the difference between a scheduled red
        # that gets investigated and one that gets ignored.
        with tempfile.TemporaryDirectory() as tmp:
            _, summary = self._summarize(Path(tmp))
            self.assertIn("outside this repository", summary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

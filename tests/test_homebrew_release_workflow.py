"""Execute the `run:` bodies of .github/workflows/update-homebrew-formula.yml.

This workflow is what points the Homebrew formula at a release. It pushes
straight to `main`, so between the tag being published and users running
`brew upgrade` there is no human step -- and nothing in the suite ran a line
of its shell. tests/test_homebrew_formula.py asserts on the workflow as text
(`ref: main`, `--update`, `--check`, `git push origin HEAD:main` all appear in
it), and tests/test_workflow_dependencies.py reads it to compare action pins;
neither runs it. Substring assertions cannot see the order those commands run
in, whether a failing `--check` stops the push, or what happens when the
optional coverage tooling is missing.

Three of its decisions fail quietly:

* `--check` runs *after* `--update` and its exit status is what stops a bad
  formula from being pushed. `set -euo pipefail` is the only thing enforcing
  that; without it a release tagged without bumping VERSION would be pushed
  with a formula whose own `brew test` block cannot pass.
* The two script invocations are measured by `coverage run` and then
  `coverage run -a`. The `-a` is what makes the second run append to the first
  one's data instead of replacing it. Drop it and the artifact still uploads,
  the run still passes, and the reported homebrew-release coverage silently
  becomes "the --check run only".
* `pip install coverage` is `continue-on-error`, so the shell has to cope with
  coverage being absent. That fallback exists so a PyPI outage cannot be what
  leaves a published release pinned to the previous version -- a branch that
  only runs on a bad day, and would be found on that day.

The shell is extracted from the workflow rather than copied here, so editing
the workflow re-runs these assertions against the edit. The real
homebrew_formula.py, atomic_image_builder.py and Formula/ file are used: the
seam being tested is the step driving those, not a stand-in. Only two things
are stubbed, both in a directory placed on `PYTHONPATH`:

* `sitecustomize.py` replaces `urllib.request.urlopen`, so the release tarball
  is served from a local fixture and no test can reach github.com. It records
  every URL requested, which is how a case asserts the formula was pointed at
  the tag's archive.
* `coverage.py` stands in for `python3 -m coverage`, recording the argv the
  step would have measured with and then running the requested script itself.
  A second variant reports the module as missing, because CI installs coverage
  (see .github/workflows/ci.yml) and so its absence cannot be arranged by not
  installing it.
"""

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_if, step_run_body
from atomic_image_builder import VERSION
from homebrew_formula import parse_formula, render_formula, tarball_url

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/update-homebrew-formula.yml"
UPDATE_STEP = "Point the formula at the release"
PUSH_STEP = "Commit and push"
FORMULA = "Formula/atomic-image-builder.rb"

# Stands in for the release tarball. The digest the formula ends up recording
# is this content's real sha256, computed by the real fetch_sha256.
TARBALL = b"atomic-image-builder release tarball\n"
TARBALL_SHA = hashlib.sha256(TARBALL).hexdigest()

# Serves TARBALL for any URL and records what was asked for. Imported by every
# interpreter the step starts, including the ones under the coverage stub.
SITECUSTOMIZE = '''
import os
import urllib.request


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        chunk = self._body if size is None or size < 0 else self._body[:size]
        self._body = b"" if size is None or size < 0 else self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(request, *args, **kwargs):
    url = getattr(request, "full_url", request)
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write("urlopen\\t%s\\n" % url)
    with open(os.environ["STUB_TARBALL"], "rb") as tarball:
        return _Response(tarball.read())


urllib.request.urlopen = _urlopen
'''

# Records the argv `python3 -m coverage` was given, then runs the script the
# step asked it to measure so the real work still happens.
COVERAGE_STUB = '''
import os
import runpy
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("coverage\\t%s\\n" % "\\t".join(argv))

if argv[:1] == ["--version"]:
    print("Coverage.py, version 0.0.0 (stub)")
    raise SystemExit(0)
if argv[:1] != ["run"]:
    raise SystemExit("stub coverage: unsupported invocation %r" % (argv,))

rest = argv[1:]
while rest and rest[0].startswith("-"):
    rest.pop(0)
if not rest:
    raise SystemExit("stub coverage: nothing to run")
sys.argv = rest
runpy.run_path(rest[0], run_name="__main__")
'''

# What `python3 -m coverage` looks like when the `pip install` step failed.
COVERAGE_MISSING_STUB = '''
import sys

sys.stderr.write("No module named coverage\\n")
raise SystemExit(1)
'''


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


class _StepHarness(unittest.TestCase):
    """A throwaway checkout of the real repo files, plus the two stubs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        (self.stubs / "sitecustomize.py").write_text(SITECUSTOMIZE)

        self.log = self.tmp / "stub.log"
        self.log.touch()
        self.tarball = self.tmp / "tarball"
        self.tarball.write_bytes(TARBALL)

        # An empty global config, so the identity a step sets is the only one
        # available to it and cannot be supplied by the machine running this.
        self.gitconfig = self.tmp / "gitconfig"
        self.gitconfig.touch()

    def make_repo(self, path: Path, *, formula_text: str | None = None) -> Path:
        """A git repo holding the files the workflow's steps touch."""
        (path / "Formula").mkdir(parents=True)
        (path / FORMULA).write_text(formula_text if formula_text is not None else (ROOT / FORMULA).read_text())
        for name in ("homebrew_formula.py", "atomic_image_builder.py", ".coveragerc.maintenance-audit"):
            (path / name).write_bytes((ROOT / name).read_bytes())
        _git(path, "init", "-q", "-b", "main")
        _git(path, "add", "-A")
        self.commit(path, "initial")
        return path

    def commit(self, repo: Path, message: str) -> None:
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        )

    def step_env_for(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.stubs)
        env["STUB_LOG"] = str(self.log)
        env["STUB_TARBALL"] = str(self.tarball)
        env["GIT_CONFIG_GLOBAL"] = str(self.gitconfig)
        env.update(overrides)
        return env

    def calls(self, kind: str) -> list[list[str]]:
        """The recorded stub invocations of one kind, in order."""
        recorded = []
        for line in self.log.read_text().splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if fields[0] == kind:
                recorded.append(fields[1:])
        return recorded


class UpdateStepTests(_StepHarness):
    """The step that rewrites the formula and verifies it before anything is pushed."""

    def run_update_step(
        self,
        *,
        tag: str = f"v{VERSION}",
        formula_text: str | None = None,
        coverage_available: bool = True,
    ) -> tuple[subprocess.CompletedProcess, dict[str, str], Path]:
        stub = COVERAGE_STUB if coverage_available else COVERAGE_MISSING_STUB
        (self.stubs / "coverage.py").write_text(stub)
        repo = self.make_repo(self.tmp / "repo", formula_text=formula_text)
        output_file = self.tmp / "github_output"
        output_file.touch()

        proc = subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, UPDATE_STEP)],
            env=self.step_env_for(TAG=tag, GITHUB_OUTPUT=str(output_file)),
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        outputs = {}
        for line in output_file.read_text().splitlines():
            if line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return proc, outputs, repo

    def test_the_step_reads_the_tag_from_the_release_or_the_dispatch_input(self) -> None:
        # workflow_dispatch exists to re-run against a release published before
        # this workflow did. If only the release expression were declared, a
        # dispatched run would update the formula to the empty tag.
        self.assertEqual(
            step_env(WORKFLOW, UPDATE_STEP),
            {"TAG": "${{ github.event.release.tag_name || inputs.tag }}"},
        )

    def test_the_formula_is_pointed_at_the_tag_with_the_tarballs_real_digest(self) -> None:
        proc, outputs, repo = self.run_update_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        url, sha = parse_formula((repo / FORMULA).read_text())
        self.assertEqual(url, tarball_url(f"v{VERSION}"))
        self.assertEqual(sha, TARBALL_SHA)
        self.assertEqual(outputs["changed"], "true")

    def test_the_formula_is_verified_against_a_second_download(self) -> None:
        # --check re-downloads the url the update just recorded. One request
        # would mean the recorded digest was never confirmed against anything.
        _, _, _ = self.run_update_step()
        requested = self.calls("urlopen")
        self.assertEqual(requested, [[tarball_url(f"v{VERSION}")]] * 2)

    def test_both_invocations_are_measured_and_the_check_appends(self) -> None:
        # Without -a the second run replaces the first run's data file, and
        # the coverage this job exists to report becomes the --check run only.
        self.run_update_step()
        rcfile = "--rcfile=.coveragerc.maintenance-audit"
        self.assertEqual(
            self.calls("coverage"),
            [
                ["--version"],
                ["run", rcfile, "homebrew_formula.py", "--update", f"v{VERSION}"],
                ["run", rcfile, "-a", "homebrew_formula.py", "--check"],
            ],
        )

    def test_nothing_to_do_when_the_formula_already_points_at_the_release(self) -> None:
        # A re-run against an already-updated release must not report a change:
        # `changed=true` sends the next step into `git commit` with nothing
        # staged, which fails the job on a release that was fine.
        current = render_formula(
            (ROOT / FORMULA).read_text(),
            url=tarball_url(f"v{VERSION}"),
            sha256=TARBALL_SHA,
        )
        proc, outputs, _ = self.run_update_step(formula_text=current)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["changed"], "false")

    def test_a_release_tagged_without_bumping_version_fails_the_step(self) -> None:
        # --update happily rewrites the formula to any tag; --check is what
        # notices the tag disagrees with the tool's VERSION. The formula on
        # disk is wrong at that point, so the step failing is the only thing
        # keeping it out of the push step below.
        proc, outputs, repo = self.run_update_step(tag="v99.0.0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("changed", outputs)
        self.assertEqual(parse_formula((repo / FORMULA).read_text())[0], tarball_url("v99.0.0"))
        self.assertIn("VERSION", proc.stdout + proc.stderr)

    def test_a_missing_coverage_module_still_updates_the_formula(self) -> None:
        # The measurement is advisory; pointing the formula at the release is
        # not. This is the branch a failed `pip install coverage` takes.
        proc, outputs, repo = self.run_update_step(coverage_available=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(parse_formula((repo / FORMULA).read_text())[1], TARBALL_SHA)
        self.assertIn("::warning::coverage unavailable", proc.stdout)
        # And it really ran unmeasured: the module that records invocations is
        # the one reporting itself missing, so nothing was measured at all.
        self.assertEqual(self.calls("coverage"), [])

    def test_the_check_still_gates_when_coverage_is_missing(self) -> None:
        # The fallback swaps the command prefix, not the verification.
        proc, outputs, _ = self.run_update_step(tag="v99.0.0", coverage_available=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("changed", outputs)


class CommitAndPushStepTests(_StepHarness):
    """The step that puts the updated formula on main."""

    def set_up_clone(self) -> tuple[Path, Path]:
        """A bare origin holding the repo files, and a clone with an edited formula."""
        origin_source = self.make_repo(self.tmp / "origin-source")
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(origin_source), str(origin)], check=True)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        return origin, clone

    def edit_formula(self, clone: Path) -> None:
        (clone / FORMULA).write_text(
            render_formula(
                (clone / FORMULA).read_text(),
                url=tarball_url(f"v{VERSION}"),
                sha256=TARBALL_SHA,
            )
        )

    def run_push_step(self, clone: Path, *, tag: str = f"v{VERSION}") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, PUSH_STEP)],
            env=self.step_env_for(TAG=tag),
            cwd=str(clone),
            capture_output=True,
            text=True,
        )

    def test_the_step_runs_only_when_the_formula_actually_changed(self) -> None:
        self.assertEqual(step_if(WORKFLOW, PUSH_STEP), "steps.update.outputs.changed == 'true'")
        self.assertEqual(
            step_env(WORKFLOW, PUSH_STEP),
            {"TAG": "${{ github.event.release.tag_name || inputs.tag }}"},
        )

    def test_an_unchanged_formula_would_fail_the_step(self) -> None:
        # Which is what makes the `if:` above load-bearing rather than tidy:
        # `git commit -am` with nothing to commit exits non-zero.
        _, clone = self.set_up_clone()
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)

    def test_the_formula_change_reaches_main(self) -> None:
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pushed = _git(origin, "show", f"main:{FORMULA}")
        self.assertEqual(parse_formula(pushed), (tarball_url(f"v{VERSION}"), TARBALL_SHA))

    def test_the_commit_names_the_release_it_is_for(self) -> None:
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        self.run_push_step(clone, tag="v1.2.3")
        self.assertEqual(_git(origin, "log", "-1", "--format=%s", "main"), "Point the Homebrew formula at v1.2.3")

    def test_the_commit_carries_the_formula_and_nothing_else(self) -> None:
        # The gate on this step is `git diff --quiet -- Formula/` and the
        # verification before it is `homebrew_formula.py --check`, which reads
        # the formula and nothing else. Those are the only paths the job has
        # established anything about, so they are the only paths its commit may
        # contain -- this is the one push to main with no review between it and
        # everyone's `brew upgrade`.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "show", "--name-only", "--format=", "main").split(), [FORMULA])

    def test_another_modified_file_stops_the_release_instead_of_riding_along(self) -> None:
        # The case the pathspec exists for. `git commit -am` would stage this
        # file too and push it to main under a message naming only the release,
        # with neither the `if:` gate nor `--check` having looked at it. Scoped
        # to Formula/, it is left unstaged instead, and the `git rebase` on the
        # next line refuses to run against a dirty tree -- so a job whose
        # contract is "a single machine-generated sha256" goes red rather than
        # publishing something nothing verified.
        origin, clone = self.set_up_clone()
        before = _git(origin, "rev-parse", "main")
        self.edit_formula(clone)
        (clone / ".coveragerc.maintenance-audit").write_text("[run]\nsource = .\n")
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unstaged changes", proc.stderr)
        self.assertEqual(_git(origin, "rev-parse", "main"), before)

    def test_the_commit_is_authored_by_the_actions_bot(self) -> None:
        # There is no committer identity on a runner, so the step supplies one.
        # Without it `git commit` fails and the release stays unpinned.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            _git(origin, "log", "-1", "--format=%an|%ae|%cn", "main"),
            "github-actions[bot]|41898282+github-actions[bot]@users.noreply.github.com|github-actions[bot]",
        )

    def test_a_main_that_moved_during_the_release_is_rebased_onto_not_clobbered(self) -> None:
        # The checkout happens while the release is being published, so main
        # can gain a commit before this step runs. Without the fetch/rebase the
        # push is rejected and the release stays unpinned; with a force push it
        # would take that commit with it.
        origin, clone = self.set_up_clone()
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        (other / "README.md").write_text("landed while the release was publishing\n")
        _git(other, "add", "README.md")
        self.commit(other, "unrelated change on main")
        _git(other, "push", "-q", "origin", "main")

        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "show", "main:README.md"), "landed while the release was publishing")
        self.assertEqual(parse_formula(_git(origin, "show", f"main:{FORMULA}"))[1], TARBALL_SHA)
        self.assertEqual(
            _git(origin, "log", "--format=%s", "main").splitlines()[:2],
            [f"Point the Homebrew formula at v{VERSION}", "unrelated change on main"],
        )


if __name__ == "__main__":
    unittest.main()

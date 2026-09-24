"""Execute the `run:` bodies of .github/workflows/update-homebrew-formula.yml.

This workflow is what points the Homebrew formula at a release. It pushes the
change to a `formula/<tag>` branch and leaves a link, in the run summary and
in a reminder issue, that opens the pull request in one click. Between the tag
being published and users running `brew upgrade` stand only that branch, that
link, and the pull request passing `test` -- and nothing in the suite ran a
line of its shell. tests/test_workflow_dependencies.py reads it to
compare install pins; that does not run it. Substring assertions cannot see
the order those commands run in, whether a failing `--check` stops the push,
where the push lands, or what happens when the optional coverage tooling is
missing.

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
seam being tested is the step driving those, not a stand-in. Two things are
stubbed in a directory placed on `PYTHONPATH`:

* `sitecustomize.py` replaces `urllib.request.urlopen`, so the release tarball
  is served from a local fixture and no test can reach github.com. It records
  every URL requested, which is how a case asserts the formula was pointed at
  the tag's archive.
* `coverage.py` stands in for `python3 -m coverage`, recording the argv the
  step would have measured with and then running the requested script itself.
  A second variant reports the module as missing, because CI installs coverage
  (see .github/workflows/ci.yml) and so its absence cannot be arranged by not
  installing it.

The reminder-issue step talks to GitHub only through `gh`, so a third stub,
an executable `gh` placed first on `PATH`, records each call and answers the
subcommands that step uses. It runs the step's own `--jq` filter through
the real `jq`, so which issues count as the reminder is decided by the
workflow's filter, not by the stub.
"""

import hashlib
import json
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
PUSH_STEP = "Push the formula branch"
ISSUE_STEP = "Open the reminder issue"
SUMMARY_STEP = "Summarize"
FORMULA = "Formula/atomic-image-builder.rb"
REPO = "Danathar/atomic-image-builder"


def compare_link(tag: str) -> str:
    """The link that opens the formula pull request, spelled out by hand."""
    return (
        f"https://github.com/{REPO}/compare/main...formula/{tag}"
        f"?expand=1&title=Point%20the%20Homebrew%20formula%20at%20{tag}"
    )


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


# Stands in for `gh`. Records each call, keeps a copy of any --body-file, and
# answers the subcommands the reminder-issue step uses. `issue list` feeds
# STUB_GH_ISSUES, shaped the way `gh --json` prints issues, through the step's
# own --jq filter with the real jq.
GH_STUB = '''#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("gh\\t%s\\n" % "\\t".join(argv))
if argv[:2] == ["issue", "list"]:
    jq = argv[argv.index("--jq") + 1]
    result = subprocess.run(
        ["jq", "-r", jq], input=os.environ.get("STUB_GH_ISSUES", "[]"), capture_output=True, text=True, check=True
    )
    sys.stdout.write(result.stdout)
elif argv[:2] == ["issue", "create"]:
    shutil.copy(argv[argv.index("--body-file") + 1], os.environ["STUB_GH_BODY"])
    print(os.environ["STUB_GH_NEW_ISSUE"])
elif argv[:2] == ["issue", "comment"]:
    shutil.copy(argv[argv.index("--body-file") + 1], os.environ["STUB_GH_COMMENT"])
elif argv[:2] == ["issue", "close"]:
    pass
else:
    raise SystemExit("stub gh: unsupported invocation %r" % (argv,))
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
        # Nothing from the machine running this: the global config is an empty
        # file, and the system config is not read at all.
        env["GIT_CONFIG_GLOBAL"] = str(self.gitconfig)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env.update(overrides)
        return env

    def read_outputs(self, path: Path) -> dict[str, str]:
        outputs = {}
        for line in path.read_text().splitlines():
            if line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return outputs

    def gh_on_path(self) -> str:
        """A PATH whose first entry holds the `gh` stub."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

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


class PushStepTests(_StepHarness):
    """The step that puts the updated formula on a `formula/<tag>` branch."""

    def set_up_clone(self) -> tuple[Path, Path]:
        """A bare origin holding the repo files, and a clone with an edited formula."""
        origin_source = self.make_repo(self.tmp / "origin-source")
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(origin_source), str(origin)], check=True)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        return origin, clone

    def edit_formula(self, clone: Path, sha256: str = TARBALL_SHA) -> None:
        (clone / FORMULA).write_text(
            render_formula(
                (clone / FORMULA).read_text(),
                url=tarball_url(f"v{VERSION}"),
                sha256=sha256,
            )
        )

    def run_push_step(self, clone: Path, *, tag: str = f"v{VERSION}") -> subprocess.CompletedProcess:
        self.output_file = self.tmp / "github_output"
        self.output_file.write_text("")
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, PUSH_STEP)],
            env=self.step_env_for(
                TAG=tag,
                GITHUB_OUTPUT=str(self.output_file),
                GITHUB_SERVER_URL="https://github.com",
                GITHUB_REPOSITORY=REPO,
            ),
            cwd=str(clone),
            capture_output=True,
            text=True,
        )

    def protect_main(self, origin: Path) -> None:
        """Refuse every push to main, the way the `protect main` ruleset does."""
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read -r old new ref; do\n"
            '  if [ "$ref" = refs/heads/main ]; then echo "refused: main is protected" >&2; exit 1; fi\n'
            "done\n"
        )
        hook.chmod(0o755)

    def branch(self, tag: str = f"v{VERSION}") -> str:
        return f"formula/{tag}"

    def test_the_step_reads_the_tag_from_the_release_or_the_dispatch_input(self) -> None:
        # And nothing else: the push authenticates with the checkout's own
        # GITHUB_TOKEN, so no secret or other token reaches this step.
        self.assertEqual(step_env(WORKFLOW, PUSH_STEP), {"TAG": "${{ github.event.release.tag_name || inputs.tag }}"})
        self.assertEqual(step_if(WORKFLOW, PUSH_STEP), "steps.update.outputs.changed == 'true'")

    def test_an_unchanged_formula_would_fail_the_step(self) -> None:
        # Which is what makes the `if:` load-bearing rather than tidy:
        # `git commit` with nothing to commit exits non-zero.
        _, clone = self.set_up_clone()
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)

    def test_the_change_lands_on_the_formula_branch_and_main_is_untouched(self) -> None:
        # The ruleset refuses any direct push to main. A step that went back
        # to `git push origin HEAD:main` would fail every release, and would
        # skip the pull request and `test` wherever the push got through.
        origin, clone = self.set_up_clone()
        main_before = _git(origin, "rev-parse", "main")
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "rev-parse", "main"), main_before)
        self.assertEqual(
            _git(origin, "for-each-ref", "--format=%(refname)", "refs/heads").split(),
            [f"refs/heads/{self.branch()}", "refs/heads/main"],
        )
        pushed = _git(origin, "show", f"{self.branch()}:{FORMULA}")
        self.assertEqual(parse_formula(pushed), (tarball_url(f"v{VERSION}"), TARBALL_SHA))
        self.assertEqual(_git(origin, "rev-parse", f"{self.branch()}^"), main_before)

    def test_the_push_succeeds_where_main_is_protected(self) -> None:
        # What the release actually runs against: an origin that refuses
        # every update to main. Any push to main, alone or next to the
        # branch push, fails the step here.
        origin, clone = self.set_up_clone()
        self.protect_main(origin)
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "rev-list", "--count", f"main..{self.branch()}"), "1")

    def test_the_step_writes_the_link_that_opens_the_pull_request(self) -> None:
        # The title goes through URL encoding; spaces left raw would cut the
        # link short wherever it is pasted or rendered.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_outputs(self.output_file), {"compare": compare_link(f"v{VERSION}")})

    def test_a_failed_push_writes_no_link(self) -> None:
        # The link is what the issue and the summary offer. Written before a
        # push that then failed, it would point at a branch that is not there.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
        hook.chmod(0o755)
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.read_outputs(self.output_file), {})

    def test_the_commit_names_the_release_it_is_for(self) -> None:
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        self.run_push_step(clone, tag="v1.2.3")
        self.assertEqual(
            _git(origin, "log", "-1", "--format=%s", self.branch("v1.2.3")),
            "Point the Homebrew formula at v1.2.3",
        )

    def test_the_commit_carries_the_formula_and_nothing_else(self) -> None:
        # The gate on this step is `git diff --quiet -- Formula/` and the
        # verification before it is `homebrew_formula.py --check`, which reads
        # the formula and nothing else. Those are the only paths the job has
        # established anything about, so they are the only paths its commit may
        # contain.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "show", "--name-only", "--format=", self.branch()).split(), [FORMULA])

    def test_another_modified_file_stops_the_release_instead_of_riding_along(self) -> None:
        # `git commit -am` would stage this file too and push it under a
        # message naming only the release, with neither the `if:` gate nor
        # `--check` having looked at it. Scoped to Formula/, it is left
        # unstaged instead, and the `git rebase` on the next line refuses to
        # run against a dirty tree -- so the job goes red rather than
        # publishing something nothing verified.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        (clone / ".coveragerc.maintenance-audit").write_text("[run]\nsource = .\n")
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unstaged changes", proc.stderr)
        self.assertEqual(_git(origin, "branch", "--list", self.branch()), "")

    def test_the_commit_is_authored_by_the_actions_bot(self) -> None:
        # There is no committer identity on a runner, so the step supplies one.
        # Without it `git commit` fails and the release stays unpinned.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            _git(origin, "log", "-1", "--format=%an|%ae|%cn", self.branch()),
            "github-actions[bot]|41898282+github-actions[bot]@users.noreply.github.com|github-actions[bot]",
        )

    def test_the_branch_starts_from_the_main_the_release_ended_on(self) -> None:
        # The checkout happens while the release is being published, so main
        # can gain a commit before this step runs. Without the fetch/rebase
        # the pull request would carry a stale base, and merging it would
        # look like it reverts that commit in the diff.
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
        self.assertEqual(
            _git(origin, "log", "--format=%s", self.branch()).splitlines()[:2],
            [f"Point the Homebrew formula at v{VERSION}", "unrelated change on main"],
        )

    def test_a_rerun_replaces_the_branch_an_earlier_run_pushed(self) -> None:
        # The branch belongs to this workflow. A re-run for the same tag, after
        # a failed or superseded run, has to replace it rather than be refused
        # as a non-fast-forward and leave the old commit in the pull request.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone, sha256="0" * 64)
        self.assertEqual(self.run_push_step(clone).returncode, 0)

        rerun = self.tmp / "rerun"
        subprocess.run(["git", "clone", "-q", str(origin), str(rerun)], check=True)
        self.edit_formula(rerun)
        proc = self.run_push_step(rerun)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(parse_formula(_git(origin, "show", f"{self.branch()}:{FORMULA}"))[1], TARBALL_SHA)
        self.assertEqual(_git(origin, "rev-list", "--count", f"main..{self.branch()}"), "1")
        # Replacing it moves any open pull request to a commit `test` never
        # ran on, so the summary and the issue have to be told.
        self.assertEqual(self.read_outputs(self.output_file)["replaced"], "true")

    def test_a_rerun_with_the_same_formula_leaves_the_branch_alone(self) -> None:
        # A push with GITHUB_TOKEN starts no CI. Moving the branch under a
        # pull request someone already opened would leave its head without a
        # `test` result, and the ruleset would keep it from merging.
        origin, clone = self.set_up_clone()
        self.edit_formula(clone)
        self.assertEqual(self.run_push_step(clone).returncode, 0)
        first = _git(origin, "rev-parse", self.branch())

        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        (other / "README.md").write_text("landed after the first run\n")
        _git(other, "add", "README.md")
        self.commit(other, "unrelated change on main")
        _git(other, "push", "-q", "origin", "main")

        rerun = self.tmp / "rerun"
        subprocess.run(["git", "clone", "-q", str(origin), str(rerun)], check=True)
        self.edit_formula(rerun)
        proc = self.run_push_step(rerun)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "rev-parse", self.branch()), first)
        self.assertEqual(self.read_outputs(self.output_file), {"compare": compare_link(f"v{VERSION}")})

    def test_older_formula_branches_are_deleted_and_nothing_else(self) -> None:
        # This run is for the newest release (`--check` fails any other tag).
        # An older formula branch changes the same lines, and merging it would
        # point Homebrew back at the previous release.
        origin, clone = self.set_up_clone()
        for branch in ("formula/v0.0.1", "formula-notes", "feature/formula"):
            _git(origin, "branch", branch, "main")
        self.protect_main(origin)
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            _git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split(),
            ["feature/formula", "formula-notes", self.branch(), "main"],
        )


class IssueStepTests(_StepHarness):
    """The reminder issue that carries the link, opened once per tag."""

    NEW_ISSUE = f"https://github.com/{REPO}/issues/501"
    OPEN_ISSUE = f"https://github.com/{REPO}/issues/499"

    @staticmethod
    def issue(number: int, title: str, author: str = "app/github-actions") -> dict:
        return {
            "number": number,
            "url": f"https://github.com/{REPO}/issues/{number}",
            "title": title,
            "author": {"login": author, "is_bot": author.startswith("app/")},
        }

    def run_issue_step(
        self, *, tag: str = "v1.2.3", issues: list[dict] | None = None, replaced: str = ""
    ) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        output_file = self.tmp / "github_output"
        output_file.write_text("")
        runner_temp = self.tmp / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        proc = subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, ISSUE_STEP)],
            env=self.step_env_for(
                TAG=tag,
                COMPARE_URL=compare_link(tag),
                REPLACED=replaced,
                PATH=self.gh_on_path(),
                GITHUB_OUTPUT=str(output_file),
                RUNNER_TEMP=str(runner_temp),
                RUN_URL=f"https://github.com/{REPO}/actions/runs/1",
                STUB_GH_ISSUES=json.dumps(issues or []),
                STUB_GH_NEW_ISSUE=self.NEW_ISSUE,
                STUB_GH_BODY=str(self.tmp / "body.md"),
                STUB_GH_COMMENT=str(self.tmp / "comment.md"),
            ),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        return proc, self.read_outputs(output_file)

    def gh(self, *subcommand: str) -> list[list[str]]:
        return [call for call in self.calls("gh") if tuple(call[: len(subcommand)]) == subcommand]

    def test_it_runs_whenever_the_push_left_a_link_and_uses_the_workflow_token(self) -> None:
        # The link exists only once the branch is pushed. A secret or an App
        # token here would bring back the setup this flow exists to avoid.
        self.assertEqual(step_if(WORKFLOW, ISSUE_STEP), "steps.push.outputs.compare != ''")
        env = step_env(WORKFLOW, ISSUE_STEP)
        self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(env["COMPARE_URL"], "${{ steps.push.outputs.compare }}")
        self.assertEqual(env["REPLACED"], "${{ steps.push.outputs.replaced }}")
        self.assertEqual(env["TAG"], "${{ github.event.release.tag_name || inputs.tag }}")

    def test_it_opens_an_issue_with_the_link(self) -> None:
        proc, outputs = self.run_issue_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        create = self.gh("issue", "create")
        self.assertEqual(len(create), 1)
        args = create[0]
        self.assertEqual(args[args.index("--title") + 1], "Open the Homebrew formula PR for v1.2.3")
        body = (self.tmp / "body.md").read_text()
        self.assertIn(compare_link("v1.2.3"), body)
        self.assertIn("A person has to open it", body)
        self.assertIn("`formula/v1.2.3`", body)
        self.assertIn("actions/runs/1", body)
        self.assertEqual(outputs, {"url": self.NEW_ISSUE})

    def test_a_rerun_reuses_the_open_reminder(self) -> None:
        issues = [self.issue(498, "Open the Homebrew formula PR for v1.2.2"), self.issue(499, "Open the Homebrew formula PR for v1.2.3")]
        proc, outputs = self.run_issue_step(issues=issues)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.gh("issue", "create"), [])
        self.assertEqual(outputs, {"url": self.OPEN_ISSUE})

    def test_a_replaced_branch_is_reported_on_the_reused_reminder(self) -> None:
        # A pull request already open from the branch now points at a commit
        # `test` never ran on. Closing and reopening it is what runs `test`.
        issues = [self.issue(499, "Open the Homebrew formula PR for v1.2.3")]
        proc, outputs = self.run_issue_step(issues=issues, replaced="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        comment = self.gh("issue", "comment")
        self.assertEqual(len(comment), 1)
        self.assertEqual(comment[0][2], self.OPEN_ISSUE)
        self.assertIn("close and reopen it", (self.tmp / "comment.md").read_text())
        self.assertEqual(outputs, {"url": self.OPEN_ISSUE})

    def test_an_unchanged_branch_adds_nothing_to_the_reminder(self) -> None:
        issues = [self.issue(499, "Open the Homebrew formula PR for v1.2.3")]
        proc, _ = self.run_issue_step(issues=issues)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.gh("issue", "comment"), [])

    def test_reminders_for_older_tags_are_closed(self) -> None:
        # Their branches are gone, and following one would merge a formula
        # for the previous release. Someone else's issue stays, whatever it is
        # called, and so does anything that is not a reminder.
        issues = [
            self.issue(480, "Open the Homebrew formula PR for v1.2.2"),
            self.issue(481, "Open the Homebrew formula PR for v1.2.1", author="someone"),
            self.issue(482, "Homebrew formula notes"),
        ]
        proc, outputs = self.run_issue_step(issues=issues)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        closed = self.gh("issue", "close")
        self.assertEqual([call[2] for call in closed], ["480"])
        comment = closed[0][closed[0].index("--comment") + 1]
        self.assertIn(self.NEW_ISSUE, comment)
        self.assertIn("v1.2.3", comment)
        self.assertEqual(outputs, {"url": self.NEW_ISSUE})

    def test_only_open_issues_are_considered(self) -> None:
        # A closed reminder is one somebody finished with; reusing it would
        # hand back a link nobody is looking at.
        self.run_issue_step()
        listed = self.gh("issue", "list")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0][listed[0].index("--state") + 1], "open")

    def test_someone_elses_issue_with_the_same_title_is_not_the_reminder(self) -> None:
        # Anyone can open an issue, and a tag's reminder title is predictable
        # before the release. Reusing theirs would put their link, not this
        # run's, in front of whoever opens the pull request.
        issues = [self.issue(499, "Open the Homebrew formula PR for v1.2.3", author="someone")]
        proc, outputs = self.run_issue_step(issues=issues)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.gh("issue", "create")), 1)
        self.assertEqual(outputs, {"url": self.NEW_ISSUE})

    def test_a_reminder_for_another_tag_is_not_reused(self) -> None:
        issues = [self.issue(499, "Open the Homebrew formula PR for v1.2.30")]
        proc, outputs = self.run_issue_step(issues=issues)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.gh("issue", "create")), 1)
        self.assertEqual(outputs, {"url": self.NEW_ISSUE})

    def test_the_job_opens_no_pull_request_itself(self) -> None:
        # One opened with GITHUB_TOKEN starts no CI, so `test` would never
        # report and it could never merge.
        self.assertNotIn("gh pr ", WORKFLOW.read_text())


class SummaryStepTests(_StepHarness):
    """What the run summary tells whoever looks at a release's run."""

    def summarize(self, **env: str) -> str:
        summary = self.tmp / "summary.md"
        summary.write_text("")
        repo = self.make_repo(self.tmp / "repo")
        base = {"TAG": "v1.2.3", "CHANGED": "", "COMPARE_URL": "", "REPLACED": "", "ISSUE_URL": ""}
        proc = subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, SUMMARY_STEP)],
            env=self.step_env_for(GITHUB_STEP_SUMMARY=str(summary), **{**base, **env}),
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return summary.read_text()

    def test_every_value_the_summary_reads_is_passed_in(self) -> None:
        self.assertEqual(
            step_env(WORKFLOW, SUMMARY_STEP),
            {
                "TAG": "${{ github.event.release.tag_name || inputs.tag }}",
                "CHANGED": "${{ steps.update.outputs.changed }}",
                "COMPARE_URL": "${{ steps.push.outputs.compare }}",
                "REPLACED": "${{ steps.push.outputs.replaced }}",
                "ISSUE_URL": "${{ steps.issue.outputs.url }}",
            },
        )
        self.assertEqual(step_if(WORKFLOW, SUMMARY_STEP), "always()")

    def test_a_pushed_branch_gets_the_one_click_link_and_why_a_person_opens_it(self) -> None:
        issue = f"https://github.com/{REPO}/issues/501"
        text = self.summarize(CHANGED="true", COMPARE_URL=compare_link("v1.2.3"), ISSUE_URL=issue)
        self.assertIn(f"]({compare_link('v1.2.3')})", text)
        self.assertIn("A person has to open it so CI runs", text)
        self.assertIn(issue, text)

    def test_a_replaced_branch_says_to_reopen_its_pull_request(self) -> None:
        text = self.summarize(CHANGED="true", COMPARE_URL=compare_link("v1.2.3"), REPLACED="true")
        self.assertIn("close and reopen it", text)

    def test_a_new_or_untouched_branch_does_not_ask_for_a_reopen(self) -> None:
        text = self.summarize(CHANGED="true", COMPARE_URL=compare_link("v1.2.3"))
        self.assertNotIn("close and reopen it", text)

    def test_a_missing_reminder_issue_is_called_out(self) -> None:
        text = self.summarize(CHANGED="true", COMPARE_URL=compare_link("v1.2.3"))
        self.assertIn(compare_link("v1.2.3"), text)
        self.assertIn("No reminder issue was opened", text)

    def test_a_failed_push_offers_no_link(self) -> None:
        text = self.summarize(CHANGED="true")
        self.assertIn("did not push it", text)
        self.assertNotIn("/compare/", text)

    def test_nothing_to_do_says_so(self) -> None:
        text = self.summarize(CHANGED="false")
        self.assertIn("Already pointed at `v1.2.3`; nothing to do.", text)
        self.assertNotIn("/compare/", text)


if __name__ == "__main__":
    unittest.main()

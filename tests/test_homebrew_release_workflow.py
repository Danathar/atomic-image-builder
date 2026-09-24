"""Execute the `run:` bodies of .github/workflows/update-homebrew-formula.yml.

This workflow is what points the Homebrew formula at a release. It pushes the
change to a `formula/<tag>` branch and opens a pull request with a GitHub App
token, so the only thing between the tag being published and users running
`brew upgrade` is that pull request passing `test` -- and nothing in the suite
ran a line of its shell. tests/test_workflow_dependencies.py reads it to
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

The pull-request and auto-merge steps talk to GitHub only through `gh`, so a
third stub, an executable `gh` placed first on `PATH`, records each call and
answers the handful of subcommands those steps use.
"""

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_if, step_run_body, step_with
from atomic_image_builder import VERSION
from homebrew_formula import parse_formula, render_formula, tarball_url

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/update-homebrew-formula.yml"
UPDATE_STEP = "Point the formula at the release"
SECRETS_STEP = "Check the formula App secrets are set"
TOKEN_STEP = "Mint the formula App token"
PUSH_STEP = "Push the formula branch"
PR_STEP = "Open the formula pull request"
AUTOMERGE_STEP = "Turn on auto-merge"
SUMMARY_STEP = "Summarize"
FORMULA = "Formula/atomic-image-builder.rb"
APP_TOKEN = "${{ steps.app-token.outputs.token }}"
APP_TOKEN_ACTION = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0"

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
# answers the subcommands the pull-request and auto-merge steps use. What it
# answers is set per test through STUB_GH_* variables.
GH_STUB = '''#!/usr/bin/env python3
import os
import shutil
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("gh\\t%s\\n" % "\\t".join(argv))
if argv[:2] == ["pr", "list"]:
    print(os.environ.get("STUB_GH_OPEN_PR", ""))
elif argv[:2] == ["pr", "create"]:
    shutil.copy(argv[argv.index("--body-file") + 1], os.environ["STUB_GH_BODY"])
    print(os.environ["STUB_GH_NEW_PR"])
elif argv[:2] == ["pr", "view"]:
    print("PR_kwDOnode")
elif argv[:2] == ["api", "graphql"]:
    refusal = os.environ.get("STUB_GH_AUTOMERGE_REFUSAL")
    if refusal:
        sys.stderr.write("GraphQL: %s (enablePullRequestAutoMerge)\\n" % refusal)
        raise SystemExit(1)
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
        env["GIT_CONFIG_GLOBAL"] = str(self.gitconfig)
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


class SecretsStepTests(_StepHarness):
    """The first step: a missing App secret fails the run instead of skipping it."""

    def run_secrets_step(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, SECRETS_STEP)],
            env=self.step_env_for(**env),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )

    def test_only_whether_each_secret_is_set_reaches_the_step(self) -> None:
        # The private key never needs to be in a shell's environment to be
        # checked for; the expression hands the step "true" or "false".
        self.assertEqual(
            step_env(WORKFLOW, SECRETS_STEP),
            {
                "HAS_APP_ID": "${{ secrets.FORMULA_APP_ID != '' }}",
                "HAS_APP_KEY": "${{ secrets.FORMULA_APP_PRIVATE_KEY != '' }}",
            },
        )
        self.assertIsNone(step_if(WORKFLOW, SECRETS_STEP))

    def test_both_secrets_set_lets_the_run_continue(self) -> None:
        proc = self.run_secrets_step(HAS_APP_ID="true", HAS_APP_KEY="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_missing_secret_fails_with_an_error_naming_both_and_the_doc(self) -> None:
        for present, missing in (("HAS_APP_ID", "FORMULA_APP_PRIVATE_KEY"), ("HAS_APP_KEY", "FORMULA_APP_ID")):
            env = {"HAS_APP_ID": "false", "HAS_APP_KEY": "false", present: "true"}
            with self.subTest(missing=missing):
                proc = self.run_secrets_step(**env)
                self.assertEqual(proc.returncode, 1)
                error = [line for line in proc.stdout.splitlines() if line.startswith("::error")]
                self.assertEqual(len(error), 1, proc.stdout)
                for needle in ("FORMULA_APP_ID", "FORMULA_APP_PRIVATE_KEY", "docs/branch-protection.md", f"missing: {missing}."):
                    self.assertIn(needle, error[0])


class TokenStepTests(unittest.TestCase):
    """The App token: what it can do, and when it exists."""

    def test_the_token_reaches_this_repository_with_two_permissions_and_no_more(self) -> None:
        inputs = step_with(WORKFLOW, TOKEN_STEP)
        self.assertEqual(
            {key: value for key, value in inputs.items() if key.startswith("permission-")},
            {"permission-contents": "write", "permission-pull-requests": "write"},
        )
        self.assertEqual(inputs["owner"], "${{ github.repository_owner }}")
        self.assertEqual(inputs["repositories"], "${{ github.event.repository.name }}")
        self.assertEqual(inputs["client-id"], "${{ secrets.FORMULA_APP_ID }}")
        self.assertEqual(inputs["private-key"], "${{ secrets.FORMULA_APP_PRIVATE_KEY }}")

    def test_the_token_is_minted_after_the_formula_is_verified_and_on_every_run(self) -> None:
        # After: nothing that runs the release's Python holds the token.
        # Every run, not only when the formula changed: a dispatch for the tag
        # the formula already names is how docs/branch-protection.md has the
        # App's credentials checked before a real release depends on them.
        text = WORKFLOW.read_text()
        self.assertIn(f"uses: {APP_TOKEN_ACTION}", text)
        order = [line.strip()[len("- name: ") :] for line in text.splitlines() if line.strip().startswith("- name: ")]
        self.assertLess(order.index(UPDATE_STEP), order.index(TOKEN_STEP))
        self.assertLess(order.index(TOKEN_STEP), order.index(PUSH_STEP))
        self.assertIsNone(step_if(WORKFLOW, TOKEN_STEP))

    def test_every_step_that_writes_to_github_uses_the_app_token(self) -> None:
        # GITHUB_TOKEN here only reads, and a pull request it opened would
        # start no CI, so `test` would never report on it.
        for step in (PUSH_STEP, PR_STEP, AUTOMERGE_STEP):
            with self.subTest(step=step):
                self.assertEqual(step_env(WORKFLOW, step)["GH_TOKEN"], APP_TOKEN)
                self.assertEqual(step_if(WORKFLOW, step), "steps.update.outputs.changed == 'true'")

    def test_the_push_step_hands_git_the_app_token_from_the_environment(self) -> None:
        # checkout runs with persist-credentials: false, so this helper is the
        # only credential git has. Asked the way `git push` asks, it has to
        # answer with the token and the username GitHub expects for one.
        env = {key: value for key, value in step_env(WORKFLOW, PUSH_STEP).items() if key.startswith("GIT_CONFIG_")}
        env = {key: value.strip("\"'") for key, value in env.items()}
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env={**os.environ, **env, "GH_TOKEN": "ghs_exampletoken", "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("username=x-access-token", proc.stdout.splitlines())
        self.assertIn("password=ghs_exampletoken", proc.stdout.splitlines())


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
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, PUSH_STEP)],
            env=self.step_env_for(TAG=tag),
            cwd=str(clone),
            capture_output=True,
            text=True,
        )

    def branch(self, tag: str = f"v{VERSION}") -> str:
        return f"formula/{tag}"

    def test_the_step_reads_the_tag_from_the_release_or_the_dispatch_input(self) -> None:
        self.assertEqual(step_env(WORKFLOW, PUSH_STEP)["TAG"], "${{ github.event.release.tag_name || inputs.tag }}")

    def test_an_unchanged_formula_would_fail_the_step(self) -> None:
        # Which is what makes the `if:` load-bearing rather than tidy:
        # `git commit` with nothing to commit exits non-zero.
        _, clone = self.set_up_clone()
        proc = self.run_push_step(clone)
        self.assertNotEqual(proc.returncode, 0)

    def test_the_change_lands_on_the_formula_branch_and_main_is_untouched(self) -> None:
        # The ruleset refuses any direct push to main. A step that went back
        # to `git push origin HEAD:main` would fail every release once it is
        # applied, and until then would skip the pull request and `test`.
        origin, clone = self.set_up_clone()
        main_before = _git(origin, "rev-parse", "main")
        self.edit_formula(clone)
        proc = self.run_push_step(clone)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git(origin, "rev-parse", "main"), main_before)
        pushed = _git(origin, "show", f"{self.branch()}:{FORMULA}")
        self.assertEqual(parse_formula(pushed), (tarball_url(f"v{VERSION}"), TARBALL_SHA))
        self.assertEqual(_git(origin, "rev-parse", f"{self.branch()}^"), main_before)

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


class PullRequestStepTests(_StepHarness):
    """The step that opens the pull request, or finds the one a re-run left."""

    NEW_PR = "https://github.com/Danathar/atomic-image-builder/pull/501"
    OPEN_PR = "https://github.com/Danathar/atomic-image-builder/pull/499"

    def run_pr_step(self, *, tag: str = "v1.2.3", open_pr: str = "") -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        output_file = self.tmp / "github_output"
        output_file.write_text("")
        runner_temp = self.tmp / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        proc = subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, PR_STEP)],
            env=self.step_env_for(
                TAG=tag,
                PATH=self.gh_on_path(),
                GITHUB_OUTPUT=str(output_file),
                RUNNER_TEMP=str(runner_temp),
                RUN_URL="https://github.com/Danathar/atomic-image-builder/actions/runs/1",
                STUB_GH_OPEN_PR=open_pr,
                STUB_GH_NEW_PR=self.NEW_PR,
                STUB_GH_BODY=str(self.tmp / "body.md"),
            ),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        return proc, self.read_outputs(output_file)

    def test_it_opens_a_pull_request_from_the_formula_branch_into_main(self) -> None:
        proc, outputs = self.run_pr_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        create = [call for call in self.calls("gh") if call[:2] == ["pr", "create"]]
        self.assertEqual(len(create), 1)
        args = create[0]
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], "formula/v1.2.3")
        self.assertEqual(args[args.index("--title") + 1], "Point the Homebrew formula at v1.2.3")
        body = (self.tmp / "body.md").read_text()
        self.assertIn("v1.2.3", body)
        self.assertIn("actions/runs/1", body)
        self.assertEqual(outputs["url"], self.NEW_PR)

    def test_a_rerun_reuses_the_open_pull_request(self) -> None:
        # The push step has already moved the branch the pull request tracks;
        # a second `gh pr create` for the same head would fail the run.
        proc, outputs = self.run_pr_step(open_pr=self.OPEN_PR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([call for call in self.calls("gh") if call[:2] == ["pr", "create"]], [])
        self.assertEqual(outputs["url"], self.OPEN_PR)
        listed = [call for call in self.calls("gh") if call[:2] == ["pr", "list"]][0]
        self.assertEqual(listed[listed.index("--head") + 1], "formula/v1.2.3")
        self.assertEqual(listed[listed.index("--state") + 1], "open")


class AutoMergeStepTests(_StepHarness):
    """Auto-merge is asked for, never forced, and a refusal does not fail the run."""

    PR = "https://github.com/Danathar/atomic-image-builder/pull/501"

    def run_automerge_step(self, *, refusal: str = "") -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        output_file = self.tmp / "github_output"
        output_file.write_text("")
        proc = subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, AUTOMERGE_STEP)],
            env=self.step_env_for(
                PATH=self.gh_on_path(),
                GITHUB_OUTPUT=str(output_file),
                PR_URL=self.PR,
                STUB_GH_AUTOMERGE_REFUSAL=refusal,
            ),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        return proc, self.read_outputs(output_file)

    def test_it_turns_on_auto_merge_with_a_merge_commit(self) -> None:
        proc, outputs = self.run_automerge_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs, {"enabled": "true"})
        graphql = [call for call in self.calls("gh") if call[:2] == ["api", "graphql"]]
        self.assertEqual(len(graphql), 1)
        self.assertIn("id=PR_kwDOnode", graphql[0])
        query = next(arg for arg in graphql[0] if arg.startswith("query="))
        self.assertIn("enablePullRequestAutoMerge", query)
        self.assertIn("mergeMethod: MERGE", query)

    def test_it_never_merges_on_the_spot(self) -> None:
        # `gh pr merge` merges at once when nothing is required, which before
        # the ruleset is applied means without waiting for `test`.
        self.run_automerge_step()
        self.assertEqual([call for call in self.calls("gh") if call[:2] == ["pr", "merge"]], [])

    def test_a_refusal_leaves_the_pull_request_open_and_says_why(self) -> None:
        proc, outputs = self.run_automerge_step(refusal="Auto merge is not allowed for this repository")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["enabled"], "false")
        self.assertIn("Auto merge is not allowed for this repository", outputs["reason"])
        warning = [line for line in proc.stdout.splitlines() if line.startswith("::warning")]
        self.assertEqual(len(warning), 1, proc.stdout)
        self.assertIn(self.PR, warning[0])


class SummaryStepTests(_StepHarness):
    """What the run summary tells whoever looks at a release's run."""

    def summarize(self, **env: str) -> str:
        summary = self.tmp / "summary.md"
        summary.write_text("")
        repo = self.make_repo(self.tmp / "repo")
        base = {"TAG": "v1.2.3", "SECRETS": "success", "CHANGED": "", "PR_URL": "", "AUTO_MERGE": "", "AUTO_MERGE_REASON": ""}
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
            set(step_env(WORKFLOW, SUMMARY_STEP)),
            {"TAG", "SECRETS", "CHANGED", "PR_URL", "AUTO_MERGE", "AUTO_MERGE_REASON"},
        )

    def test_missing_secrets_are_named(self) -> None:
        text = self.summarize(SECRETS="failure")
        self.assertIn("FORMULA_APP_ID", text)
        self.assertIn("FORMULA_APP_PRIVATE_KEY", text)

    def test_a_pull_request_left_open_says_so(self) -> None:
        url = "https://github.com/Danathar/atomic-image-builder/pull/501"
        text = self.summarize(CHANGED="true", PR_URL=url, AUTO_MERGE="false", AUTO_MERGE_REASON="not allowed")
        self.assertIn(url, text)
        self.assertIn("stays open until someone merges it", text)
        self.assertIn("not allowed", text)

    def test_auto_merge_on_says_it_merges_by_itself(self) -> None:
        url = "https://github.com/Danathar/atomic-image-builder/pull/501"
        text = self.summarize(CHANGED="true", PR_URL=url, AUTO_MERGE="true")
        self.assertIn("merges by itself", text)


if __name__ == "__main__":
    unittest.main()

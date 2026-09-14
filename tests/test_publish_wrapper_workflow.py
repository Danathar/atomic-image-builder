"""Execute the `run:` bodies of .github/workflows/publish-wrapper.yml.

This job publishes the only two files the recommended install downloads. The
wrapper it attaches runs the image as root and decides whether to forward the
user's GitHub credential into it, so it sits upstream of every check it
performs -- and `aib.sha256` is the only thing standing between a user and an
`aib` that was never the one this repository built. The job's whole value is
that the pair it uploads agrees and is named what the docs fetch.

Nothing ran a line of it. One test reads the file
(`test_release_wrapper_assets_match_what_the_docs_download` in
tests/test_atomic_image_builder.py) and it greps for three substrings, so a
body that still contains those strings and no longer produces a matching pair
reads as covered. The failure mode is also invisible from here: the job runs on
`release: published`, and a release with a wrong or missing checksum looks
exactly like a release with a right one until somebody runs the documented
`sha256sum -c -`.

All three `run: |` bodies are executed:

* *Stage the wrapper and its checksum* is where the pair is made. Two cases run
  it: one proves the digest it writes is the digest of `contrib/aib` and that
  the name recorded beside it is `aib` (what the docs download), the other
  hands the step a disagreeing pair and proves it stops before printing or
  uploading it.
* *Attach to the release* is the job's only write. A recording `gh` stub holds
  its argv, which is then compared with what staging actually left in `dist/`
  and with the asset names the README and the install docs fetch by URL --
  computed from both sides rather than grepped on one.
* *Summarize* carries `if: always()`, so it is what a failed publish leaves
  behind; its fallback branch is reached only in that state, and is therefore
  the half no successful run would ever show to be broken.

Bodies run under a plain `bash -c`, not `bash -e`, so the `set -euo pipefail`
each body writes for itself is under test rather than supplied by the harness.
`gh` is a stub and every file is local: no case reaches the network.
"""

import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_if, step_run_body, step_with

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-wrapper.yml"
CHECKOUT_STEP = "Checkout the release"
STAGE_STEP = "Stage the wrapper and its checksum"
UPLOAD_STEP = "Attach to the release"
SUMMARIZE_STEP = "Summarize"

# The tag a case publishes to. Any string works; it is asserted to come out of
# the step in the position the tag belongs in, not to be this value.
TAG = "v0.9.5"

# The expression both the checkout ref and the upload tag are written as. A
# release event carries `release.tag_name`; a dispatch rerun carries neither
# and falls back to the input.
TAG_EXPRESSION = "${{ github.event.release.tag_name || inputs.tag }}"

# Where the docs send users for the published assets. The file names are
# deliberately not spelled here -- they are read out of this URL prefix in the
# docs and compared with what the upload step was given.
RELEASE_DOWNLOAD = "https://github.com/Danathar/atomic-image-builder/releases/latest/download"
_ASSET_URL = re.compile(re.escape(RELEASE_DOWNLOAD) + r"/([A-Za-z0-9._-]+)")

# Records argv so the release write can be asserted, and reports whether it was
# given a token: an upload that runs without `GH_TOKEN` fails on a private API
# call rather than on anything this shell does.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$0" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
printf 'GH_TOKEN=%s\\n' "${GH_TOKEN:-}" >> "$STUB_LOG"
exit "${GH_EXIT:-0}"
"""

# Writes a digest that is not the file's on the generating call, and delegates
# the verifying call to the real tool. That is the only way to hand the step a
# pair that does not agree: both halves come from the same command over the
# same bytes, so a pair produced normally always verifies.
DISAGREEING_SHA256SUM_STUB = """#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then
  exec "$REAL_SHA256SUM" "$@"
fi
printf '%s  %s\\n' "0000000000000000000000000000000000000000000000000000000000000000" "$1"
"""


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def _workflow_lines() -> list[str]:
    return WORKFLOW.read_text().splitlines()


def _mapping_under(lines: list[str], index: int) -> dict[str, str]:
    """The flat `key: value` mapping nested under the line at *index*."""
    indent = len(lines[index]) - len(lines[index].lstrip())
    mapping = {}
    for line in lines[index + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        key, _, value = line.strip().partition(": ")
        mapping[key] = value
    return mapping


def _permissions(indent: int) -> dict[str, str]:
    """The `permissions:` mapping written at *indent* (0 = workflow, 4 = job)."""
    lines = _workflow_lines()
    for i, line in enumerate(lines):
        if line.strip() == "permissions:" and len(line) - len(line.lstrip()) == indent:
            return _mapping_under(lines, i)
    raise AssertionError(f"no `permissions:` block at indent {indent} in {WORKFLOW}")


def _dispatch_inputs() -> dict[str, dict[str, str]]:
    """The `workflow_dispatch` inputs, as name -> its own mapping."""
    lines = _workflow_lines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "inputs:"]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one `inputs:` block in {WORKFLOW}, found {len(starts)}")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    inputs = {}
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = len(lines[i]) - len(lines[i].lstrip())
        if line_indent <= indent:
            break
        if line_indent == indent + 2 and stripped.endswith(":"):
            inputs[stripped[:-1]] = _mapping_under(lines, i)
    return inputs


def _body(step_name: str) -> str:
    """A step's shell, checked to be runnable as extracted.

    An unexpanded `${{ ... }}` left in a body would be executed here as a bash
    brace group and quietly do something other than what Actions does, so a
    body that grows one has to be given substitution rather than run as is.
    """
    body = step_run_body(WORKFLOW, step_name)
    if "${{" in body:
        raise AssertionError(f"{step_name!r} interpolates an expression in its body: {body}")
    return body


def _step_environment(step_name: str, values: dict[str, str]) -> dict[str, str]:
    """The step's declared `env:` keys, filled in from *values*.

    Keyed off the workflow rather than off a literal dict so an env entry added
    to the step fails here instead of being silently unset when the body runs.
    """
    declared = step_env(WORKFLOW, step_name)
    unknown = sorted(set(declared) - set(values))
    if unknown:
        raise AssertionError(f"{step_name!r} declares env {unknown} this harness does not supply")
    return {key: values[key] for key in declared}


class _StageRun:
    """The result of executing *Stage the wrapper and its checksum*."""

    def __init__(self, proc: subprocess.CompletedProcess, workspace: Path) -> None:
        self.proc = proc
        self.workspace = workspace
        self.dist = workspace / "dist"

    @property
    def staged(self) -> set[str]:
        return {path.name for path in self.dist.iterdir()} if self.dist.is_dir() else set()

    @property
    def checksum_text(self) -> str:
        return (self.dist / "aib.sha256").read_text()


def _workspace(tmp: Path) -> Path:
    """A checkout-shaped directory holding the repository's real wrapper."""
    workspace = tmp / "workspace"
    (workspace / "contrib").mkdir(parents=True)
    (workspace / "contrib/aib").write_bytes((ROOT / "contrib/aib").read_bytes())
    return workspace


def run_stage_step(tmp: Path, *, disagreeing_checksum: bool = False) -> _StageRun:
    """Run the staging body against a copy of the repository's `contrib/aib`."""
    workspace = _workspace(tmp)
    env = dict(os.environ)
    if disagreeing_checksum:
        bin_dir = tmp / "stub-bin"
        bin_dir.mkdir()
        _write_stub(bin_dir, "sha256sum", DISAGREEING_SHA256SUM_STUB)
        env["REAL_SHA256SUM"] = subprocess.run(
            ["bash", "-c", "command -v sha256sum"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    proc = subprocess.run(
        ["bash", "-c", _body(STAGE_STEP)],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
    )
    return _StageRun(proc, workspace)


class _UploadRun:
    """The result of executing *Attach to the release*."""

    def __init__(self, proc: subprocess.CompletedProcess, log: Path) -> None:
        self.proc = proc
        self.calls = []
        self.tokens = []
        for line in log.read_text().splitlines() if log.exists() else []:
            if line.startswith("GH_TOKEN="):
                self.tokens.append(line[len("GH_TOKEN=") :])
            else:
                self.calls.append(line.rstrip("\t").split("\t"))


def run_upload_step(workspace: Path, tmp: Path, *, tag: str = TAG, gh_exit: int = 0) -> _UploadRun:
    """Run the upload body with a recording `gh` on the PATH."""
    bin_dir = tmp / "gh-bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "gh", GH_STUB)
    log = tmp / "gh.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(log)
    env["GH_EXIT"] = str(gh_exit)
    env.update(_step_environment(UPLOAD_STEP, {"GH_TOKEN": "ghs_stub-token", "TAG": tag}))
    proc = subprocess.run(
        ["bash", "-c", _body(UPLOAD_STEP)],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
    )
    return _UploadRun(proc, log)


def run_summarize_step(workspace: Path, tmp: Path, *, tag: str = TAG, seed: str = "") -> tuple[subprocess.CompletedProcess, str]:
    """Run the summary body, returning the process and the summary file."""
    summary = tmp / "step-summary.md"
    summary.write_text(seed)
    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    env.update(_step_environment(SUMMARIZE_STEP, {"TAG": tag}))
    proc = subprocess.run(
        ["bash", "-c", _body(SUMMARIZE_STEP)],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, summary.read_text()


def documented_asset_names() -> set[str]:
    """The release assets the install docs fetch, read out of their URLs."""
    names: set[str] = set()
    for doc in ("README.md", "docs/installing.md"):
        names.update(_ASSET_URL.findall((ROOT / doc).read_text()))
    return names


def documented_verification_command() -> str:
    """The command the docs pipe `aib.sha256` into, taken from README.md."""
    for line in (ROOT / "README.md").read_text().splitlines():
        if f"{RELEASE_DOWNLOAD}/aib.sha256" in line and "|" in line:
            command = line.split("|", 1)[1].strip()
            return command.removesuffix("&&").strip()
    raise AssertionError("README.md does not pipe the published checksum into anything")


class StageStepTests(unittest.TestCase):
    """The pair the release carries is made here, or it is never made."""

    def test_the_staged_wrapper_is_the_wrapper_in_this_checkout(self) -> None:
        # The asset is what users run as root with their GitHub credential
        # forwarded into it. Anything that made `dist/aib` differ from
        # `contrib/aib` -- a rename, a filter, a different source path --
        # would publish something no test in this repository has ever read.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            self.assertEqual(
                (run.dist / "aib").read_bytes(),
                (ROOT / "contrib/aib").read_bytes(),
            )

    def test_the_checksum_is_of_the_wrapper_it_ships_beside(self) -> None:
        # The digest is computed by the step; recomputing it here from the
        # repository's own bytes is what makes "the pair always agrees" a
        # measurement rather than a claim in a comment.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            digest, _, name = run.checksum_text.strip().partition("  ")
        self.assertEqual(digest, hashlib.sha256((ROOT / "contrib/aib").read_bytes()).hexdigest())
        self.assertEqual(name, "aib")

    def test_the_checksum_names_the_file_the_docs_download(self) -> None:
        # `sha256sum -c` matches on the path recorded in the file, and the
        # documented install pipes the checksum in beside a download named
        # `aib` in the current directory. A checksum recording `dist/aib` --
        # what dropping the `cd dist` subshell produces -- verifies here and
        # fails for every user, with `sha256sum: aib: No such file`.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            downloaded = Path(tmp) / "download"
            downloaded.mkdir()
            (downloaded / "aib").write_bytes((run.dist / "aib").read_bytes())
            verify = subprocess.run(
                ["bash", "-c", documented_verification_command()],
                cwd=str(downloaded),
                input=run.checksum_text,
                capture_output=True,
                text=True,
            )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("aib: OK", verify.stdout)

    def test_a_pair_that_does_not_agree_stops_the_job(self) -> None:
        # The step's own comment says the verify line "fails the job rather
        # than uploading a pair that does not agree". Without it -- or without
        # `set -euo pipefail`, which is what turns a failed check inside a
        # subshell into a failed step -- the body runs to the end and the
        # upload step attaches a checksum that matches nothing.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp), disagreeing_checksum=True)
        self.assertNotEqual(run.proc.returncode, 0, run.proc.stdout)
        self.assertIn("FAILED", run.proc.stdout + run.proc.stderr)
        # Stopped at the check: the digest was never printed as a result.
        self.assertNotIn("0000000000000000", run.proc.stdout)

    def test_the_job_log_records_the_checksum_it_published(self) -> None:
        # The digest only exists on the release afterwards. Printing it is what
        # lets a maintainer compare a suspect download against the run that
        # produced it without re-deriving it from a tag.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            self.assertIn(run.checksum_text.strip(), run.proc.stdout)

    def test_staging_produces_exactly_the_two_release_assets(self) -> None:
        # A third file in `dist/` would not be uploaded (the upload names its
        # files), and a missing one fails the upload at release time.
        with tempfile.TemporaryDirectory() as tmp:
            run = run_stage_step(Path(tmp))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr)
            self.assertEqual(run.staged, {"aib", "aib.sha256"})


class UploadStepTests(unittest.TestCase):
    """The job's only write, and the only place the tag is used."""

    def _staged_upload(self, tmp: Path, **kwargs) -> tuple[_StageRun, _UploadRun]:
        stage = run_stage_step(tmp)
        self.assertEqual(stage.proc.returncode, 0, stage.proc.stderr)
        return stage, run_upload_step(stage.workspace, tmp, **kwargs)

    def test_the_upload_attaches_exactly_what_staging_produced(self) -> None:
        # Both sides are computed: the file names come out of `dist/` after the
        # step before this one ran, and the uploaded paths come out of the argv
        # `gh` was given. A rename on either side leaves the other behind.
        with tempfile.TemporaryDirectory() as tmp:
            stage, upload = self._staged_upload(Path(tmp))
            self.assertEqual(upload.proc.returncode, 0, upload.proc.stderr)
            self.assertEqual(len(upload.calls), 1, upload.calls)
            argv = upload.calls[0]
            self.assertEqual(argv[1:4], ["release", "upload", TAG])
            uploaded = argv[4:-1]
            self.assertEqual(
                sorted(uploaded),
                sorted(f"dist/{name}" for name in stage.staged),
            )
            for path in uploaded:
                self.assertTrue((stage.workspace / path).is_file(), path)

    def test_the_uploaded_assets_are_the_ones_the_docs_fetch(self) -> None:
        # The docs give users a URL per asset; this job decides what is there
        # to fetch. Neither side can be checked against itself, so the names
        # are taken from the executed argv and from the documented URLs and
        # compared -- a rename here 404s the recommended install.
        with tempfile.TemporaryDirectory() as tmp:
            _, upload = self._staged_upload(Path(tmp))
        uploaded = {Path(arg).name for arg in upload.calls[0][4:-1]}
        self.assertEqual(uploaded, documented_asset_names())

    def test_a_rerun_replaces_the_assets_instead_of_failing(self) -> None:
        # `workflow_dispatch` exists for "a release published before this
        # workflow existed, or after a failed run". Without `--clobber` the
        # second attempt fails on names that already exist, which makes the
        # documented recovery path the one thing the job cannot do.
        with tempfile.TemporaryDirectory() as tmp:
            _, upload = self._staged_upload(Path(tmp))
        self.assertEqual(upload.calls[0][-1], "--clobber")

    def test_the_upload_is_given_a_token(self) -> None:
        # `gh release upload` authenticates; with no `GH_TOKEN` in the step's
        # env it fails at release time and nowhere else.
        with tempfile.TemporaryDirectory() as tmp:
            _, upload = self._staged_upload(Path(tmp))
        self.assertEqual(step_env(WORKFLOW, UPLOAD_STEP)["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(upload.tokens, ["ghs_stub-token"])

    def test_a_failed_upload_fails_the_job(self) -> None:
        # The release is the thing being published. A silent failure here ships
        # a release whose documented install 404s.
        with tempfile.TemporaryDirectory() as tmp:
            _, upload = self._staged_upload(Path(tmp), gh_exit=1)
        self.assertNotEqual(upload.proc.returncode, 0)

    def test_the_tag_uploaded_to_is_the_tag_checked_out(self) -> None:
        # The assets are generated from the checked-out tree and attached to a
        # tag named separately. If the two expressions drift, the job attaches
        # one release's wrapper to another release, and both look published.
        self.assertEqual(step_with(WORKFLOW, CHECKOUT_STEP)["ref"], TAG_EXPRESSION)
        self.assertEqual(step_env(WORKFLOW, UPLOAD_STEP)["TAG"], TAG_EXPRESSION)
        self.assertEqual(step_env(WORKFLOW, SUMMARIZE_STEP)["TAG"], TAG_EXPRESSION)

    def test_the_dispatch_fallback_names_a_required_input(self) -> None:
        # On `workflow_dispatch` there is no release payload, so the fallback
        # half of the expression is the whole tag. An input that is optional,
        # or named something else, resolves to empty -- and an empty tag
        # uploads to no release while the step still exits 0.
        inputs = _dispatch_inputs()
        self.assertIn("tag", inputs)
        self.assertEqual(inputs["tag"].get("required"), "true")
        self.assertIn("inputs.tag", TAG_EXPRESSION)


class SummarizeStepTests(unittest.TestCase):
    """What a failed publish leaves behind."""

    def test_the_summary_records_the_published_checksum_and_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = run_stage_step(Path(tmp))
            self.assertEqual(stage.proc.returncode, 0, stage.proc.stderr)
            proc, summary = run_summarize_step(stage.workspace, Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("## aib wrapper", summary)
            self.assertIn(TAG, summary)
            self.assertIn(stage.checksum_text.strip(), summary)

    def test_the_summary_survives_a_run_that_staged_nothing(self) -> None:
        # `if: always()` means this step runs after the staging step failed, so
        # the run it most needs to explain is the one with no `dist/` at all.
        # Without the fallback the summary shows an empty code block, and
        # without `2>/dev/null` the reader gets a `cat` error in place of the
        # explanation.
        self.assertEqual(step_if(WORKFLOW, SUMMARIZE_STEP), "always()")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(Path(tmp))
            proc, summary = run_summarize_step(workspace, Path(tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("(no checksum produced)", summary)
        self.assertIn(TAG, summary)

    def test_the_summary_appends_to_what_the_run_already_wrote(self) -> None:
        # `$GITHUB_STEP_SUMMARY` accumulates across a job. Truncating it would
        # drop every earlier step's summary on a job that gains one.
        marker = "earlier step's summary\n"
        with tempfile.TemporaryDirectory() as tmp:
            stage = run_stage_step(Path(tmp))
            self.assertEqual(stage.proc.returncode, 0, stage.proc.stderr)
            proc, summary = run_summarize_step(stage.workspace, Path(tmp), seed=marker)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(summary.startswith(marker), summary)


class JobPermissionTests(unittest.TestCase):
    """Uploading a release asset is a write to repository contents."""

    def test_the_workflow_defaults_to_read_only(self) -> None:
        self.assertEqual(_permissions(0), {"contents": "read"})

    def test_the_publishing_job_asks_for_contents_write(self) -> None:
        # Release assets are contents. Read-only here fails the upload with a
        # 403 on a release that is already published, and the recovery is a
        # dispatch rerun rather than anything automatic.
        self.assertEqual(_permissions(4), {"contents": "write"})


if __name__ == "__main__":
    unittest.main()

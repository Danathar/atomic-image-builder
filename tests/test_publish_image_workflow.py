"""Execute the `run:` bodies of .github/workflows/publish-image.yml.

publish-image.yml is the only path by which anything reaches
ghcr.io/danathar/atomic-image-builder: the script, its ACTION_PINS table and
the bundled template snapshots all reach users through that image. Nothing in
the suite referenced this workflow at all -- the dir-wide scans in
tests/test_workflow_dependencies.py read it to compare action pins, and
tests/test_atomic_image_builder.py matches its path inside the cosign identity
regex, but neither runs a line of its shell.

Two of its decisions are the kind that fail silently:

* Only a build of `main` may tag the image `latest`. A release is published
  from a tag ref, and one cut from an older commit must not drag `latest`
  backwards. Losing that guard republishes an old `latest` with a green run.
* The image reference is lowercased from the owner login, which is `Danathar`.
  GHCR rejects an uppercase reference, so a regression here fails the push --
  but the tags string and the signing reference are built from that value in
  two different steps, and only one of them is exercised by a push.

The version in the image tags is also a cross-artifact seam: the step reads
field 2 of `atomic_image_builder.py --version`. That output format is the
script's to change, and nothing else notices if it does -- the tag would
silently become `aib-tool` or empty. The real script is used here rather than
a stub, so the seam is asserted end to end.

The step's shell is extracted from the workflow rather than copied here, so
editing publish-image.yml re-runs these assertions against the edit. `cosign`
is a recording stub on PATH: the signing step never reaches Sigstore, and the
case asserts on the argv it would have sent.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_env, step_run_body

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-image.yml"
META_STEP = "Determine version, short SHA, and lowercased owner"
SIGN_STEP = "Sign the published image"

# The signing step's only command. Recorded as tab-separated argv so a case
# can assert on exact arguments rather than on a flattened command line.
COSIGN_STUB = """#!/usr/bin/env bash
printf '%s\\t' "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
exit "${STUB_COSIGN_EXIT:-0}"
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def run_meta_step(
    *,
    ref: str = "refs/heads/main",
    owner: str = "Danathar",
) -> tuple[subprocess.CompletedProcess, dict[str, str], str]:
    """Run the step in a throwaway git repo holding the real script.

    Returns the process, the parsed `$GITHUB_OUTPUT`, and the short SHA the
    repo actually has, so a case can compare the two rather than re-deriving
    the value the step was supposed to read.
    """
    script = step_run_body(PUBLISH_WORKFLOW, META_STEP)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # The step runs `git rev-parse --short HEAD` against the checkout, and
        # reads the version out of the script that ships in the image.
        (tmp_path / "atomic_image_builder.py").write_bytes((ROOT / "atomic_image_builder.py").read_bytes())
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "add", "atomic_image_builder.py")
        _git(tmp_path, "commit", "-q", "-m", "initial")
        short_sha = _git(tmp_path, "rev-parse", "--short", "HEAD")

        output_file = tmp_path / "github_output"
        output_file.touch()

        env = dict(os.environ)
        env["GITHUB_REF"] = ref
        env["GITHUB_REPOSITORY_OWNER"] = owner
        env["GITHUB_OUTPUT"] = str(output_file)

        proc = subprocess.run(
            ["bash", "-c", script],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        outputs = {}
        for line in output_file.read_text().splitlines():
            if line:
                key, _, value = line.partition("=")
                outputs[key] = value
    return proc, outputs, short_sha


class MetaStepTests(unittest.TestCase):
    """The step that decides the image's tags."""

    def test_the_harness_supplies_the_variables_the_step_reads(self) -> None:
        # The step declares no `env:` block: everything it reads comes from
        # the runner. Pinning that here keeps the harness honest -- a variable
        # moved into an `env:` block would otherwise still be picked up from
        # this test's own environment and pass for the wrong reason.
        self.assertEqual(step_env(PUBLISH_WORKFLOW, META_STEP), {})
        script = step_run_body(PUBLISH_WORKFLOW, META_STEP)
        for variable in ("GITHUB_REF", "GITHUB_REPOSITORY_OWNER", "GITHUB_OUTPUT"):
            self.assertIn(variable, script)

    def test_a_build_of_main_claims_latest(self) -> None:
        proc, outputs, short_sha = run_meta_step(ref="refs/heads/main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["tags"].split(), ["latest", outputs["version"], short_sha])

    def test_a_release_tag_ref_does_not_claim_latest(self) -> None:
        # The release event's ref is refs/tags/<tag>. A release cut from an
        # older commit still gets the version and SHA tags; it must not move
        # `latest` off the tip of main.
        proc, outputs, short_sha = run_meta_step(ref="refs/tags/v0.9.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("latest", outputs["tags"].split())
        self.assertEqual(outputs["tags"].split(), [outputs["version"], short_sha])

    def test_a_dispatch_from_another_branch_does_not_claim_latest(self) -> None:
        # workflow_dispatch can be aimed at any branch, so the guard is not
        # only about releases.
        _, outputs, _ = run_meta_step(ref="refs/heads/some-branch")
        self.assertNotIn("latest", outputs["tags"].split())

    def test_the_owner_is_lowercased_for_the_registry_path(self) -> None:
        # GHCR rejects an uppercase image reference and the owner login is
        # `Danathar`, so the lowercasing is load-bearing on every push.
        _, outputs, _ = run_meta_step(owner="Danathar")
        self.assertEqual(outputs["owner_lc"], "danathar")

    def test_an_already_lowercase_owner_is_left_alone(self) -> None:
        _, outputs, _ = run_meta_step(owner="danathar")
        self.assertEqual(outputs["owner_lc"], "danathar")

    def test_the_version_tag_is_field_two_of_the_real_scripts_version_output(self) -> None:
        # The seam this file exists for: the step parses the script's own
        # `--version` output, so a change to that format silently changes the
        # published image's version tag.
        printed = subprocess.run(
            ["python3", str(ROOT / "atomic_image_builder.py"), "--version"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertGreaterEqual(len(printed), 2, f"--version printed {printed!r}, no field 2 to tag with")
        _, outputs, _ = run_meta_step()
        self.assertEqual(outputs["version"], printed[1])
        # And it has to look like a version, not like the program name: an
        # output of "0.9.5" alone would make field 2 empty, and "aib-tool
        # (0.9.5)" would tag the image "(0.9.5)".
        self.assertRegex(outputs["version"], r"^\d+\.\d+\.\d+$")

    def test_every_output_the_later_steps_read_is_written(self) -> None:
        _, outputs, _ = run_meta_step()
        self.assertEqual(sorted(outputs), ["owner_lc", "short_sha", "tags", "version"])
        # Two of the four are read by a later step: `tags` by the build and
        # `owner_lc` by the push and the signing step's `env:`. A missing one
        # resolves to the empty string there rather than failing the run, so
        # the image would be built untagged or pushed to `ghcr.io//...`.
        workflow = PUBLISH_WORKFLOW.read_text()
        consumed = {name for name in outputs if f"steps.meta.outputs.{name}" in workflow}
        self.assertEqual(consumed, {"tags", "owner_lc"})
        # `version` and `short_sha` are folded into `tags` and read by nothing
        # else; they are published as separate outputs for the run's own
        # output view. Asserting that here keeps a later step that starts
        # reading one from looking like it was always covered.
        self.assertEqual(outputs["tags"].split()[-2:], [outputs["version"], outputs["short_sha"]])

    def test_the_short_sha_is_the_commit_being_published(self) -> None:
        _, outputs, short_sha = run_meta_step()
        self.assertEqual(outputs["short_sha"], short_sha)


class SignStepTests(unittest.TestCase):
    """The step that signs what was actually pushed."""

    def run_sign_step(
        self,
        *,
        image: str = "ghcr.io/danathar/atomic-image-builder",
        digest: str = "sha256:" + "ab" * 32,
        cosign_exit: int = 0,
    ) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
        script = step_run_body(PUBLISH_WORKFLOW, SIGN_STEP)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            cosign_stub = fake_bin / "cosign"
            cosign_stub.write_text(COSIGN_STUB)
            cosign_stub.chmod(0o755)
            log = tmp_path / "cosign.log"
            log.touch()

            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["IMAGE"] = image
            env["DIGEST"] = digest
            env["STUB_LOG"] = str(log)
            env["STUB_COSIGN_EXIT"] = str(cosign_exit)

            proc = subprocess.run(
                ["bash", "-c", script],
                env=env,
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
            )
            calls = [line.split("\t")[:-1] for line in log.read_text().splitlines() if line]
        return proc, calls

    def test_the_step_signs_the_digest_it_was_handed(self) -> None:
        digest = "sha256:" + "cd" * 32
        proc, calls = self.run_sign_step(digest=digest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            calls,
            [["sign", "-y", f"ghcr.io/danathar/atomic-image-builder@{digest}"]],
        )

    def test_the_step_declares_the_pushed_digest_and_the_lowercased_owner(self) -> None:
        env = step_env(PUBLISH_WORKFLOW, SIGN_STEP)
        self.assertEqual(sorted(env), ["DIGEST", "IMAGE"])
        # Signing a tag would sign whatever that tag points at when cosign
        # resolves it, which the next merge rewrites. The digest output of the
        # push step is the artifact this run actually produced.
        self.assertEqual(env["DIGEST"], "${{ steps.push-to-ghcr.outputs.digest }}")
        self.assertEqual(
            env["IMAGE"],
            "ghcr.io/${{ steps.meta.outputs.owner_lc }}/atomic-image-builder",
        )

    def test_the_reference_signed_is_a_digest_and_never_a_tag(self) -> None:
        _, calls = self.run_sign_step()
        self.assertEqual(len(calls), 1)
        reference = calls[0][-1]
        self.assertRegex(reference, r"@sha256:[0-9a-f]{64}$")
        # `:` after the last `/` would be a tag reference.
        self.assertNotIn(":", reference.rsplit("/", 1)[-1].split("@", 1)[0])

    def test_a_failed_signature_fails_the_step(self) -> None:
        # `set -euo pipefail`: an image that was pushed but not signed must
        # not leave a green run behind, because `contrib/aib` verifies the
        # signature before it will run the image.
        proc, calls = self.run_sign_step(cosign_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(len(calls), 1)

    def test_an_unset_digest_fails_rather_than_signing_the_bare_image(self) -> None:
        # `set -u`: if the push step produced no digest output, the reference
        # would be `<image>@` -- close enough to a valid argument that a
        # lenient shell would hand it to cosign.
        script = step_run_body(PUBLISH_WORKFLOW, SIGN_STEP)
        env = dict(os.environ)
        env["IMAGE"] = "ghcr.io/danathar/atomic-image-builder"
        env.pop("DIGEST", None)
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertRegex(proc.stderr, r"DIGEST.*unbound variable")


class WorkflowShapeTests(unittest.TestCase):
    def test_the_tags_output_feeds_the_build_step(self) -> None:
        # The tags string is computed as a space-separated list because
        # buildah-build takes it that way; a comma-separated list would build
        # one image tagged "latest,0.9.5,abc1234".
        workflow = PUBLISH_WORKFLOW.read_text()
        self.assertIn("tags: ${{ steps.meta.outputs.tags }}", workflow)
        _, outputs, _ = run_meta_step()
        self.assertNotIn(",", outputs["tags"])
        self.assertEqual(len(outputs["tags"].split()), 3)

    def test_the_push_registry_uses_the_lowercased_owner(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text()
        self.assertRegex(
            workflow,
            re.compile(r"^\s+registry: ghcr\.io/\$\{\{ steps\.meta\.outputs\.owner_lc \}\}$", re.MULTILINE),
        )


if __name__ == "__main__":
    unittest.main()

"""
Script: tests/test_brew_setup_staging.py
What: Tests the Containerfile step that confines the ublue-os/brew payload's brew-setup.service to a private /tmp.
Doing: Extracts the step the generators emit, runs it against fixture unit files reproducing the payload's real one, and checks both the drop-in it writes and the payload shapes it refuses.
Why: brew-setup.service stages a 154 MB tarball through /tmp as root with no `PrivateTmp=`, and /tmp on a booted system is a world-writable tmpfs, so the staging path is a name any local account can claim before the unit first runs.
Goal: Keep the drop-in and the payload check that backstops it from drifting apart, and prove each one by behaviour rather than by substring.

The unit is not in this repository at any revision -- it arrives from an image
built somewhere else, named by a mutable tag -- so nothing here can assert what
it contains by reading a tracked file. What can be pinned is what the generated
build step does to a tree that holds one, which is why the step is executed here
instead of pattern-matched. PAYLOAD_UNIT reproduces the unit the payload ships
today; the staging check is what covers the ones it has not shipped yet.

The one thing no tier here can prove is that a single private /tmp is shared
across every `ExecStart=` of one `Type=oneshot` invocation. That is systemd's
documented behaviour and nothing in this suite boots an image. If it were false,
first boot would report brew-setup.service failed because `cp` found nothing to
copy -- loud, not silent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atomic_image_builder import (  # noqa: E402
    BREW_SETUP_DROPIN,
    BREW_SETUP_UNIT,
    brew_setup_staging_run_lines,
)

# What the payload actually ships, at the digest ghcr.io/ublue-os/brew:latest
# resolved to when this was written. Reproduced whole rather than trimmed: the
# staging check reads ExecStart= lines and nothing else, so which lines are
# present and which are merely Condition= is the distinction under test.
PAYLOAD_UNIT = """\
[Unit]
Description=Setup Brew
Wants=basic.target
After=basic.target
ConditionPathExists=!/etc/.linuxbrew
ConditionPathExists=!/home/linuxbrew/.linuxbrew
ConditionPathExists=/usr/share/homebrew.tar.zst

[Service]
Type=oneshot
ExecStart=/usr/bin/mkdir -p /tmp/homebrew
ExecStart=/usr/bin/mkdir -p /home/linuxbrew
ExecStart=/usr/bin/tar --zstd -xf /usr/share/homebrew.tar.zst -C /tmp/homebrew
ExecStart=/usr/bin/cp -R -n /tmp/homebrew/home/linuxbrew/.linuxbrew /home/linuxbrew
ExecStart=/usr/bin/chown -R 1000:1000 /home/linuxbrew
ExecStart=/usr/bin/rm -rf /tmp/homebrew
ExecStart=/usr/bin/touch /etc/.linuxbrew

[Install]
WantedBy=default.target multi-user.target
"""

# Both paths the step touches live under this one, so re-pointing it is a single
# substitution that cannot leave half the step aimed at the real filesystem.
SYSTEM_UNIT_DIR = str(PurePosixPath(BREW_SETUP_UNIT).parent)


def step_script(root: Path) -> str:
    """Return the generated RUN as a shell script rooted at ``root``.

    The step names absolute system directories, which a test cannot write to,
    so they are re-pointed into a temporary tree. The /tmp and /var/tmp paths
    it greps *for* are text inside the fixture unit and are deliberately left
    alone -- rewriting those would test the harness instead of the step.
    """
    script = "\n".join(brew_setup_staging_run_lines()).removeprefix("RUN ")
    return script.replace(SYSTEM_UNIT_DIR, f"{root}{SYSTEM_UNIT_DIR}")


class BrewSetupStagingStepTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("sh") is None:  # pragma: no cover - every supported host has one
            self.skipTest("no POSIX shell available")
        self.root = Path(tempfile.mkdtemp(prefix="aib-brew-staging."))
        self.addCleanup(shutil.rmtree, self.root, True)

    def seed_unit(self, text: str = PAYLOAD_UNIT) -> Path:
        target = self.root / BREW_SETUP_UNIT.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def run_step(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", step_script(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def dropin(self) -> Path:
        return self.root / BREW_SETUP_DROPIN.lstrip("/")

    def test_the_step_writes_the_drop_in_for_the_payloads_own_unit(self) -> None:
        self.seed_unit()
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dropin().exists())
        self.assertEqual(self.dropin().stat().st_mode & 0o777, 0o644)

    def test_the_drop_in_sets_private_tmp_and_overrides_no_command(self) -> None:
        # A drop-in that also carried an ExecStart= would be a copy of the
        # payload's command chain under another name, and would drift silently
        # the next time the payload moves. One directive, and only one.
        self.seed_unit()
        self.assertEqual(self.run_step().returncode, 0)
        text = self.dropin().read_text()
        directives = [
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(directives, ["[Service]", "PrivateTmp=yes"])

    def test_a_payload_without_the_unit_fails_the_build(self) -> None:
        # No unit means the drop-in applies to nothing, and shipping it anyway
        # would leave an image that looks confined and is not.
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer ships", result.stderr)
        self.assertFalse(self.dropin().exists())

    def test_staging_moved_out_of_private_tmps_reach_fails_the_build(self) -> None:
        # PrivateTmp= confines /tmp and /var/tmp and nothing else, so a payload
        # that staged in /run would be shipped with a drop-in that protects it
        # from nothing. The build stops instead, and prints what it read.
        self.seed_unit(PAYLOAD_UNIT.replace("/tmp/homebrew", "/run/homebrew"))
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("/tmp or /var/tmp", result.stderr)
        self.assertIn("/run/homebrew", result.stderr)
        self.assertFalse(self.dropin().exists())

    def test_staging_in_var_tmp_is_accepted(self) -> None:
        self.seed_unit(PAYLOAD_UNIT.replace("/tmp/homebrew", "/var/tmp/homebrew"))
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dropin().exists())

    def test_staging_directly_in_tmp_is_accepted(self) -> None:
        self.seed_unit(PAYLOAD_UNIT.replace("-C /tmp/homebrew", "-C /tmp"))
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_path_that_merely_starts_like_tmp_does_not_count(self) -> None:
        # /var/lib/brew-tmp is not under either directory PrivateTmp= confines.
        self.seed_unit(PAYLOAD_UNIT.replace("/tmp/homebrew", "/var/lib/brew-tmp"))
        self.assertEqual(self.run_step().returncode, 1)

    def test_tmp_named_outside_an_exec_line_does_not_count(self) -> None:
        # The check reads ExecStart= lines only. A Condition= or a comment that
        # names /tmp says nothing about where the payload stages, and standing
        # in for one would be the check passing on the strength of a string.
        moved = PAYLOAD_UNIT.replace("/tmp/homebrew", "/run/homebrew")
        moved = moved.replace(
            "ConditionPathExists=/usr/share/homebrew.tar.zst",
            "ConditionPathExists=/tmp/homebrew\n# staged under /tmp until recently",
        )
        self.assertEqual(self.run_step().returncode, 1)
        self.seed_unit(moved)
        self.assertEqual(self.run_step().returncode, 1)

    def test_running_the_step_twice_is_clean(self) -> None:
        # An update run re-executes the generated Containerfile from the top,
        # and a step that failed on its own previous output would break exactly
        # the repositories this fix is meant to reach.
        self.seed_unit()
        self.assertEqual(self.run_step().returncode, 0)
        second = self.run_step()
        self.assertEqual(second.returncode, 0, second.stderr)


class BrewSetupStagingEmissionTests(unittest.TestCase):
    """The step has to reach all three writers of the brew block, or the one it misses ships unconfined."""

    def setUp(self) -> None:
        import atomic_image_builder

        self.app = atomic_image_builder.App()
        self.app.config.base_image_uri = "quay.io/fedora-ostree-desktops/silverblue:43"
        self.app.config.repo_name = "my-image"
        self.app.config.image_desc = "Test image"

    def assert_confined(self, text: str) -> None:
        self.assertIn(f"> {BREW_SETUP_DROPIN}", text)
        self.assertIn("PrivateTmp=yes", text)
        # Ordering: the COPY is what puts the unit in the image, so a check
        # that ran before it would read an empty path and fail every build.
        self.assertLess(text.index("/system_files /"), text.index("PrivateTmp=yes"))

    def test_the_from_scratch_containerfile_confines_the_unit(self) -> None:
        self.app.config.brew_enabled = True
        self.assert_confined(self.app.generate_containerfile())

    def test_the_patched_containerfile_confines_the_unit(self) -> None:
        self.app.config.brew_enabled = True
        existing = (
            "FROM scratch AS ctx\n"
            "COPY build_files /\n"
            "\n"
            "FROM quay.io/fedora-ostree-desktops/silverblue:43\n"
            "\n"
            "RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\\n"
            "    /ctx/build.sh\n"
        )
        self.assert_confined(self.app.render_containerfile(existing))

    def test_the_bluebuild_recipe_confines_the_unit(self) -> None:
        self.app.config.method = "bluebuild"
        self.app.config.brew_enabled = True
        recipe = self.app.generate_recipe()
        self.assert_confined(recipe)
        # Its own literal block, one per RUN: the preset, the login-fragment
        # step, and this one. Folded in with the step above it, the two RUNs
        # would reach the build shell as a single snippet.
        self.assertEqual(recipe.count("      - |"), 3)
        self.assertEqual(recipe.count("        RUN "), 3)

    def test_no_brew_means_no_drop_in(self) -> None:
        # Nothing brings brew-setup.service in, so a step that checked for it
        # would fail every build of an image without Homebrew.
        self.app.config.brew_enabled = False
        self.assertNotIn(BREW_SETUP_DROPIN, self.app.generate_containerfile())

    def test_drop_in_lines_carry_no_apostrophe(self) -> None:
        # Every line is emitted as a single-quoted printf argument, so one
        # apostrophe would close the quote and hand the rest of the line to
        # the build shell. The generator refuses rather than emit that; this
        # is the assertion that the refusal is not the only thing between the
        # repo and a broken Containerfile.
        emitted = "\n".join(brew_setup_staging_run_lines())
        quoted = [line.strip() for line in emitted.splitlines() if line.strip().startswith("'")]
        self.assertTrue(quoted)
        for line in quoted:
            self.assertEqual(line.count("'"), 2, line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

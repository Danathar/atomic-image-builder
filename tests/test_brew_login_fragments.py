"""
Script: tests/test_brew_login_fragments.py
What: Tests the Containerfile step that keeps the ublue-os/brew payload's login-shell fragments out of every generated image.
Doing: Extracts the step the generators emit, runs it against fixture trees built from the payload's real contents, and sources the fragment it installs.
Why: `COPY --from=<brew image> /system_files /` lands files this tool does not write, and brew-setup.service ends with `chown -R 1000:1000 /home/linuxbrew`, so a login shell that sources them runs user-writable code as root.
Goal: Keep the removal, the replacement, and the sweep that backstops both from drifting apart, and prove each one by behaviour rather than by substring.

The fragments are not in this repository at any revision -- they arrive from an
image built somewhere else, named by a mutable tag -- so nothing here can
assert their absence by reading a tracked file. What can be pinned is what the
generated build step does to a directory tree, which is why the step is
executed here instead of pattern-matched. The fixtures reproduce the three
fragments the payload ships today; the sweep is what covers the ones it has not
shipped yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atomic_image_builder import (  # noqa: E402
    BREW_LOGIN_FRAGMENTS,
    BREW_LOGIN_SHELL_DIRS,
    BREW_PATH_FRAGMENT,
    brew_login_fragment_run_lines,
)

# What the payload actually ships, at the digest ghcr.io/ublue-os/brew:latest
# resolved to when this was written. Only the part that matters is reproduced:
# each one executes something out of /home/linuxbrew/.linuxbrew.
PAYLOAD_FRAGMENTS = {
    "/etc/profile.d/brew.sh": 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv | grep -Ev \'\\bPATH=\')"\n',
    "/etc/profile.d/brew-bash-completion.sh": (
        "/home/linuxbrew/.linuxbrew/bin/brew completions link > /dev/null\n"
        "for rc in /home/linuxbrew/.linuxbrew/etc/bash_completion.d/*; do . \"$rc\"; done\n"
    ),
    "/usr/share/fish/vendor_conf.d/ublue-brew.fish": "/home/linuxbrew/.linuxbrew/bin/brew shellenv fish | source\n",
}


def step_script(root: Path) -> str:
    """Return the generated RUN as a shell script rooted at ``root``.

    The step names absolute system directories, which a test cannot write to,
    so each one is re-pointed into a temporary tree. Only the paths the step
    reads and writes are rewritten; the /home/linuxbrew references it puts
    *into* the fragment are text inside single-quoted printf arguments and are
    deliberately left alone.
    """
    script = "\n".join(brew_login_fragment_run_lines()).removeprefix("RUN ")
    for directory in BREW_LOGIN_SHELL_DIRS:
        script = script.replace(directory, f"{root}{directory}")
    return script


class BrewLoginFragmentStepTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("sh") is None:  # pragma: no cover - every supported host has one
            self.skipTest("no POSIX shell available")
        self.root = Path(tempfile.mkdtemp(prefix="aib-brew-fragments."))
        self.addCleanup(shutil.rmtree, self.root, True)
        for directory in BREW_LOGIN_SHELL_DIRS:
            (self.root / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def write(self, path: str, text: str) -> Path:
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def seed_payload(self) -> None:
        for path, text in PAYLOAD_FRAGMENTS.items():
            self.write(path, text)

    def run_step(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", step_script(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_step_removes_every_fragment_the_payload_ships(self) -> None:
        self.seed_payload()
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in BREW_LOGIN_FRAGMENTS:
            self.assertFalse((self.root / path.lstrip("/")).exists(), path)

    def test_the_step_leaves_unrelated_login_fragments_alone(self) -> None:
        # The sweep greps for "brew" across whole directories, so a fragment
        # that has nothing to do with Homebrew must survive it untouched --
        # otherwise enabling Homebrew silently breaks the rest of the login
        # shell.
        self.seed_payload()
        unrelated = self.write("/etc/profile.d/colorls.sh", "export CLICOLOR=1\n")
        self.assertEqual(self.run_step().returncode, 0)
        self.assertEqual(unrelated.read_text(), "export CLICOLOR=1\n")

    def test_the_step_installs_a_replacement_that_executes_nothing(self) -> None:
        self.seed_payload()
        self.assertEqual(self.run_step().returncode, 0)
        installed = self.root / BREW_PATH_FRAGMENT.lstrip("/")
        text = installed.read_text()
        self.assertTrue(installed.exists())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        # Nothing out of the prefix is run: no command substitution, no eval,
        # no sourcing. That is the whole difference between this fragment and
        # the three it replaces. The fragment's own comments name those very
        # constructs to explain why they are absent, so only the code is read.
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("$(", "`", "eval", "source", "shellenv", "/bin/brew"):
            self.assertNotIn(forbidden, code)
        self.assertIn("[ -O /home/linuxbrew/.linuxbrew ]", text)

    def test_a_fragment_from_a_later_payload_digest_fails_the_build(self) -> None:
        # Deleting three names fixes the digest that shipped them. The payload
        # is pulled by a mutable tag from an image this tool does not build, so
        # the next one can add a fourth with nothing in this repository
        # changing; the build has to stop rather than publish it.
        self.seed_payload()
        self.write(
            "/etc/profile.d/brew-next.sh",
            'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"\n',
        )
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("brew-next.sh", result.stderr)

    def test_the_replacement_does_not_trip_its_own_sweep(self) -> None:
        # The fragment the step installs mentions brew itself, so a sweep that
        # did not exclude it would fail every build that ran the step twice --
        # which is what an update run does.
        self.seed_payload()
        self.assertEqual(self.run_step().returncode, 0)
        second = self.run_step()
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_the_step_survives_a_missing_login_shell_directory(self) -> None:
        # /etc/fish/conf.d does not exist on a base image without fish, and a
        # grep error there must not fail an otherwise clean build.
        self.seed_payload()
        shutil.rmtree(self.root / "etc/fish/conf.d")
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)


class BrewPathFragmentBehaviourTests(unittest.TestCase):
    """Source the installed fragment and check which accounts it acts for."""

    def setUp(self) -> None:
        if shutil.which("sh") is None:  # pragma: no cover
            self.skipTest("no POSIX shell available")
        self.root = Path(tempfile.mkdtemp(prefix="aib-brew-path."))
        self.addCleanup(shutil.rmtree, self.root, True)
        for directory in BREW_LOGIN_SHELL_DIRS:
            (self.root / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)
        subprocess.run(["sh", "-c", step_script(self.root)], capture_output=True, text=True, check=True)
        self.prefix = self.root / "prefix"
        # The fragment hard-codes the payload's prefix, which the test cannot
        # create. Re-point it at one it can, so ownership is a real check
        # against a real directory rather than a string comparison.
        installed = self.root / BREW_PATH_FRAGMENT.lstrip("/")
        installed.write_text(installed.read_text().replace("/home/linuxbrew/.linuxbrew", str(self.prefix)))
        self.fragment = installed

    def path_after_sourcing(self, start: str = "/usr/bin:/bin") -> str:
        result = subprocess.run(
            ["sh", "-c", f'PATH="{start}"; . "{self.fragment}"; printf %s "$PATH"'],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PATH": start},
        )
        return result.stdout

    def test_the_owner_of_the_prefix_gets_it_appended_to_path(self) -> None:
        (self.prefix / "bin").mkdir(parents=True)
        self.assertEqual(
            self.path_after_sourcing(),
            f"/usr/bin:/bin:{self.prefix}/bin:{self.prefix}/sbin",
            "the prefix is appended, so system binaries keep priority",
        )

    def test_sourcing_twice_does_not_repeat_the_prefix(self) -> None:
        (self.prefix / "bin").mkdir(parents=True)
        already = f"/usr/bin:/bin:{self.prefix}/bin"
        self.assertEqual(self.path_after_sourcing(already), already)

    def test_an_absent_prefix_leaves_path_alone(self) -> None:
        self.assertEqual(self.path_after_sourcing(), "/usr/bin:/bin")

    def test_a_prefix_owned_by_someone_else_leaves_path_alone(self) -> None:
        # The point of the -O test. Running the suite as root would make every
        # path pass it, so the case is only meaningful when it can be built.
        if os.geteuid() == 0:
            self.skipTest("running as root: -O is true for every path")
        (self.prefix / "bin").mkdir(parents=True)
        foreign = Path("/usr")
        installed = self.fragment.read_text().replace(str(self.prefix), str(foreign))
        self.fragment.write_text(installed)
        self.assertEqual(self.path_after_sourcing(), "/usr/bin:/bin")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

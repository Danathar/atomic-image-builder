"""The `env` block in `.claude/settings.json` makes git refuse a planted bare repository.

`.claude/settings.json` allows `Bash(git log:*)` without a prompt. A branch can
commit a bare repository as ordinary files (`evil/HEAD`, `evil/config`,
`evil/info/attributes`, `evil/objects/...`). With `evil/` as the working
directory, `git log -p` discovers it, and the repository's own config can name
a program for git to run -- here `diff.x.textconv`, switched on for every path
by `info/attributes`. The guard hook cannot see that config, so git itself has
to refuse the repository: the settings `env` sets `safe.bareRepository` to
`explicit` for every command an agent runs.

These tests plant that repository, show the program runs when the settings env
is absent (so the refusal is not vacuous), and show it is refused when the env
is applied. The checkout's own `.git/` is not bare and keeps working.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / ".claude/settings.json"

EXPECTED_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.bareRepository",
    "GIT_CONFIG_VALUE_0": "explicit",
}


def settings_env() -> dict[str, str]:
    return json.loads(SETTINGS.read_text(encoding="utf-8")).get("env", {})


class ClaudeSettingsGitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.marker = self.dir / "ran"
        self.victim = self.plant()

    def base_env(self) -> dict[str, str]:
        """A clean git environment: no user or system config, no inherited GIT_* overrides."""
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def git(self, cwd: Path, env: dict[str, str], *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)

    def must(self, cwd: Path, *args: str, stdin: str | None = None) -> str:
        result = self.git(cwd, self.base_env(), *args, stdin=stdin)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def plant(self) -> Path:
        """Clone a repository whose tree carries a committed bare repository `evil/`."""
        src = self.dir / "src"
        evil = src / "evil"
        self.must(self.dir, "init", "-q", str(src))
        self.must(src, "init", "-q", "--bare", "evil")
        blob = self.must(evil, "hash-object", "-w", "--stdin", stdin="hello\n")
        tree = self.must(evil, "mktree", stdin=f"100644 blob {blob}\tfile.txt\n")
        commit = self.must(evil, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "-m", "planted", tree)
        self.must(evil, "update-ref", "refs/heads/main", commit)
        self.must(evil, "symbolic-ref", "HEAD", "refs/heads/main")
        (evil / "info").mkdir(exist_ok=True)
        (evil / "info/attributes").write_text("* diff=x\n", encoding="utf-8")
        self.must(evil, "config", "diff.x.textconv", f"touch '{self.marker}'; cat")
        self.must(src, "add", "evil")
        self.must(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "planted-tree")
        self.must(self.dir, "clone", "-q", str(src), "victim")
        return self.dir / "victim"

    def test_settings_env_is_exactly_the_bare_repository_guard(self) -> None:
        self.assertEqual(settings_env(), EXPECTED_ENV)

    def test_planted_program_runs_without_the_settings_env(self) -> None:
        """Precondition: the refusal below means something only if this runs."""
        self.git(self.victim / "evil", self.base_env(), "log", "-p", "-1")
        self.assertTrue(self.marker.exists(), "without safe.bareRepository the planted textconv program should run")

    def test_planted_program_is_refused_with_the_settings_env(self) -> None:
        agent_env = {**self.base_env(), **settings_env()}
        for cwd, args in (
            (self.victim / "evil", ("log", "-p", "-1")),
            (self.victim, ("-C", "evil", "log", "-p", "-1")),
        ):
            with self.subTest(args=args):
                result = self.git(cwd, agent_env, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("safe.bareRepository", result.stderr)
                self.assertFalse(self.marker.exists(), f"git {' '.join(args)} ran the planted program")

    def test_checkout_own_repository_still_works_with_the_settings_env(self) -> None:
        agent_env = {**self.base_env(), **settings_env()}
        self.assertEqual(self.git(self.victim, agent_env, "log", "-1", "--format=%s").stdout.strip(), "planted-tree")
        self.assertEqual(self.git(self.victim, agent_env, "status", "--short").returncode, 0)


if __name__ == "__main__":
    unittest.main()

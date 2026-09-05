"""Keep every copy of the shellcheck command in step with the one CI gates on.

`.github/workflows/ci.yml`'s *Run shellcheck* step is the gate. Every other
copy -- the contributor guide, the PR checklist, the two agent briefs, the
ai-fix check table -- exists so a human or an agent can reproduce that gate
before pushing. A copy listing fewer files than the gate does not announce
itself: it passes, the reader ticks the box, and the run that matters goes red
afterwards on a file they were never told to lint. That is exactly how
CONTRIBUTING.md and .github/pull_request_template.md came to name four of the
seven files and to drop `-x`, while the four machine-facing copies carried
both.

The copies are found by scanning rather than from a list written out here, so
a seventh one added to any tracked Markdown or workflow file is covered the
day it is written rather than the day someone remembers this test.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ".github/workflows/ci.yml"

# The linter's name, its flags, then its files. A quote, a backtick or the end
# of the line ends the match, which is what terminates the copies embedded in
# Markdown and in ai-fix.yml's check table. Only spaces and tabs separate the
# arguments -- \s would swallow the newline and read the next line of the code
# block as more file names. Prose that merely mentions the linter's name
# matches this too; _calls() drops those.
_INVOCATION = re.compile(r"shellcheck((?:[ \t]+-\S+)*(?:[ \t]+[\w./*-]+)+)")


def _tracked_prose_and_workflows() -> list[str]:
    # Tracked files only, so a maintainer's untracked scratch notes cannot
    # turn the suite red. template_snapshots/ is vendored and refreshed
    # wholesale from upstream, so a lint command inside one belongs to that
    # project rather than to this repo's gate.
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        rel
        for rel in listed
        if rel
        and Path(rel).suffix in {".md", ".yml", ".yaml"}
        and not rel.startswith("template_snapshots/")
    ]


def _calls(rel: str) -> list[list[str]]:
    text = (ROOT / rel).read_text()
    # ci.yml and ai-fix.yml both wrap the call over several lines with a
    # trailing backslash. Join those first so the file list is one string.
    joined = re.sub(r"\\\n\s*", " ", text)
    found = []
    for match in _INVOCATION.finditer(joined):
        tokens = match.group(1).split()
        arguments = [token for token in tokens if not token.startswith("-")]
        # Every argument the real command takes is a path with a directory in
        # it. Requiring that of all of them is what separates a call from a
        # sentence about one. A future copy passing a bare filename would be
        # skipped here rather than compared -- worth knowing if this ever
        # stops catching a drift it should have.
        if arguments and all("/" in argument for argument in arguments):
            found.append(tokens)
    return found


class ShellcheckCommandTests(unittest.TestCase):
    def _normalise(self, rel: str, tokens: list[str]) -> tuple[frozenset[str], frozenset[str]]:
        flags = frozenset(token for token in tokens if token.startswith("-"))
        paths: set[str] = set()
        for token in tokens:
            if token.startswith("-"):
                continue
            if "*" not in token:
                paths.add(token)
                continue
            # The prose copies write `tests/e2e/*.sh` where ci.yml spells the
            # three files out. Expand so the two shapes compare equal -- and
            # so a glob that has stopped matching anything is a failure here
            # rather than a lint step that silently checks nothing.
            expanded = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob(token))
            self.assertTrue(expanded, f"{rel}: {token} matches no file")
            paths.update(expanded)
        return flags, frozenset(paths)

    def test_every_copy_of_the_shellcheck_command_matches_the_gate(self) -> None:
        gate_calls = _calls(GATE)
        self.assertEqual(
            len(gate_calls),
            1,
            f"expected exactly one shellcheck call in {GATE}, found {len(gate_calls)}",
        )
        gate_flags, gate_paths = self._normalise(GATE, gate_calls[0])

        # -x is load-bearing, not decoration: the two e2e suites carry a
        # `# shellcheck source=` directive that the linter follows only when
        # allowed to read external sources.
        self.assertIn("-x", gate_flags, f"{GATE}'s gate no longer passes -x")
        for path in sorted(gate_paths):
            self.assertTrue(
                (ROOT / path).is_file(),
                f"{GATE} lints {path}, which does not exist",
            )

        copies = {}
        for rel in _tracked_prose_and_workflows():
            if rel == GATE:
                continue
            calls = _calls(rel)
            if calls:
                copies[rel] = calls
        # If the scan stops recognising the copies it will pass while checking
        # nothing, which is the failure mode this whole file exists to catch.
        self.assertGreaterEqual(
            len(copies),
            4,
            f"found only {sorted(copies)}; the scan has stopped seeing the copies",
        )

        for rel, calls in sorted(copies.items()):
            for tokens in calls:
                flags, paths = self._normalise(rel, tokens)
                self.assertEqual(
                    sorted(paths),
                    sorted(gate_paths),
                    f"{rel}'s shellcheck command lints a different set of files "
                    f"than {GATE}'s gate does",
                )
                self.assertEqual(
                    sorted(flags),
                    sorted(gate_flags),
                    f"{rel}'s shellcheck command passes different flags than "
                    f"{GATE}'s gate does",
                )


if __name__ == "__main__":
    unittest.main()

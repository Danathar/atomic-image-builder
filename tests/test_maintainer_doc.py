"""Keep maintainer_docs/MAINTAINER.md's release step pointing at the right place.

Script: tests/test_maintainer_doc.py
What: Reads the release procedure in maintainer_docs/MAINTAINER.md and checks
      that its "bump the version" step locates `VERSION` by name -- the fenced
      `VERSION = "..."` block -- and never by a line number, and that no
      Markdown in the repo carries a `(line N)` pointer into a source file.
Why: The step used to say `atomic_image_builder.py` (line 37) while `VERSION`
     sat on line 38 (#340). It was the only line-number pointer in the repo's
     Markdown, nothing read it, and it was false: any edit above the constant
     rots such a pointer again, and no test would notice. The pointer was
     dropped rather than corrected -- the fenced block on the next line already
     identifies the constant with nothing to go stale -- and this file is what
     keeps it dropped.
Goal: A line number written back into a doc fails here, in front of whoever
      wrote it, rather than misdirecting the next person cutting a release.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAINTAINER_PATH = ROOT / "maintainer_docs/MAINTAINER.md"

# `(line 37)` and `line 37 of` -- the two spellings a prose pointer at a source
# line takes. Word-bounded so a `VERSION = "0.9.5"` string or `python3` cannot
# match, and applied outside fenced blocks so quoted tool output that happens to
# say "line 12" is not read as the doc making a claim.
LINE_POINTER = re.compile(r"\(line \d+\)|\bline \d+ of\b")


def _outside_fences(text: str) -> str:
    """The document with its fenced blocks blanked, line numbers preserved.

    Fenced lines become empty rather than disappearing, so a `path:lineno` in a
    failure message below points at the real line in the file.
    """
    kept = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            kept.append("")
            continue
        kept.append("" if in_fence else line)
    return "\n".join(kept)


def _tracked_markdown() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.md", "**/*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ROOT / name for name in listed.split("\0") if name]


class MaintainerDocReleaseStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = MAINTAINER_PATH.read_text()

    def bump_step(self) -> str:
        """The 'Bump the version' step, from its bold lead to the next step."""
        match = re.search(
            r"\*\*\d+\. Bump the version\.\*\*.*?(?=\n\*\*\d+\. )",
            self.doc,
            re.S,
        )
        self.assertIsNotNone(
            match, "MAINTAINER.md no longer has a numbered 'Bump the version.' step"
        )
        return match.group(0)

    def test_the_bump_step_names_the_file_and_shows_the_constant(self) -> None:
        step = self.bump_step()
        self.assertIn(
            "`atomic_image_builder.py`",
            step,
            "the bump step no longer says which file carries VERSION",
        )
        self.assertRegex(
            step,
            r"```python\nVERSION = \"[^\"]+\"\n```",
            "the bump step no longer shows the `VERSION = ...` line it is about; "
            "that fenced block is what locates the constant now that the line "
            "number is gone",
        )

    def test_the_bump_step_does_not_locate_version_by_line_number(self) -> None:
        step = _outside_fences(self.bump_step())
        self.assertIsNone(
            LINE_POINTER.search(step),
            "the bump step points at a line number again; it was wrong by one "
            "the last time (#340) and every edit above VERSION would make it "
            "wrong again -- the fenced block below it locates the constant",
        )

    def test_no_markdown_points_into_a_source_file_by_line_number(self) -> None:
        offenders = []
        for path in _tracked_markdown():
            text = _outside_fences(path.read_text())
            for lineno, line in enumerate(text.splitlines(), start=1):
                if LINE_POINTER.search(line):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}"
                    )
        self.assertEqual(
            offenders,
            [],
            "a doc points at a source line by number; nothing keeps that "
            "number true, so name the symbol or show the line instead",
        )


if __name__ == "__main__":
    unittest.main()

"""Pin the shared workflow list to both suffixes GitHub loads.

Script: tests/test_workflow_paths.py
What: Calls `_workflow_steps.workflow_paths()` on a temporary directory that
      holds a `.yml`, a `.yaml` and a file that is not a workflow.
Why: Every scan that is meant to cover all workflows takes its list from that
     helper. The repository has no `.yaml` workflow today, so dropping `.yaml`
     from the helper leaves the whole suite green while every scan quietly
     stops seeing such a file.
Goal: Removing either suffix from the helper, or letting other files into its
      list, fails here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _workflow_steps import workflow_paths


class WorkflowPathsTests(unittest.TestCase):
    def test_both_suffixes_are_listed_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ("b.yaml", "a.yml", "notes.md"):
                (directory / name).write_text("on: push\n", encoding="utf-8")

            names = [path.name for path in workflow_paths(directory)]

        self.assertEqual(names, ["a.yml", "b.yaml"])


if __name__ == "__main__":
    unittest.main()

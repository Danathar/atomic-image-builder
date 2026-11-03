"""Join MAINTAINER.md's snapshot drift section to the audit that it describes.

Script: tests/test_maintainer_snapshot_drift_doc.py
What: Reads *Advisory → failure: the bundled template snapshot trails
      upstream* in maintainer_docs/MAINTAINER.md and checks each fact it states
      about the weekly audit against the code and workflow that produce it:
      the sample advisory is the message describe_snapshot_drift() prints for
      the same SHAs and count; the threshold named in bold is
      SNAPSHOT_DRIFT_FAILURE_COMMITS with the same value; every row of the
      never-blocks table is a message the audit really prints, non-blocking
      at any distance, and the table has as many rows as the prose counts; the
      tracking issue title is snapshot_drift_issue.ISSUE_TITLE; and the job
      that runs snapshot_drift_issue.py holds `issues: write`.
Why: This is the section a maintainer reads when the weekly audit goes red or
     opens its tracking issue, and it quotes the audit's own output to help
     them read it. tests/test_maintenance_audit.py pins the messages and the
     threshold, but nothing read this page, so renaming or moving the
     constant, rewording a message or dropping the permission left a page
     telling the maintainer to search for text the audit no longer prints.
Goal: Changing what the audit says or where it fails fails here until the
      page says the same thing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

import _block_yaml
import maintenance_audit
import snapshot_drift_issue
from maintenance_audit import TemplateSource, describe_snapshot_drift

ROOT = Path(__file__).resolve().parent.parent
MAINTAINER_PATH = ROOT / "maintainer_docs/MAINTAINER.md"
WORKFLOW_PATH = ROOT / ".github/workflows/maintenance-audit.yml"
HEADING = "### Advisory → failure: the bundled template snapshot trails upstream"
UPSTREAM = "https://github.com/ublue-os/image-template.git"

# Far enough past the threshold that "whatever the distance" is what a
# non-blocking result proves, not that the count happened to sit below it.
FAR = maintenance_audit.SNAPSHOT_DRIFT_FAILURE_COMMITS * 10

# The never-blocks table's Message cell -> the comparison that produces it.
# A comparison is what query_github_comparison() returns, or the exception it
# raises. `N` in a quoted cell stands for the count, so it is replaced with
# the count the case uses before looking for the cell in the message.
NEVER_BLOCKS = {
    "`N commit(s) OLDER than the snapshot`": ("behind", 0, FAR),
    "`on a diverged history`": ("diverged", FAR, 3),
    "no count at all": RuntimeError("HTTP 403: rate limit exceeded"),
}

NUMBER_WORDS = {"Two": 2, "Three": 3, "Four": 4, "Five": 5}


def _section(doc: str) -> str:
    start = doc.index(HEADING + "\n")
    end = doc.find("\n### ", start + len(HEADING))
    return doc[start:] if end == -1 else doc[start:end]


def _drift(comparison: object, *, revision: str = "a" * 40, head: str = "b" * 40):
    source = TemplateSource(repo=UPSTREAM, revision=revision)
    if isinstance(comparison, Exception):
        mocked = patch(
            "maintenance_audit.query_github_comparison", side_effect=comparison
        )
    else:
        mocked = patch(
            "maintenance_audit.query_github_comparison", return_value=comparison
        )
    with mocked:
        return describe_snapshot_drift(source, head)


class SnapshotDriftSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(MAINTAINER_PATH.read_text())

    def test_sample_advisory_is_what_the_audit_prints(self) -> None:
        fence = re.search(r"\n```\n(.*?)\n```\n", self.section, re.S)
        self.assertIsNotNone(fence, "the section no longer shows a sample advisory")
        sample = " ".join(fence.group(1).split())
        counts = re.search(
            r"pinned (\w{12}), upstream (\w{12}), (\d+) commit\(s\) newer", sample
        )
        self.assertIsNotNone(
            counts, f"cannot read the SHAs and count out of the sample: {sample}"
        )
        pinned, upstream, ahead = counts.group(1), counts.group(2), int(counts.group(3))
        drift = _drift(
            ("ahead", ahead, 0),
            revision=pinned.ljust(40, "0"),
            head=upstream.ljust(40, "0"),
        )
        self.assertEqual(
            drift.message,
            sample,
            "the sample advisory is not what the audit prints for the same SHAs and count",
        )
        self.assertFalse(
            drift.blocking,
            "the section shows the sample as an advisory, but the audit fails on it",
        )

    def test_failure_threshold_is_the_constant_and_its_value(self) -> None:
        stated = re.search(r"\*\*Past `(\w+)` \((\d+)\)", self.section)
        self.assertIsNotNone(
            stated,
            "the section no longer names the threshold where drift fails the job",
        )
        name, value = stated.group(1), int(stated.group(2))
        self.assertTrue(
            hasattr(maintenance_audit, name),
            f"the section tells maintainers to change {name} in maintenance_audit.py, which has no such constant",
        )
        self.assertEqual(
            getattr(maintenance_audit, name), value, f"{name} is not {value}"
        )
        self.assertTrue(
            _drift(("ahead", value, 0)).blocking,
            f"{value} commits behind does not fail the job",
        )
        self.assertFalse(
            _drift(("ahead", value - 1, 0)).blocking,
            f"drift under {value} commits fails the job",
        )

    def _never_blocks_rows(self) -> list[str]:
        table = re.search(
            r"\| Message +\| Why it stays advisory +\|\n\|[-| ]+\|\n((?:\|.*\|\n?)+)",
            self.section,
        )
        self.assertIsNotNone(table, "the section no longer has the never-blocks table")
        return [row.split("|")[1].strip() for row in table.group(1).splitlines()]

    def test_never_blocks_table_counts_match_the_prose(self) -> None:
        said = re.search(r"\b(\w+) cases never block\b", self.section)
        self.assertIsNotNone(
            said, "the section no longer says how many cases never block"
        )
        self.assertIn(
            said.group(1), NUMBER_WORDS, f"cannot read {said.group(1)!r} as a count"
        )
        self.assertEqual(len(self._never_blocks_rows()), NUMBER_WORDS[said.group(1)])

    def test_never_blocks_table_rows_are_audit_messages_that_never_block(self) -> None:
        rows = self._never_blocks_rows()
        unknown = [cell for cell in rows if cell not in NEVER_BLOCKS]
        self.assertEqual(
            unknown,
            [],
            "the table lists a case this test cannot map to a comparison; add it to NEVER_BLOCKS",
        )
        self.assertEqual(
            sorted(rows),
            sorted(NEVER_BLOCKS),
            "a case the audit never blocks on left the table",
        )
        for cell in rows:
            comparison = NEVER_BLOCKS[cell]
            with self.subTest(cell=cell):
                drift = _drift(comparison)
                self.assertFalse(
                    drift.blocking,
                    f"the audit fails the job on {cell!r} {FAR} commits out",
                )
                if cell.startswith("`"):
                    status, ahead, behind = comparison
                    count = behind if status == "behind" else ahead
                    quoted = cell.strip("`").replace("N ", f"{count} ", 1)
                    self.assertIn(
                        quoted, drift.message, f"the audit no longer prints {cell}"
                    )
                else:
                    self.assertNotRegex(
                        drift.message,
                        r"\d+ commit\(s\)",
                        "an uncounted comparison prints a count",
                    )

    def test_tracking_issue_title_is_the_one_the_script_files(self) -> None:
        titled = re.search(r"titled \*([^*]+)\*", self.section)
        self.assertIsNotNone(
            titled, "the section no longer names the tracking issue's title"
        )
        self.assertEqual(
            titled.group(1).replace("\n", " "), snapshot_drift_issue.ISSUE_TITLE
        )

    def test_the_job_running_the_issue_script_can_write_issues(self) -> None:
        self.assertIn(
            "`snapshot_drift_issue.py` does that, and the audit job carries `issues: write`",
            self.section,
        )
        jobs = _block_yaml.parse(WORKFLOW_PATH.read_text())["jobs"]
        running = [
            name
            for name, job in jobs.items()
            if any(
                "snapshot_drift_issue.py" in str(step.get("run", ""))
                for step in job["steps"]
            )
        ]
        self.assertEqual(
            running,
            ["audit"],
            "the section says the audit job runs snapshot_drift_issue.py",
        )
        self.assertEqual(jobs["audit"].get("permissions", {}).get("issues"), "write")


if __name__ == "__main__":
    unittest.main()

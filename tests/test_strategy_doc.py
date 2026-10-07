"""Join docs/strategy.md to the roadmap it reads and the data its commands read.

Script: tests/test_strategy_doc.py
What: Reads docs/strategy.md and checks the claims a reader acts on without
      noticing they are wrong: that it has one entry per column of ROADMAP.md's
      beta-exit table, that its links land, that every list command names the
      repository and a limit high enough to see everything, that each list
      asks for the state and date window its entry's prose describes, that the
      merged-work grouping turns branch names into prefix counts, that the bug
      label it filters on is the one the bug form applies, and that the
      coverage lookup can match the history the CI job writes.
Doing: Reuses tests/test_metrics_doc.py's section, link and fence parsers and
       tests/test_roadmap_doc.py's column list, so the three documents are read
       the same way.
Why: The page is commands rather than numbers, and a stale command does not
     fail. A `gh ... list` with gh's default limit of 30 silently undercounts
     (docs/metrics.md records this happening), a renamed label returns no
     issues, and a coverage history that stopped writing full SHAs would make
     the tag lookup print nothing -- which the page tells the reader to read
     as "no push ended on that commit". gh lists only open items unless told
     otherwise, so a dropped `--state` turns "every bug since the release"
     into "the ones still open", and a `created:` where `merged:` belongs
     drops work that was opened before the release and merged after it.
Goal: Make a new beta-exit column, a moved heading, a renamed label, a dropped
      `--limit` or `--state`, a shifted date window or a change to the trend
      file's SHA fail here.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coverage_badge import trend_row  # noqa: E402
from test_metrics_doc import (  # noqa: E402
    PUBLISH_BRANCH,
    PUBLISH_JOB,
    PUBLISH_JOB_BLOCK,
    SLUG,
    TRACKED,
    TREND_FILE,
    anchor_slug,
    fenced_blocks,
    links,
    split_sections,
)
from test_roadmap_doc import PROGRESS_COLUMNS  # noqa: E402

DOC_PATH = ROOT / "docs/strategy.md"
DOC = DOC_PATH.read_text(encoding="utf-8")
SECTIONS = split_sections(DOC)
THRESHOLDS = json.loads((ROOT / ".coverage-thresholds.json").read_text(encoding="utf-8"))
BUG_FORM = ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml"

GOAL_SECTION = "The goal"
ROW_SECTION = "Filling in a beta-exit row"
WORK_SECTION = "Is the work going there?"
NO_JOB_SECTION = "Why there is no report job"
NAMED_SECTIONS = (GOAL_SECTION, ROW_SECTION, WORK_SECTION, NO_JOB_SECTION)

# gh returns the newest N and says nothing about the rest. A count read from a
# list has to ask for more than there will ever be; release list is exempt
# because it only wants the newest few.
LIST_LIMIT = 1000

# Labels the page filters on that a file in this repository applies. Each maps
# to the file that applies it. `needs-decision` is applied by the Hive, outside
# this repository, so there is nothing here to join it to.
LABEL_SOURCES = {"bug": BUG_FORM}
EXTERNAL_LABELS = {"needs-decision"}

# Each issue and pr list, keyed by the bold name its entry opens with, as
# (kind, --state, --search, --json). gh's own default state is `open`, so every
# entry that counts closed or merged items has to say so. The search windows
# are inclusive on <date>: a bug filed on release day counts against that
# release, and one closed on release day was not open at its end.
LIST_QUERIES = {
    # "every bug issue opened since the release", fixed or not.
    "Containerfile regressions": ("issue", "all", "created:>=<date>", "number,title,state"),
    # "still open at the end of the release day": filed by then, not closed by
    # then. Its state today does not matter, so it has to be `all`.
    "Open five-stage correctness issues": (
        "issue",
        "all",
        "created:<=<date> -closed:<=<date>",
        "number,title",
    ),
    # "merged since the newest release", whenever the PR was opened.
    "What has merged since the newest release": ("pr", "merged", "merged:>=<date>", "headRefName"),
    # Waiting now, so open now; no date window.
    "What is waiting on the maintainer": ("issue", "open", None, None),
}


def commands() -> list[list[str]]:
    """Every `gh` command in the page's fences, as argv, continuations joined."""
    found = []
    for block in fenced_blocks(DOC):
        for line in block.replace("\\\n", " ").splitlines():
            if line.startswith("gh "):
                found.append(shlex.split(line))
    return found


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def led_lists() -> list[tuple[str | None, list[str]]]:
    """Every `gh issue list` and `gh pr list`, with the bold name its entry opens with."""
    found = []
    lead = None
    fence: list[str] | None = None
    for line in DOC.splitlines():
        if line.startswith("```"):
            if fence is None:
                fence = []
                continue
            for command in "\n".join(fence).replace("\\\n", " ").splitlines():
                if command.startswith("gh "):
                    argv = shlex.split(command)
                    if argv[1] in {"issue", "pr"} and argv[2] == "list":
                        found.append((lead, argv))
            fence = None
        elif fence is not None:
            fence.append(line)
        elif lead_match := re.match(r"^\*\*(.+?)\*\*", line):
            lead = lead_match.group(1)
    return found


class Outline(unittest.TestCase):
    def test_every_section_is_one_this_module_reads(self) -> None:
        headings = re.findall(r"^## (.*)$", DOC, re.M)
        self.assertEqual(headings, list(NAMED_SECTIONS))

    def test_every_relative_link_lands_on_a_committed_file_and_heading(self) -> None:
        checked = 0
        for label, target in links(DOC):
            if target.startswith("http"):
                continue
            checked += 1
            relative, _, anchor = target.partition("#")
            resolved = (DOC_PATH.parent / relative).resolve()
            with self.subTest(link=label, target=target):
                self.assertIn(str(resolved.relative_to(ROOT)), TRACKED)
                if anchor:
                    titles = {
                        anchor_slug(title)
                        for title in re.findall(r"^#+\s+(.*)$", resolved.read_text(), re.M)
                    }
                    self.assertIn(anchor, titles, f"{relative} has no heading #{anchor}")
        self.assertGreaterEqual(checked, 8, "the link scan found almost nothing to check")

    def test_the_roadmap_points_here(self) -> None:
        roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertIn("docs/strategy.md", {target for _, target in links(roadmap)})


class BetaExitRow(unittest.TestCase):
    def test_each_roadmap_column_has_an_entry_in_order(self) -> None:
        # The entries lead with the column names in bold, two to a lead where
        # one command answers both. A column added to the table has to arrive
        # here too, or the page stops covering the row a release needs.
        leads = re.findall(r"^\*\*(.+?)\*\*(?: and \*\*(.+?)\*\*)?:", SECTIONS[ROW_SECTION], re.M)
        named = [name for pair in leads for name in pair if name]
        self.assertEqual(named, list(PROGRESS_COLUMNS))

    def test_the_gate_lookup_reads_the_gated_threshold(self) -> None:
        lookups = re.findall(r"jq -er '\.([\w.]+)' \.coverage-thresholds\.json", SECTIONS[ROW_SECTION])
        self.assertEqual(lookups, ["gated.unit"])
        value = THRESHOLDS
        for key in lookups[0].split("."):
            value = value[key]
        self.assertIsInstance(value, int)

    def test_the_tag_lookup_can_match_a_trend_row(self) -> None:
        # The page greps the history for `git rev-list -n 1 <tag>`, a full SHA,
        # and reads no output as "no push ended there". That only holds while
        # the job passes the full SHA and the writer keeps it whole.
        self.assertIn(f"origin/{PUBLISH_BRANCH}:{TREND_FILE}", SECTIONS[ROW_SECTION])
        self.assertIn("git rev-list -n 1 <tag>", SECTIONS[ROW_SECTION])
        self.assertIn('--sha "$GITHUB_SHA"', PUBLISH_JOB_BLOCK, f"ci.yml's {PUBLISH_JOB} job")
        full = "0123456789abcdef0123456789abcdef01234567"
        self.assertIn(f",{full},", trend_row(date="2026-01-02", sha=full, percent=99))


class Commands(unittest.TestCase):
    def test_every_command_names_this_repository(self) -> None:
        found = commands()
        self.assertGreaterEqual(len(found), 5, "the command scan found almost nothing")
        for argv in found:
            with self.subTest(command=" ".join(argv[:3])):
                self.assertEqual(option(argv, "--repo"), SLUG)

    def test_every_issue_and_pr_list_asks_for_everything(self) -> None:
        lists = [argv for argv in commands() if argv[1] in {"issue", "pr"} and argv[2] == "list"]
        self.assertGreaterEqual(len(lists), 4)
        for argv in lists:
            with self.subTest(command=" ".join(argv)):
                self.assertEqual(option(argv, "--limit"), str(LIST_LIMIT))

    def test_each_list_asks_for_the_state_and_window_its_entry_describes(self) -> None:
        found = {
            lead: (argv[1], option(argv, "--state"), option(argv, "--search"), option(argv, "--json"))
            for lead, argv in led_lists()
        }
        self.assertEqual(len(found), len(led_lists()), "two lists sit under one entry")
        self.assertEqual(found, LIST_QUERIES)

    def test_the_merged_work_grouping_counts_branch_prefixes(self) -> None:
        # Feed the --jq filter the shape --json asks gh for and check it does
        # what the prose says: one count per branch prefix.
        if shutil.which("jq") is None:
            self.skipTest("jq is not installed")
        (argv,) = [argv for lead, argv in led_lists() if argv[1] == "pr"]
        fields = option(argv, "--json").split(",")
        sample = [
            {"headRefName": "quality/test-one"},
            {"headRefName": "fix/two"},
            {"headRefName": "quality/three"},
            {"headRefName": "renovate/four"},
        ]
        result = subprocess.run(
            ["jq", "-c", option(argv, "--jq")],
            input=json.dumps([{key: row[key] for key in fields if key in row} for row in sample]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"prefix": "fix", "count": 1},
                {"prefix": "quality", "count": 2},
                {"prefix": "renovate", "count": 1},
            ],
        )

    def test_every_label_is_one_something_applies(self) -> None:
        labels = {option(argv, "--label") for argv in commands()} - {None}
        self.assertEqual(labels, set(LABEL_SOURCES) | EXTERNAL_LABELS)
        for label, source in LABEL_SOURCES.items():
            with self.subTest(label=label):
                applied = re.search(r"^labels:\s*\[(.*)\]\s*$", source.read_text(), re.M)
                self.assertIsNotNone(applied, f"{source.name} no longer sets labels")
                self.assertIn(f'"{label}"', applied.group(1))


if __name__ == "__main__":
    unittest.main()

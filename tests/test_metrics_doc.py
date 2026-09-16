"""Join docs/metrics.md to the machinery that produces the numbers it explains.

Script: tests/test_metrics_doc.py
What: Reads docs/metrics.md and checks every pointer it hand-copies -- job id,
      branch, artifact name, filename, CSV column order, cron day, section
      title, repository slug -- against the workflow, script or document that
      actually decides it.
Doing: Splits the doc into heading-scoped sections, pulls its fenced blocks,
       inline code spans and links, and resolves each claim: the badge and
       trend claims against `ci.yml`'s `publish-coverage` job and
       `coverage_badge.trend_row`, the tier list against
       `.coverage-thresholds.json`, the artifact names and retention against
       every `actions/upload-artifact` step that uploads a coverage tier, the
       schedule against `maintenance-audit.yml`'s cron, and every link against
       the committed tree.
Why: docs/metrics.md was opened by no test at any tier. `.coveragerc` measures
     six Python modules, `.coveragerc.e2e` has the same Python-only shape and
     ruff does not read Markdown, so no percentage moves when a pointer in
     here stops being true. This is the only document that says how to
     reproduce the project's own numbers, and a reader who follows a stale
     command gets no error -- they get a different number, or someone else's.
Goal: Make a renamed job, a moved branch, a dropped artifact, a re-ordered CSV
      column or a re-scheduled audit fail here, instead of quietly turning the
      reproduction instructions into fiction.

Scoping is by heading. A claim that slid under a neighbouring heading is a
different document: the download command under *The other four coverage tiers*
has to name the four advisory artifacts, and a whole-file search for those
names cannot see it move.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coverage_badge import trend_row  # noqa: E402

DOC_PATH = ROOT / "docs/metrics.md"
DOC = DOC_PATH.read_text()

CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
AUDIT_WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"
THRESHOLDS_PATH = ROOT / ".coverage-thresholds.json"
CONTRIBUTING_PATH = ROOT / "CONTRIBUTING.md"
MAINTAINER_PATH = ROOT / "maintainer_docs/MAINTAINER.md"
README_PATH = ROOT / "README.md"

# Sections this module asserts against, named rather than discovered: the
# titles are what scopes every other assertion here, so a silently renamed
# section has to fail rather than make the assertions under it vacuous.
HISTORY_SECTION = "Unit coverage, and its history"
TIERS_SECTION = "The other four coverage tiers"
THROUGHPUT_SECTION = "Pull request throughput"
FINDINGS_SECTION = "Review findings per pull request"
AUDIT_SECTION = "Weekly audit outcomes"
CAVEATS_SECTION = "What these numbers do not mean"

NAMED_SECTIONS = (
    HISTORY_SECTION,
    TIERS_SECTION,
    THROUGHPUT_SECTION,
    FINDINGS_SECTION,
    AUDIT_SECTION,
    CAVEATS_SECTION,
)

# The job in ci.yml that writes both published artifacts, and the branch it
# writes them to. Both are hand-copied into the doc.
PUBLISH_JOB = "publish-coverage"
PUBLISH_BRANCH = "coverage-data"
TREND_FILE = "coverage-trend.csv"
BADGE_FILE = "coverage-unit.json"

# Files the doc names that are published to PUBLISH_BRANCH rather than
# committed here. A path span that names one of these is resolved against the
# publish job's arguments instead of against `git ls-files`; resolving it the
# usual way would fail for the wrong reason and hide a real rename.
PUBLISHED_FILES = {TREND_FILE, BADGE_FILE}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}

# cron day-of-week is 0-6 with Sunday at both 0 and 7.
WEEKDAY_NUMBERS = {
    "Sunday": 0,
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
}

# How each advisory key in .coverage-thresholds.json is spelled in prose. The
# doc writes "End-to-end", the file writes "e2e"; without a table the tier
# join can only be a substring search, which "maintenance-audit" would pass by
# accident. test_the_tier_wording_table_covers_every_advisory_tier asserts the
# table is complete, so a new advisory tier cannot arrive unspelled.
TIER_WORDS = {
    "e2e": "End-to-end",
    "shell": "shell-entrypoint",
    "maintenance-audit": "maintenance-audit",
    "homebrew-release": "homebrew-release",
}


def split_sections(text: str) -> dict[str, str]:
    """Map each heading's exact title to the body beneath it.

    The body stops at the next heading of any level, so a claim that slid
    under a neighbouring heading is no longer in the section that carries it.
    """
    sections: dict[str, str] = {}
    title: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^(#+)\s+(.*)$", line)
        if heading:
            if title is not None:
                sections[title] = "\n".join(body)
            title = heading.group(2).strip()
            body = []
            continue
        if title is not None:
            body.append(line)
    if title is not None:
        sections[title] = "\n".join(body)
    return sections


SECTIONS = split_sections(DOC)


def flatten(text: str) -> str:
    """Collapse the hard wrapping so a sentence can be matched as a sentence."""
    return re.sub(r"\s+", " ", text).strip()


def fenced_blocks(body: str) -> list[str]:
    """The contents of every fenced code block in one section."""
    return [block.strip("\n") for block in re.findall(r"^```[^\n]*\n(.*?)^```", body, re.S | re.M)]


def inline_spans(text: str) -> list[str]:
    """Every inline code span, fenced blocks removed first."""
    without_blocks = re.sub(r"^```[^\n]*\n.*?^```", "", text, flags=re.S | re.M)
    return re.findall(r"`([^`\n]+)`", without_blocks)


def links(text: str) -> list[tuple[str, str]]:
    """Every inline Markdown link as (label, target)."""
    return re.findall(r"\[([^\]\n]+)\]\(([^)\s]+)\)", text)


def anchor_slug(title: str) -> str:
    """GitHub's heading anchor for a title."""
    slug = title.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


def tracked_files() -> set[str]:
    """Every file git knows about, tracked or newly added and not ignored."""
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in proc.stdout.splitlines() if line}


TRACKED = tracked_files()


def job_block(workflow: Path, job_id: str) -> str:
    """The lines of one top-level job, by id.

    Indentation-bound rather than a search for the id: a job whose body was
    reindented or whose steps moved under a different job is a different
    workflow, and a substring search cannot see either.
    """
    lines = workflow.read_text().splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^  {re.escape(job_id)}:\s*$", line):
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line)
    return "\n".join(body)


def coverage_uploads() -> list[dict[str, str]]:
    """Every `actions/upload-artifact` step that uploads a coverage artifact.

    Discovered from the workflows rather than listed here: a hand-written list
    of step names would keep passing after a tier stopped being uploaded.
    """
    found: list[dict[str, str]] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        lines = workflow.read_text().splitlines()
        for index, line in enumerate(lines):
            uses = re.match(r"^(\s*)uses: actions/upload-artifact@", line)
            if not uses:
                continue
            indent = len(uses.group(1))
            step: dict[str, str] = {"workflow": workflow.name}
            for following in lines[index + 1 :]:
                if following.strip() and (
                    len(following) - len(following.lstrip()) < indent
                    or following.lstrip().startswith("- ")
                ):
                    break
                key = re.match(r"^\s*(name|retention-days):\s*(\S+)\s*$", following)
                if key:
                    step.setdefault(key.group(1), key.group(2))
            if step.get("name", "").startswith("coverage-"):
                found.append(step)
    return found


UPLOADS = coverage_uploads()
THRESHOLDS = json.loads(THRESHOLDS_PATH.read_text())
ADVISORY_TIERS = THRESHOLDS["advisory"]
PUBLISH_JOB_BLOCK = job_block(CI_WORKFLOW, PUBLISH_JOB)


def repository_slug() -> str:
    """The owner/repo this project is, taken from README's own workflow badges."""
    slugs = set(re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/actions/", README_PATH.read_text()))
    if len(slugs) != 1:
        raise AssertionError(f"README names {sorted(slugs)} as the repository, expected exactly one")
    return slugs.pop()


SLUG = repository_slug()


class DocumentStructure(unittest.TestCase):
    """The scoping this module depends on, asserted before anything uses it."""

    def test_the_section_scan_finds_every_named_section(self) -> None:
        for title in NAMED_SECTIONS:
            with self.subTest(section=title):
                self.assertIn(title, SECTIONS, f"docs/metrics.md no longer has a '{title}' section")
                self.assertTrue(SECTIONS[title].strip(), f"'{title}' is empty")

    def test_every_section_in_the_doc_is_read_by_this_module(self) -> None:
        # A new section can otherwise arrive unread, which is exactly how this
        # document got into the state that made this module necessary.
        headings = [match.group(1).strip() for match in re.finditer(r"^## (?!#)(.*)$", DOC, re.M)]
        self.assertEqual(
            sorted(headings),
            sorted(NAMED_SECTIONS),
            "docs/metrics.md has a section this module neither reads nor knows about",
        )

    def test_the_link_scan_finds_the_links_it_checks(self) -> None:
        self.assertGreaterEqual(len(links(DOC)), 5, "the link scan found almost nothing to check")

    def test_every_relative_link_target_is_committed(self) -> None:
        checked = 0
        for label, target in links(DOC):
            if target.startswith("http") or target.startswith("#"):
                continue
            checked += 1
            relative = target.split("#", 1)[0]
            with self.subTest(link=label, target=target):
                resolved = (DOC_PATH.parent / relative).resolve()
                self.assertEqual(
                    str(resolved.relative_to(ROOT)) in TRACKED,
                    True,
                    f"docs/metrics.md links to {target}, which is not a committed file",
                )
        self.assertTrue(checked, "docs/metrics.md no longer links to anything in this repository")

    def test_every_anchor_a_link_names_is_a_heading_in_the_file_it_points_at(self) -> None:
        checked = 0
        for label, target in links(DOC):
            if target.startswith("http") or "#" not in target:
                continue
            relative, anchor = target.split("#", 1)
            if not relative.endswith(".md"):
                continue
            checked += 1
            resolved = (DOC_PATH.parent / relative).resolve()
            titles = {
                anchor_slug(match.group(1))
                for match in re.finditer(r"^#+\s+(.*)$", resolved.read_text(), re.M)
            }
            with self.subTest(link=label, target=target):
                self.assertIn(
                    anchor,
                    titles,
                    f"{relative} has no heading whose anchor is #{anchor}",
                )
        self.assertTrue(checked, "no anchored link left to check")

    def test_every_path_the_doc_names_resolves(self) -> None:
        # Inline spans that look like a path: no whitespace, a file extension.
        # Three spellings resolve -- a file published to the coverage-data
        # branch, a bare workflow filename, and a path from the repository
        # root -- and anything else is a path this doc invented.
        candidates = {
            span
            for span in inline_spans(DOC)
            if re.fullmatch(r"[\w./-]+\.(py|json|csv|md|yml|sh)", span)
        }
        self.assertGreaterEqual(len(candidates), 4, "the path scan found almost nothing to check")
        for span in sorted(candidates):
            with self.subTest(path=span):
                if span in PUBLISHED_FILES:
                    self.assertIn(
                        span,
                        PUBLISH_JOB_BLOCK,
                        f"{span} is documented as published to {PUBLISH_BRANCH}, "
                        f"but ci.yml's {PUBLISH_JOB} job does not write it",
                    )
                    continue
                if span.endswith(".yml") and "/" not in span:
                    self.assertIn(
                        f".github/workflows/{span}",
                        TRACKED,
                        f"docs/metrics.md names the workflow {span}, which is not committed",
                    )
                    continue
                self.assertIn(
                    span, TRACKED, f"docs/metrics.md names {span}, which is not a committed file"
                )

    def test_the_docs_that_point_at_this_file_still_point_at_it(self) -> None:
        # A rename that updated the doc but not its two referrers leaves both
        # pointing at nothing, and neither of them is executable either.
        quality = ROOT / "docs/quality.md"
        targets = {target for _, target in links(quality.read_text())}
        self.assertIn(
            DOC_PATH.name,
            targets,
            "docs/quality.md no longer links to metrics.md",
        )
        tuning = (ROOT / ".github/auto-qa-tuning.json").read_text()
        self.assertIn(
            str(DOC_PATH.relative_to(ROOT)),
            tuning,
            ".github/auto-qa-tuning.json names a metrics doc that is not this one",
        )


class UnitCoverageHistory(unittest.TestCase):
    """The badge and trend claims, against the job that publishes both."""

    def setUp(self) -> None:
        self.section = SECTIONS[HISTORY_SECTION]
        self.flat = flatten(self.section)

    def test_the_publishing_job_the_doc_names_is_a_job_in_the_workflow(self) -> None:
        self.assertIn(PUBLISH_JOB, self.flat, "the section no longer names the publishing job")
        self.assertTrue(
            PUBLISH_JOB_BLOCK.strip(),
            f"ci.yml has no top-level `{PUBLISH_JOB}:` job for the doc to point at",
        )

    def test_the_publishing_job_runs_the_script_the_doc_names(self) -> None:
        self.assertIn("coverage_badge.py", self.flat)
        self.assertIn(
            "python3 coverage_badge.py",
            PUBLISH_JOB_BLOCK,
            f"ci.yml's {PUBLISH_JOB} job no longer runs coverage_badge.py",
        )

    def test_the_trend_gets_one_row_per_push_to_main(self) -> None:
        self.assertIn("one row per push to `main`", self.section)
        condition = re.search(r"^    if: (.*)$", PUBLISH_JOB_BLOCK, re.M)
        self.assertIsNotNone(condition, f"ci.yml's {PUBLISH_JOB} job has no `if:` condition")
        self.assertIn("github.event_name == 'push'", condition.group(1))
        self.assertIn("github.ref == 'refs/heads/main'", condition.group(1))

    def test_the_branch_the_doc_reads_is_the_branch_the_job_pushes(self) -> None:
        for block in fenced_blocks(self.section):
            if "git fetch" in block:
                self.assertIn(f"origin {PUBLISH_BRANCH}:", block)
                break
        else:
            self.fail("the section no longer shows how to fetch the coverage branch")
        self.assertIn(
            f"push origin HEAD:{PUBLISH_BRANCH}",
            PUBLISH_JOB_BLOCK,
            f"ci.yml's {PUBLISH_JOB} job no longer pushes to {PUBLISH_BRANCH}",
        )

    def test_the_trend_file_the_doc_reads_is_the_file_the_job_writes(self) -> None:
        shown = [block for block in fenced_blocks(self.section) if "git show" in block]
        self.assertTrue(shown, "the section no longer shows how to read the trend")
        self.assertIn(f"origin/{PUBLISH_BRANCH}:{TREND_FILE}", shown[0])
        self.assertRegex(
            PUBLISH_JOB_BLOCK,
            rf"--trend-out \"\$badge_worktree/{re.escape(TREND_FILE)}\"",
        )
        self.assertIn(f"add {BADGE_FILE} {TREND_FILE}", PUBLISH_JOB_BLOCK)

    def test_the_csv_columns_are_the_columns_the_writer_produces(self) -> None:
        # The doc names the columns in a trailing comment. Rather than compare
        # that text to another piece of text, the writer is called with a
        # distinguishable value per column, so a re-ordered trend_row fails
        # here even though every column name is still present.
        shown = [block for block in fenced_blocks(self.section) if "git show" in block]
        comment = re.search(r"#\s*(.+)$", shown[0], re.M)
        self.assertIsNotNone(comment, "the trend command no longer names its columns")
        columns = [part.strip() for part in comment.group(1).split(",")]
        self.assertEqual(len(columns), 3, f"expected three columns, doc names {columns}")
        sentinels = {"date": "2026-01-02", "sha": "0123456789ab", "percent": "77"}
        self.assertEqual(
            sorted(columns),
            sorted(sentinels),
            f"docs/metrics.md names columns {columns}, which coverage_badge does not write",
        )
        produced = trend_row(
            date=sentinels["date"], sha=sentinels["sha"], percent=int(sentinels["percent"])
        )
        self.assertEqual(
            produced.split(","),
            [sentinels[column] for column in columns],
            f"coverage_badge.trend_row writes {produced}, "
            f"not the {','.join(columns)} order this doc documents",
        )

    def test_the_badge_the_doc_points_at_is_the_file_the_job_writes(self) -> None:
        self.assertIn("README badge", self.flat)
        endpoints = re.findall(r"url=(https%3A[^)\s]+)", README_PATH.read_text())
        self.assertEqual(len(endpoints), 1, "README has no single shields endpoint badge")
        decoded = urllib.parse.unquote(endpoints[0])
        self.assertTrue(
            decoded.endswith(f"/{SLUG}/{PUBLISH_BRANCH}/{BADGE_FILE}"),
            f"README's coverage badge reads {decoded}, not {BADGE_FILE} on {PUBLISH_BRANCH}",
        )
        self.assertRegex(
            PUBLISH_JOB_BLOCK,
            rf"--badge-out \"\$badge_worktree/{re.escape(BADGE_FILE)}\"",
        )

    def test_the_gate_comes_from_the_thresholds_file_and_not_the_workflow(self) -> None:
        self.assertIn(
            "The gate itself comes from `.coverage-thresholds.json`, not from the workflow",
            self.flat,
        )
        self.assertIn(".coverage-thresholds.json", CI_WORKFLOW.read_text())
        self.assertNotRegex(CI_WORKFLOW.read_text(), r"--fail-under=\d")

    def test_the_thresholds_file_gates_the_tier_this_section_is_about(self) -> None:
        self.assertEqual(
            sorted(THRESHOLDS["gated"]),
            ["unit"],
            "the unit tier is no longer the only gated measurement this section can mean",
        )
        self.assertIsInstance(THRESHOLDS["gated"]["unit"], int)

    def test_the_count_of_measurements_matches_the_thresholds_file(self) -> None:
        stated = re.search(r"describes all (\w+) coverage measurements", self.flat)
        self.assertIsNotNone(stated, "the section no longer says how many measurements there are")
        self.assertEqual(
            NUMBER_WORDS[stated.group(1)],
            len(THRESHOLDS["gated"]) + len(ADVISORY_TIERS),
            ".coverage-thresholds.json no longer holds the number of measurements this doc claims",
        )

    def test_contributing_states_the_same_count(self) -> None:
        # The doc defers to CONTRIBUTING.md for the explanation, so the two
        # counts have to agree or the reader is sent to a contradiction.
        stated = re.search(
            r"There are (\w+) separate coverage measurements", CONTRIBUTING_PATH.read_text()
        )
        self.assertIsNotNone(stated, "CONTRIBUTING.md no longer states the measurement count")
        self.assertEqual(
            NUMBER_WORDS[stated.group(1)],
            len(THRESHOLDS["gated"]) + len(ADVISORY_TIERS),
        )


class AdvisoryTiers(unittest.TestCase):
    """The four advisory tiers, their artifacts, and how to download them."""

    def setUp(self) -> None:
        self.section = SECTIONS[TIERS_SECTION]
        self.flat = flatten(self.section)

    def test_the_tier_wording_table_covers_every_advisory_tier(self) -> None:
        # Without this the table can fall behind .coverage-thresholds.json and
        # every tier assertion below silently stops covering the new tier.
        self.assertEqual(sorted(TIER_WORDS), sorted(ADVISORY_TIERS))

    def test_the_heading_states_the_number_of_advisory_tiers(self) -> None:
        stated = re.search(r"The other (\w+) coverage tiers", TIERS_SECTION)
        self.assertIsNotNone(stated, "the section heading no longer counts the tiers")
        self.assertEqual(NUMBER_WORDS[stated.group(1)], len(ADVISORY_TIERS))

    def test_the_tier_sentence_names_exactly_the_advisory_tiers(self) -> None:
        stated = re.search(
            r"(.+?) coverage are uploaded as workflow artifacts and expire after (\d+) days",
            self.flat,
        )
        self.assertIsNotNone(stated, "the section no longer says which tiers it is about")
        named = [part.strip() for part in re.split(r",\s*|\s+and\s+", stated.group(1)) if part.strip()]
        self.assertEqual(
            sorted(named),
            sorted(TIER_WORDS[tier] for tier in ADVISORY_TIERS),
            "docs/metrics.md names a different set of tiers than .coverage-thresholds.json calls advisory",
        )

    def test_the_gated_tier_is_not_one_of_them(self) -> None:
        for gated in THRESHOLDS["gated"]:
            with self.subTest(tier=gated):
                self.assertNotIn(
                    f"coverage-{gated}",
                    self.section,
                    f"the section about the ungated tiers offers coverage-{gated}, "
                    f"which is the gated one",
                )

    def test_the_artifact_scan_finds_a_coverage_upload_for_every_measurement(self) -> None:
        self.assertEqual(
            sorted(step["name"] for step in UPLOADS),
            sorted(
                [f"coverage-{tier}" for tier in THRESHOLDS["gated"]]
                + [f"coverage-{tier}" for tier in ADVISORY_TIERS]
            ),
            "the workflows no longer upload one coverage artifact per measurement",
        )

    def test_the_download_command_names_every_advisory_artifact(self) -> None:
        blocks = [block for block in fenced_blocks(self.section) if "gh run download" in block]
        self.assertTrue(blocks, "the section no longer shows how to download the artifacts")
        comment = re.search(r"#\s*(.+)$", blocks[0], re.M)
        self.assertIsNotNone(comment, "the download command no longer names the artifacts")
        offered = [part.strip() for part in comment.group(1).split(",")]
        self.assertEqual(
            sorted(offered),
            sorted(f"coverage-{tier}" for tier in ADVISORY_TIERS),
            "the download command offers a different set of artifacts than the tiers "
            "this section is about",
        )

    def test_every_artifact_the_download_command_offers_is_uploaded_by_a_workflow(self) -> None:
        blocks = [block for block in fenced_blocks(self.section) if "gh run download" in block]
        comment = re.search(r"#\s*(.+)$", blocks[0], re.M)
        uploaded = {step["name"] for step in UPLOADS}
        for artifact in (part.strip() for part in comment.group(1).split(",")):
            with self.subTest(artifact=artifact):
                self.assertIn(
                    artifact, uploaded, f"no workflow uploads an artifact called {artifact}"
                )

    def test_every_coverage_artifact_expires_when_the_doc_says(self) -> None:
        stated = re.search(r"expire after (\d+) days", self.flat)
        self.assertIsNotNone(stated, "the section no longer states a retention period")
        self.assertTrue(UPLOADS, "no coverage upload steps were found to check")
        for step in UPLOADS:
            with self.subTest(artifact=step["name"], workflow=step["workflow"]):
                self.assertEqual(
                    step.get("retention-days"),
                    stated.group(1),
                    f"{step['workflow']} keeps {step['name']} for "
                    f"{step.get('retention-days')} days, not the {stated.group(1)} this doc promises",
                )

    def test_the_advisory_tiers_are_ungated(self) -> None:
        self.assertIn("They are", self.flat)
        self.assertIn("advisory", self.flat)
        for tier in ADVISORY_TIERS:
            with self.subTest(tier=tier):
                self.assertNotIn(
                    tier,
                    THRESHOLDS["gated"],
                    f"{tier} is documented as advisory but .coverage-thresholds.json gates it",
                )


class WeeklyAudit(unittest.TestCase):
    """The audit's schedule and the maintainer section the doc defers to."""

    def setUp(self) -> None:
        self.section = SECTIONS[AUDIT_SECTION]
        self.flat = flatten(self.section)

    def test_the_audit_workflow_the_section_names_is_committed(self) -> None:
        self.assertIn("maintenance-audit.yml", self.flat)
        self.assertIn(
            str(AUDIT_WORKFLOW.relative_to(ROOT)),
            TRACKED,
            "the weekly audit workflow this section is about is not committed",
        )

    def test_the_audit_runs_on_the_day_the_doc_says(self) -> None:
        stated = re.search(r"runs (\w+days)\b", self.flat)
        self.assertIsNotNone(stated, "the section no longer says which day the audit runs")
        day = stated.group(1).rstrip("s")
        self.assertIn(day, WEEKDAY_NUMBERS, f"{stated.group(1)} is not a day of the week")
        crons = re.findall(r"^\s*- cron: '([^']+)'", AUDIT_WORKFLOW.read_text(), re.M)
        self.assertEqual(len(crons), 1, f"maintenance-audit.yml has {len(crons)} schedules, expected one")
        fields = crons[0].split()
        self.assertEqual(len(fields), 5, f"unreadable cron expression {crons[0]!r}")
        self.assertEqual(
            fields[4],
            str(WEEKDAY_NUMBERS[day]),
            f"maintenance-audit.yml runs on cron day {fields[4]}, "
            f"not the {day} this doc promises",
        )

    def test_the_maintainer_section_the_doc_quotes_is_a_heading(self) -> None:
        quoted = re.search(r"\*([^*]+)\*", self.flat)
        self.assertIsNotNone(quoted, "the section no longer points at a part of MAINTAINER.md")
        titles = {
            match.group(1).strip()
            for match in re.finditer(r"^#+\s+(.*)$", MAINTAINER_PATH.read_text(), re.M)
        }
        self.assertIn(
            quoted.group(1),
            titles,
            f"MAINTAINER.md has no '{quoted.group(1)}' section for this doc to send a reader to",
        )

    def test_every_workflow_a_command_names_is_committed(self) -> None:
        named = re.findall(r"--workflow (\S+\.yml)", DOC)
        self.assertTrue(named, "no `gh run list --workflow` command left to check")
        for workflow in named:
            with self.subTest(workflow=workflow):
                self.assertIn(f".github/workflows/{workflow}", TRACKED)


class Commands(unittest.TestCase):
    """The reproduction commands, and the repository they are run against."""

    def test_the_command_scan_finds_every_reproduction_block(self) -> None:
        blocks = fenced_blocks(DOC)
        self.assertGreaterEqual(
            len(blocks), 4, "docs/metrics.md is supposed to carry one command per number"
        )
        for section in (HISTORY_SECTION, TIERS_SECTION, THROUGHPUT_SECTION, FINDINGS_SECTION, AUDIT_SECTION):
            with self.subTest(section=section):
                self.assertTrue(
                    fenced_blocks(SECTIONS[section]),
                    f"'{section}' promises a reproduction command and shows none",
                )

    def test_every_repository_a_command_queries_is_this_repository(self) -> None:
        queried = re.findall(r"repos/([\w.-]+/[\w.-]+)/", DOC)
        self.assertTrue(queried, "no `gh api repos/...` command left to check")
        for slug in queried:
            with self.subTest(slug=slug):
                self.assertEqual(
                    slug, SLUG, f"a command reads {slug}, which is not this repository"
                )

    def test_every_issue_the_doc_cites_is_an_issue_in_this_repository(self) -> None:
        cited = re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", DOC)
        self.assertTrue(cited, "the doc no longer cites the issues its warnings come from")
        for slug, number in cited:
            with self.subTest(issue=number):
                self.assertEqual(slug, SLUG)

    def test_the_caveats_point_at_the_record_they_claim_exists(self) -> None:
        # "the record of the ones that changed a decision is in
        # .claude/memory/corrections.md" -- a promise that the file is there
        # and says something, not merely that a path was spelled.
        corrections = ROOT / ".claude/memory/corrections.md"
        self.assertIn(str(corrections.relative_to(ROOT)), DOC)
        self.assertIn(str(corrections.relative_to(ROOT)), TRACKED)
        self.assertTrue(corrections.read_text().strip(), "the corrections record is empty")

    def test_the_caveat_about_the_gated_tier_is_in_the_caveats_section(self) -> None:
        caveats = flatten(SECTIONS[CAVEATS_SECTION])
        self.assertIn("100% unit coverage is not 100% tested", caveats)
        self.assertIn("end-to-end tier", caveats)


if __name__ == "__main__":
    unittest.main()

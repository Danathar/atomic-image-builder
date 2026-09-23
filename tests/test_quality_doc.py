"""Join docs/quality.md, the quality index, to the files it summarizes.

Script: tests/test_quality_doc.py
What: Reads docs/quality.md as a subject and checks each claim it makes about
      the repository -- which coverage tiers exist and which one is gated,
      which linters gate the build and in which job, which artifact or
      workflow each signal lives in, what "the gate" runs on, how many files
      quote the threshold, and where the trend is kept -- against the
      workflow, config or document that decides it.
Doing: Parses the signal table out of its section and classifies every row
       exhaustively: a coverage tier (joined to `.coverage-thresholds.json`
       both ways), the linter row (joined to the `test` job's `Run <tool>`
       steps as a set), or one of the named non-coverage signals. Resolves
       each Where cell to an upload step or workflow, reads number words out
       of the prose and compares them to counts taken from the machine, and
       resolves every link against the committed tree.
Why: This page is the index every other quality document is reached from,
     and until now no test read it as a subject. The one test that opened it
     (test_quality_index_quotes_the_gate_from_its_source) checks the gate's
     number and nothing else, so a new advisory tier, a fifth gated linter or
     a renamed artifact left this table describing a repository that no
     longer exists. `.coveragerc` measures Python modules and ruff does not
     read Markdown, so no percentage moves when it drifts.
Goal: Make a tier, linter, artifact, trigger or count that changes without
      this page fail here.

The gate number itself is not re-asserted: test_atomic_image_builder.py owns
that, and a second copy of the same check would be the duplication the page
itself warns about.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "docs/quality.md"
DOC_RELATIVE = "docs/quality.md"
DOC = DOC_PATH.read_text()

WORKFLOW_DIR = ROOT / ".github/workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
THRESHOLDS = json.loads((ROOT / ".coverage-thresholds.json").read_text())

SIGNALS_SECTION = "The signals, and how much to trust them"
MISREAD_SECTION = "The two numbers most often misread"
ENFORCED_SECTION = "How quality is actually enforced"
DASHBOARD_SECTION = "Why there is no dashboard"
NAMED_SECTIONS = (SIGNALS_SECTION, MISREAD_SECTION, ENFORCED_SECTION, DASHBOARD_SECTION)

TABLE_HEADER = ["Signal", "Gated?", "Where", "Worth"]

# How each key in .coverage-thresholds.json is spelled as a row of the table.
# The file writes "e2e", the page writes "End-to-end coverage"; without this
# the join is a substring search that "maintenance-audit" would pass by
# accident. test_the_tier_wording_table_is_exactly_the_tier_set keeps it
# complete in both directions.
TIER_ROWS = {
    "unit": "Unit coverage",
    "e2e": "End-to-end coverage",
    "shell": "Shell-entrypoint coverage",
    "maintenance-audit": "Maintenance-audit coverage",
    "homebrew-release": "Homebrew-release coverage",
}

# Where each tier's number is uploaded from. The Where cell of an advisory row
# either names the artifact (`coverage-e2e`) or describes the workflow that
# uploads it ("weekly artifact"); either way it has to resolve to the one
# upload step that really produces the tier.
TIER_ARTIFACTS = {
    "unit": "coverage-unit",
    "e2e": "coverage-e2e",
    "shell": "coverage-shell",
    "maintenance-audit": "coverage-maintenance-audit",
    "homebrew-release": "coverage-homebrew-release",
}

# The rows that are not a coverage tier and not the linter row. Named rather
# than skipped: a new row has to be classified here before this module will
# pass, so the table cannot grow a signal nothing checks.
OTHER_ROWS = {"Weekly audit", "Review findings per PR"}

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def split_sections(text: str) -> dict[str, str]:
    """Map each heading's exact title to the body beneath it."""
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


def number_word(word: str) -> int:
    """A spelled-out count, failing loudly on a word this module cannot read."""
    lowered = word.lower()
    if lowered not in NUMBER_WORDS:
        raise AssertionError(f"{DOC_RELATIVE} uses the count {word!r}, which NUMBER_WORDS cannot read")
    return NUMBER_WORDS[lowered]


def signal_rows() -> list[list[str]]:
    """The signal table's body rows, header and separator removed."""
    rows = []
    for line in SECTIONS.get(SIGNALS_SECTION, "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if set(cells[0]) <= {"-", ":", " "}:
            continue
        rows.append(cells)
    return rows


ROWS = signal_rows()


def uncommented(text: str) -> str:
    """A workflow with its whole-line comments removed.

    Only whole lines: a `#` inside a cron or a shell body is content.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def job_block(workflow: Path, job_id: str) -> str:
    """The lines of one top-level job, by id, bounded by indentation."""
    lines = uncommented(workflow.read_text()).splitlines()
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


def trigger_keys(workflow: Path) -> set[str]:
    """The event names under a workflow's top-level `on:` mapping."""
    lines = uncommented(workflow.read_text()).splitlines()
    keys: set[str] = set()
    inside = False
    for line in lines:
        if re.match(r"^on:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith(" "):
                break
            key = re.match(r"^  ([a-z_]+):", line)
            if key:
                keys.add(key.group(1))
    return keys


def cron_lines(workflow: Path) -> list[str]:
    """The five cron fields of every schedule entry in a workflow."""
    return re.findall(r"cron:\s*['\"]([^'\"]+)['\"]", uncommented(workflow.read_text()))


def coverage_uploads() -> dict[str, str]:
    """Map every uploaded `coverage-*` artifact to the workflow that uploads it.

    Discovered from the workflows, so a tier that stops being uploaded drops
    out of this map rather than staying in a hand-written list.
    """
    found: dict[str, str] = {}
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        lines = uncommented(workflow.read_text()).splitlines()
        for index, line in enumerate(lines):
            uses = re.match(r"^(\s*)uses: actions/upload-artifact@", line)
            if not uses:
                continue
            indent = len(uses.group(1))
            for following in lines[index + 1 :]:
                if following.strip() and (
                    len(following) - len(following.lstrip()) < indent or following.lstrip().startswith("- ")
                ):
                    break
                name = re.match(r"^\s*name:\s*(coverage-\S+)\s*$", following)
                if name:
                    if name.group(1) in found:
                        raise AssertionError(f"{name.group(1)} is uploaded by more than one step")
                    found[name.group(1)] = workflow.name
    return found


UPLOADS = coverage_uploads()


def gated_linters() -> set[str]:
    """The tools the `test` job runs as `- name: Run <tool>` lint steps.

    A single-word step name is the lint shape; "Run tests" is the suite, not
    a linter, and is excluded by name.
    """
    names = re.findall(r"^\s*- name: Run (\S+)\s*$", job_block(CI_WORKFLOW, "test"), re.M)
    return {name for name in names if name != "tests"}


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


def files_quoting_the_threshold() -> set[str]:
    """Every current document that states the gate's number on a gate line.

    A dated reflection is a record of a day, not a claim about now, and a
    pinned upstream snapshot is not this repo's prose; neither is counted.
    """
    threshold = THRESHOLDS["gated"]["unit"]
    gate_line = re.compile(r"gate|fail-under|fails the build", re.I)
    value = re.compile(rf"(?<![\d.]){threshold}(?![\d.])")
    quoting = set()
    for name in TRACKED:
        if not name.endswith(".md") or name.startswith(("tests/", "template_snapshots/", "docs/reflections/")):
            continue
        for line in (ROOT / name).read_text().splitlines():
            if gate_line.search(line) and value.search(line):
                quoting.add(name)
                break
    return quoting


class QualityDocTests(unittest.TestCase):
    def test_every_section_this_module_scopes_by_still_exists(self) -> None:
        # Every assertion below is scoped to one of these; a renamed heading
        # would make the assertions under it vacuous rather than red.
        for title in NAMED_SECTIONS:
            self.assertIn(title, SECTIONS, f"{DOC_RELATIVE} no longer has a {title!r} section")
            self.assertTrue(SECTIONS[title].strip(), f"{DOC_RELATIVE}'s {title!r} section is empty")

    def test_the_signal_table_keeps_its_columns(self) -> None:
        self.assertTrue(ROWS, f"{DOC_RELATIVE} no longer has a signal table")
        self.assertEqual(ROWS[0], TABLE_HEADER, f"{DOC_RELATIVE}'s signal table changed its columns")
        self.assertGreater(len(ROWS), 1, f"{DOC_RELATIVE}'s signal table has no rows")
        for row in ROWS[1:]:
            self.assertEqual(len(row), len(TABLE_HEADER), f"row {row[0]!r} has {len(row)} cells")
            self.assertTrue(all(row), f"row {row[0]!r} has an empty cell")

    def test_the_tier_wording_table_is_exactly_the_tier_set(self) -> None:
        # Both directions: a tier added to the thresholds file with no row
        # spelling here, and a spelling left behind for a tier that is gone.
        tiers = set(THRESHOLDS["gated"]) | set(THRESHOLDS["advisory"])
        self.assertEqual(set(TIER_ROWS), tiers, "TIER_ROWS disagrees with .coverage-thresholds.json")
        self.assertEqual(set(TIER_ARTIFACTS), tiers, "TIER_ARTIFACTS disagrees with .coverage-thresholds.json")

    def test_every_row_is_classified(self) -> None:
        # Exhaustive: each row is a coverage tier, the linter row, or a named
        # other signal. An unclassified row means the table grew a signal no
        # assertion here reads.
        tier_rows = set(TIER_ROWS.values())
        for row in ROWS[1:]:
            name = row[0]
            classified = name in tier_rows or name in OTHER_ROWS or name.startswith("`")
            self.assertTrue(
                classified,
                f"{DOC_RELATIVE}'s signal table has a row {name!r} that this module does not classify",
            )
        names = [row[0] for row in ROWS[1:]]
        for name in sorted(OTHER_ROWS):
            self.assertIn(name, names, f"OTHER_ROWS names {name!r}, which is no longer a row; drop it")

    def test_the_coverage_rows_are_exactly_the_tiers_and_only_the_gated_one_says_yes(self) -> None:
        by_name = {row[0]: row for row in ROWS[1:]}
        doc_tiers = {key for key, row_name in TIER_ROWS.items() if row_name in by_name}
        tiers = set(THRESHOLDS["gated"]) | set(THRESHOLDS["advisory"])
        self.assertEqual(
            doc_tiers,
            tiers,
            f"{DOC_RELATIVE}'s coverage rows disagree with .coverage-thresholds.json "
            f"(missing {sorted(tiers - doc_tiers)})",
        )
        coverage_rows = [row for row in ROWS[1:] if row[0].endswith(" coverage")]
        self.assertEqual(
            len(coverage_rows),
            len(tiers),
            f"{DOC_RELATIVE} has a coverage row that is not a tier in .coverage-thresholds.json",
        )
        for key, row_name in TIER_ROWS.items():
            gated_cell = by_name[row_name][1]
            if key in THRESHOLDS["gated"]:
                self.assertRegex(gated_cell, r"^\*\*Yes\*\*", f"{row_name} is gated and the table says {gated_cell!r}")
            else:
                self.assertEqual(gated_cell, "No", f"{row_name} is advisory and the table says {gated_cell!r}")

    def test_the_linter_row_is_exactly_the_gated_lint_steps(self) -> None:
        # Set equality with the `test` job's lint steps: the maintainer
        # handbook's workflow table once named one of four linters, and a
        # containment check could not have seen it.
        linter_rows = [row for row in ROWS[1:] if row[0].startswith("`")]
        self.assertEqual(len(linter_rows), 1, f"{DOC_RELATIVE} should have exactly one linter row")
        row = linter_rows[0]
        named = set(re.findall(r"`([^`]+)`", row[0]))
        machine = gated_linters()
        self.assertTrue(machine, "ci.yml's test job has no `Run <tool>` lint step")
        self.assertEqual(
            named,
            machine,
            f"{DOC_RELATIVE}'s linter row names {sorted(named)}, ci.yml's test job runs {sorted(machine)}",
        )
        self.assertRegex(row[1], r"^\*\*Yes\*\*", "the linter row no longer says the linters are gated")
        where = re.fullmatch(r"`([\w.-]+\.yml)` `([\w-]+)` job", row[2])
        self.assertIsNotNone(where, f"the linter row's Where cell changed shape: {row[2]!r}")
        workflow, job = where.groups()
        self.assertTrue((WORKFLOW_DIR / workflow).is_file(), f"the linter row names {workflow}, which does not exist")
        self.assertTrue(job_block(WORKFLOW_DIR / workflow, job), f"{workflow} has no job {job!r}")
        # And the gated job must run unconditionally, or "either clean or the
        # build is red" is only true on the runs that reach it.
        self.assertNotRegex(job_block(WORKFLOW_DIR / workflow, job), r"(?m)^    if:", f"{job} became conditional")

    def test_every_artifact_the_table_names_is_uploaded_for_that_tier(self) -> None:
        by_name = {row[0]: row for row in ROWS[1:]}
        self.assertEqual(
            set(TIER_ARTIFACTS.values()),
            set(UPLOADS),
            "the coverage artifacts uploaded by the workflows are not exactly one per tier",
        )
        for key, row_name in TIER_ROWS.items():
            where = by_name[row_name][2]
            # A span followed by "branch" is where the unit number is
            # published, not an artifact; the unit row checks that one.
            spans = re.findall(r"`(coverage-[\w-]+)`(?! branch)", where)
            for span in spans:
                self.assertEqual(
                    span,
                    TIER_ARTIFACTS[key],
                    f"{row_name}'s Where cell names {span}, which is not that tier's artifact",
                )

    def test_the_described_artifacts_come_from_the_workflow_described(self) -> None:
        # "weekly artifact" and "release artifact" name a workflow by what
        # triggers it rather than by file, so resolve the uploader and check
        # its trigger.
        by_name = {row[0]: row for row in ROWS[1:]}
        described = 0
        for key, row_name in TIER_ROWS.items():
            where = by_name[row_name][2]
            if "`" in where or "badge" in where:
                continue
            described += 1
            workflow = WORKFLOW_DIR / UPLOADS[TIER_ARTIFACTS[key]]
            if where == "weekly artifact":
                crons = cron_lines(workflow)
                self.assertEqual(len(crons), 1, f"{workflow.name} should run on one schedule")
                fields = crons[0].split()
                self.assertTrue(
                    fields[2] == "*" and fields[3] == "*" and re.fullmatch(r"\d", fields[4]),
                    f"{workflow.name}'s cron {crons[0]!r} is not weekly",
                )
            elif where == "release artifact":
                self.assertIn("release", trigger_keys(workflow), f"{workflow.name} is not triggered by a release")
            else:
                self.fail(f"{row_name}'s Where cell {where!r} is neither an artifact nor a described one")
        self.assertGreater(described, 0, "no Where cell describes its workflow, so this checked nothing")

    def test_the_unit_row_names_where_the_number_is_published(self) -> None:
        # "README badge, `coverage-data` branch": the badge is shown, and the
        # branch is where publish-coverage pushes the badge and trend files.
        row = next(row for row in ROWS[1:] if row[0] == TIER_ROWS["unit"])
        branches = re.findall(r"`([\w-]+)` branch", row[2])
        self.assertEqual(len(branches), 1, f"the unit row's Where cell changed shape: {row[2]!r}")
        publish = job_block(CI_WORKFLOW, "publish-coverage")
        self.assertIn(f"HEAD:{branches[0]}", publish, f"publish-coverage no longer pushes to {branches[0]}")
        if "README badge" in row[2]:
            # The badge URL is percent-encoded inside the shields.io endpoint.
            readme = urllib.parse.unquote((ROOT / "README.md").read_text())
            self.assertIn(f"/{branches[0]}/coverage-unit.json", readme, "README's badge no longer reads that branch")

    def test_the_weekly_audit_row_names_a_weekly_workflow(self) -> None:
        row = next(row for row in ROWS[1:] if row[0] == "Weekly audit")
        named = re.findall(r"`([\w.-]+\.yml)`", row[2])
        self.assertEqual(len(named), 1, f"the Weekly audit row should name one workflow: {row[2]!r}")
        workflow = WORKFLOW_DIR / named[0]
        self.assertTrue(workflow.is_file(), f"the Weekly audit row names {named[0]}, which does not exist")
        crons = cron_lines(workflow)
        self.assertEqual(len(crons), 1)
        self.assertRegex(crons[0].split()[4], r"^\d$", f"{named[0]} is not weekly")

    def test_the_tier_counts_in_the_prose_match_the_thresholds_file(self) -> None:
        # "explains all five coverage tiers and why exactly one is gated".
        section = flatten(SECTIONS[SIGNALS_SECTION])
        match = re.search(r"all (\w+) coverage tiers and why exactly (\w+) is gated", section)
        self.assertIsNotNone(match, f"{DOC_RELATIVE} no longer states how many tiers exist and are gated")
        total, gated = (number_word(word) for word in match.groups())
        self.assertEqual(total, len(THRESHOLDS["gated"]) + len(THRESHOLDS["advisory"]))
        self.assertEqual(gated, len(THRESHOLDS["gated"]))

    def test_the_gate_runs_on_every_push_and_pull_request(self) -> None:
        # "**The gate**, on every push and PR." True only while ci.yml runs on
        # both events and the step that applies --fail-under sits in a job
        # with no `if:`.
        section = flatten(SECTIONS[ENFORCED_SECTION])
        self.assertIn("**The gate**, on every push and PR", section)
        self.assertLessEqual({"push", "pull_request"}, trigger_keys(CI_WORKFLOW))
        test_job = job_block(CI_WORKFLOW, "test")
        self.assertRegex(test_job, r"--fail-under=\"\$threshold\"", "the test job no longer applies the gate")
        self.assertNotRegex(test_job, r"(?m)^    if:", "the gated job became conditional")
        self.assertIn("`.coverage-thresholds.json`", section)
        self.assertIn(".coverage-thresholds.json", test_job)

    def test_the_count_of_files_quoting_the_threshold_is_current(self) -> None:
        # "the coverage threshold across N files". Counted from the tree, so
        # a new document that quotes the number moves the count, and so does
        # one that stops.
        section = flatten(SECTIONS[ENFORCED_SECTION])
        match = re.search(r"the coverage threshold across (\w+) files", section)
        self.assertIsNotNone(match, f"{DOC_RELATIVE} no longer says how many files quote the threshold")
        quoting = files_quoting_the_threshold()
        self.assertIn(DOC_RELATIVE, quoting, "the count scan no longer sees this page's own quote")
        self.assertEqual(
            number_word(match.group(1)),
            len(quoting),
            f"{DOC_RELATIVE} says {match.group(1)} files; these quote the threshold: {sorted(quoting)}",
        )

    def test_the_weekly_audit_is_scheduled_weekly(self) -> None:
        section = flatten(SECTIONS[ENFORCED_SECTION])
        self.assertIn("**The weekly audit**", section)
        crons = cron_lines(WORKFLOW_DIR / "maintenance-audit.yml")
        self.assertEqual(len(crons), 1)
        self.assertRegex(crons[0].split()[4], r"^\d$")

    def test_the_mechanical_enforcement_file_exists(self) -> None:
        section = flatten(SECTIONS[ENFORCED_SECTION])
        spans = set(re.findall(r"`([^`]+)`", section))
        self.assertIn(".claude/settings.json", spans)
        for span in spans:
            if "/" in span or span.startswith("."):
                self.assertIn(span, TRACKED, f"{DOC_RELATIVE} names {span}, which is not committed")

    def test_the_trend_is_kept_where_the_page_says(self) -> None:
        section = flatten(SECTIONS[DASHBOARD_SECTION])
        match = re.search(r"`([\w.-]+\.csv)` on the `([\w-]+)` branch", section)
        self.assertIsNotNone(match, f"{DOC_RELATIVE} no longer says where the trend is kept")
        trend_file, branch = match.groups()
        publish = job_block(CI_WORKFLOW, "publish-coverage")
        self.assertIn(f"--trend-out \"$badge_worktree/{trend_file}\"", publish)
        self.assertIn(f"HEAD:{branch}", publish)
        # "artifacts expire after 30 days": every coverage upload that sets a
        # retention sets that one.
        retentions = set(re.findall(r"retention-days:\s*(\d+)", uncommented(CI_WORKFLOW.read_text())))
        days = re.search(r"artifacts expire after (\d+) days", section)
        self.assertIsNotNone(days)
        self.assertEqual(retentions, {days.group(1)})

    def test_every_link_resolves(self) -> None:
        found = re.findall(r"\[[^\]\n]+\]\(([^)\s]+)\)", DOC)
        self.assertTrue(found, f"{DOC_RELATIVE} has no links, so this checked nothing")
        for target in found:
            if target.startswith("https://"):
                continue
            path, _, anchor = target.partition("#")
            resolved = (DOC_PATH.parent / path).resolve()
            relative = str(resolved.relative_to(ROOT))
            self.assertIn(relative, TRACKED, f"{DOC_RELATIVE} links {target}, which is not committed")
            if anchor:
                headings = re.findall(r"^#+\s+(.*)$", resolved.read_text(), re.M)
                slugs = {re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", h.strip().lower())) for h in headings}
                self.assertIn(anchor, slugs, f"{DOC_RELATIVE} links {target}, and that heading does not exist")


if __name__ == "__main__":
    unittest.main()

"""Join `maintainer_docs/MAINTAINER.md`'s *What runs automatically* table to the workflows.

Script: tests/test_maintainer_workflow_table.py
What: Reads the handbook's eight-row CI index -- which workflows exist, what
      fires each one, and what each one does -- against `.github/workflows/`,
      `.coverage-thresholds.json`, and the step bodies the rows describe.
Doing: Parses the table out of the *What runs automatically* section and strips
       the backticks off the first column; walks each workflow's `on:` block by
       indentation, comments removed, to get the triggers it actually declares,
       and compares them against the *Fires on* cell in both directions; turns a
       `cron` field into a day name and a zero-padded UTC time and looks for
       both in the cell; collects the `test` job's `Run <tool>` step names and
       the `case` patterns that apply `security`; and checks the remaining
       row claims (`--add-label` with no `--remove-label`, the `ai-fix`
       trigger label, `--no-cache`, the path-scoped image build, the gate
       percentage) against the lines that decide each.
Why: The table is the handbook's index of CI and it was joined to nothing. The
     three tests in tests/test_maintainer_doc.py read the release procedure;
     everything else that reaches this file reaches one sentence and never a
     row -- the coverage-threshold sweep in tests/test_atomic_image_builder.py
     reads any line stating the gate, tests/test_ai_fix_workflow.py asserts the
     trigger label appears somewhere in the file, and
     tests/test_verify_change_skill.py and tests/test_prompt_catalog.py split
     the document on its `##` headings and assert section titles. No percentage
     moves when a row goes stale either: `.coveragerc` measures the Python
     modules, ruff does not read Markdown, and the Containerfile never copies
     `maintainer_docs/` into the image, so no coverage tier can reach it.
Goal: Make a new workflow, a renamed one, a changed trigger, a moved cron, a
      new linter in the gate and a new `security` pattern fail here, instead of
      leaving the handbook describing a CI that has moved on.

The table drifted by omission in exactly the way a summary column invites. The
`ci.yml` row named `ruff` while the `test` job gates on `shellcheck`,
`actionlint` and `hadolint` too, and the `triage.yml` row named three of the
five patterns that apply `security` -- one of the two it dropped, `signing
key`, having been added after `"Signing key is missing"` slipped through. Both
directions of the trigger join are asserted for the same reason: a cell that
omits a trigger hides a way the workflow starts, and a cell that names one the
workflow dropped sends a maintainer to wait for a run that will never fire.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAINTAINER_RELATIVE = "maintainer_docs/MAINTAINER.md"
MAINTAINER = ROOT / MAINTAINER_RELATIVE
WORKFLOW_DIR = ROOT / ".github/workflows"
THRESHOLDS = ROOT / ".coverage-thresholds.json"

TABLE_SECTION = "What runs automatically"

# The `on:` keys these workflows use, each paired with the wording the table is
# allowed to describe it with. Matched against the *Fires on* cell in both
# directions, so the pattern has to be specific enough that a cell describing a
# different trigger does not satisfy it: "dispatch" appears in no other cell
# wording, and `\bPR\b` does not match "prompt" or "published".
TRIGGER_WORDING = {
    "push": r"\bpush\b",
    "pull_request": r"\bPR\b|\bpull request\b",
    "workflow_dispatch": r"\bdispatch\b",
    "release": r"\brelease\b",
    "schedule": r"\bUTC\b",
    "issues": r"\bissue\b|\blabel\b",
}

# cron day-of-week, as the table spells it. `*` is every day; the numeric forms
# are only what this repo's two scheduled workflows use, and an unrecognised
# field fails rather than being skipped.
CRON_DAYS = {
    "*": "Daily",
    "0": "Sunday",
    "1": "Monday",
    "2": "Tuesday",
    "3": "Wednesday",
    "4": "Thursday",
    "5": "Friday",
    "6": "Saturday",
}


def _uncommented(text: str) -> list[str]:
    """The lines of a workflow with whole-line `#` comments dropped.

    Only whole-line comments: a `#` inside a cron expression or a shell body is
    content, and stripping from the first `#` on every line would corrupt both.
    """
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def triggers(workflow: Path) -> set[str]:
    """The keys of a workflow's `on:` block.

    Walked by indentation rather than parsed with PyYAML: CI installs only
    `coverage` and `ruff` for the unit suite, the same constraint
    tests/_workflow_steps.py is written under.
    """
    found: set[str] = set()
    inside = False
    for line in _uncommented(workflow.read_text()):
        if not line.strip():
            continue
        if line.startswith("on:"):
            inside = True
            continue
        if inside:
            if not line.startswith(" "):
                break
            if len(line) - len(line.lstrip()) != 2:
                continue
            key = line.strip().rstrip(":").split(":")[0]
            found.add(key)
    return found


def cron_fields(workflow: Path) -> list[list[str]]:
    """Every `cron:` expression in a workflow, split into its five fields."""
    expressions = re.findall(r"-\s*cron:\s*['\"]?([^'\"\n]+)['\"]?", workflow.read_text())
    return [expression.split() for expression in expressions]


def table_rows() -> list[list[str]]:
    """The three-cell rows of the *What runs automatically* table."""
    lines = MAINTAINER.read_text().splitlines()
    section = [
        line
        for line in _section_lines(lines, TABLE_SECTION)
        if line.strip().startswith("|")
    ]
    rows = []
    for line in section:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and not set(cells[0]) <= {"-", ":", " "}:
            rows.append(cells)
    return rows


def _section_lines(lines: list[str], title: str) -> list[str]:
    """The body of one `## <title>` section, up to the next heading."""
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {title}":
            start = index + 1
            break
    if start is None:
        raise AssertionError(f"{MAINTAINER_RELATIVE} has no '## {title}' section")
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def job_step_names(workflow: Path, job: str) -> list[str]:
    """The `- name:` step names of one job, in order."""
    lines = _uncommented(workflow.read_text())
    inside = False
    names = []
    for line in lines:
        if line.startswith(f"  {job}:"):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith("    ") and line.startswith("  "):
                break
            match = re.match(r"\s*- name: (?P<name>.+)$", line)
            if match:
                names.append(match.group("name").strip())
    return names


def row_for(workflow_name: str) -> list[str]:
    matches = [row for row in table_rows() if row[0].strip("`") == workflow_name]
    if len(matches) != 1:
        raise AssertionError(
            f"{MAINTAINER_RELATIVE}'s workflow table has {len(matches)} rows for "
            f"{workflow_name}, expected exactly one"
        )
    return matches[0]


class TableShapeTests(unittest.TestCase):
    """The table exists, and its rows are the workflow files."""

    def test_the_section_still_holds_a_table(self) -> None:
        # A rewrite that drops the table would otherwise make every join below
        # vacuous rather than failing.
        rows = table_rows()
        self.assertGreater(
            len(rows),
            1,
            f"{MAINTAINER_RELATIVE}'s '{TABLE_SECTION}' section no longer holds a "
            "workflow table",
        )
        self.assertEqual(
            rows[0][0],
            "Workflow",
            f"{MAINTAINER_RELATIVE}'s workflow table no longer starts with a "
            "Workflow column",
        )

    def test_the_rows_are_exactly_the_workflow_files(self) -> None:
        # Both directions. A workflow with no row is one a maintainer reading
        # the handbook does not know runs; a row with no workflow sends them
        # looking for a file that was deleted or renamed.
        documented = {row[0].strip("`") for row in table_rows()[1:]}
        on_disk = {path.name for path in WORKFLOW_DIR.glob("*.yml")}
        self.assertTrue(on_disk, ".github/workflows/ holds no .yml file")
        self.assertEqual(
            documented,
            on_disk,
            f"{MAINTAINER_RELATIVE}'s workflow table and .github/workflows/ disagree: "
            f"only in the table {sorted(documented - on_disk)}, "
            f"only on disk {sorted(on_disk - documented)}",
        )

    def test_no_row_is_blank(self) -> None:
        for row in table_rows()[1:]:
            for column, cell in zip(("Workflow", "Fires on", "Does"), row):
                self.assertTrue(
                    cell,
                    f"{MAINTAINER_RELATIVE}'s {row[0]} row has an empty {column} cell",
                )


class TriggerTests(unittest.TestCase):
    """*Fires on* against each workflow's `on:` block, both directions."""

    def test_every_declared_trigger_is_named_by_its_row(self) -> None:
        for row in table_rows()[1:]:
            name = row[0].strip("`")
            workflow = WORKFLOW_DIR / name
            declared = triggers(workflow)
            self.assertTrue(declared, f"{name} declares no trigger this test can read")
            for trigger in sorted(declared):
                self.assertIn(
                    trigger,
                    TRIGGER_WORDING,
                    f"{name} declares the trigger {trigger!r}, which this test has no "
                    "wording for -- add it to TRIGGER_WORDING with the phrase the "
                    "table should use",
                )
                self.assertRegex(
                    row[1],
                    re.compile(TRIGGER_WORDING[trigger], re.IGNORECASE),
                    f"{MAINTAINER_RELATIVE}'s {name} row does not say it fires on "
                    f"{trigger}, which {name} declares",
                )

    def test_no_row_names_a_trigger_its_workflow_dropped(self) -> None:
        for row in table_rows()[1:]:
            name = row[0].strip("`")
            declared = triggers(WORKFLOW_DIR / name)
            for trigger, wording in TRIGGER_WORDING.items():
                if trigger in declared:
                    continue
                self.assertNotRegex(
                    row[1],
                    re.compile(wording, re.IGNORECASE),
                    f"{MAINTAINER_RELATIVE}'s {name} row says it fires on {trigger}, "
                    f"which {name} no longer declares",
                )

    def test_a_scheduled_row_states_the_schedule_its_cron_sets(self) -> None:
        scheduled = 0
        for row in table_rows()[1:]:
            name = row[0].strip("`")
            fields = cron_fields(WORKFLOW_DIR / name)
            if not fields:
                continue
            self.assertEqual(
                len(fields),
                1,
                f"{name} has {len(fields)} cron expressions; the table states one "
                "schedule per row",
            )
            minute, hour, _, _, weekday = fields[0]
            self.assertIn(
                weekday,
                CRON_DAYS,
                f"{name}'s cron day-of-field is {weekday!r}, which this test cannot "
                "turn into the word the table uses",
            )
            scheduled += 1
            self.assertIn(
                CRON_DAYS[weekday],
                row[1],
                f"{MAINTAINER_RELATIVE}'s {name} row does not say "
                f"{CRON_DAYS[weekday]}, which its cron {' '.join(fields[0])} sets",
            )
            self.assertIn(
                f"{int(hour):02d}:{int(minute):02d} UTC",
                row[1],
                f"{MAINTAINER_RELATIVE}'s {name} row states a time other than the "
                f"{int(hour):02d}:{int(minute):02d} UTC its cron sets",
            )
        self.assertTrue(scheduled, "no workflow has a cron, so this test checked nothing")


class CiRowTests(unittest.TestCase):
    """The `ci.yml` row against the `test` job and the threshold file."""

    def setUp(self) -> None:
        self.row = row_for("ci.yml")
        self.workflow = WORKFLOW_DIR / "ci.yml"

    def test_the_row_names_every_check_the_gate_job_runs(self) -> None:
        # The row summarises, and a summary that drops three of the four
        # linters tells a maintainer a shellcheck, actionlint or hadolint
        # failure is not CI's decision. Every `Run <tool>` step of the `test`
        # job has to be named; the steps that install, package or upload are
        # plumbing and are not.
        tools = [
            name.removeprefix("Run ").lower()
            for name in job_step_names(self.workflow, "test")
            if name.startswith("Run ") and " " not in name.removeprefix("Run ")
        ]
        self.assertGreaterEqual(
            len(tools),
            2,
            "ci.yml's test job has fewer 'Run <tool>' steps than this test expects to "
            "find; the step naming has changed",
        )
        does = self.row[2].lower()
        for tool in tools:
            self.assertIn(
                tool,
                does,
                f"{MAINTAINER_RELATIVE}'s ci.yml row does not name {tool}, which "
                "ci.yml's test job runs and gates on",
            )

    def test_the_row_quotes_the_gate_from_the_threshold_file(self) -> None:
        threshold = json.loads(THRESHOLDS.read_text())["gated"]["unit"]
        self.assertIn(
            f"{threshold}%",
            self.row[2],
            f"{MAINTAINER_RELATIVE}'s ci.yml row quotes a gate other than the "
            f"{threshold} in .coverage-thresholds.json",
        )

    def test_the_path_scoped_image_claim_has_a_step_behind_it(self) -> None:
        # "when image files change" is a conditional, and a conditional that
        # was removed reads identically in the table.
        self.assertIn(
            "when image files change",
            self.row[2],
            f"{MAINTAINER_RELATIVE}'s ci.yml row no longer states that the image "
            "build is path-scoped",
        )
        steps = job_step_names(self.workflow, "container-build")
        self.assertIn(
            "Check for container-related changes",
            steps,
            "ci.yml's container-build job no longer decides whether the image changed, "
            "so the handbook's 'when image files change' is not what it does",
        )
        self.assertIn(
            "steps.changed.outputs.any_changed == 'true'",
            self.workflow.read_text(),
            "ci.yml builds the image unconditionally, but the handbook says it does so "
            "only when image files change",
        )


class TriageRowTests(unittest.TestCase):
    """The `triage.yml` row against the labels the workflow applies."""

    def setUp(self) -> None:
        self.row = row_for("triage.yml")
        self.text = (WORKFLOW_DIR / "triage.yml").read_text()

    def security_patterns(self) -> list[str]:
        """The `case` patterns that add the `security` label."""
        match = re.search(
            r"^\s*(?P<patterns>\*[^\n)]*)\)\n\s*labels\+=\(\"security\"\)",
            self.text,
            re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "triage.yml no longer applies `security` from a case this test can read",
        )
        patterns = []
        for raw in match.group("patterns").split("|"):
            patterns.append(raw.strip().strip("*").strip('"').strip("*"))
        return [pattern for pattern in patterns if pattern]

    def test_the_row_names_every_pattern_that_applies_security(self) -> None:
        # A pattern missing from the row is a way the label gets applied that
        # nobody reading the handbook knows about -- and `signing key` was
        # added precisely because "Signing key is missing" had slipped through.
        patterns = self.security_patterns()
        self.assertGreaterEqual(len(patterns), 2, "triage.yml's security case reads as one pattern")
        does = self.row[2].lower()
        for pattern in patterns:
            self.assertIn(
                pattern,
                does,
                f"{MAINTAINER_RELATIVE}'s triage.yml row does not name {pattern!r}, "
                "which triage.yml matches to apply `security`",
            )

    def test_the_row_names_the_label_prefix_the_workflow_applies(self) -> None:
        applied = sorted(set(re.findall(r'labels\+=\("(?P<label>[^"]+)"\)', self.text)))
        levels = [label for label in applied if label.startswith("acmm-l")]
        self.assertTrue(levels, "triage.yml no longer applies an acmm level label")
        self.assertIn(
            "acmm-lN",
            self.row[2],
            f"{MAINTAINER_RELATIVE}'s triage.yml row no longer describes the "
            f"{levels[0]}-style labels the workflow applies",
        )

    def test_the_additive_only_claim_holds(self) -> None:
        self.assertIn(
            "never removes a label",
            self.row[2],
            f"{MAINTAINER_RELATIVE}'s triage.yml row no longer makes the additive-only "
            "claim this test exists to hold it to",
        )
        self.assertIn("--add-label", self.text, "triage.yml no longer adds labels")
        self.assertNotIn(
            "--remove-label",
            self.text,
            "triage.yml removes a label, but the handbook says it never does",
        )


class AiFixRowTests(unittest.TestCase):
    """The `ai-fix.yml` row against the job it describes."""

    def setUp(self) -> None:
        self.row = row_for("ai-fix.yml")
        self.text = (WORKFLOW_DIR / "ai-fix.yml").read_text()

    def test_the_row_names_the_label_the_job_gates_on(self) -> None:
        label = re.search(r"github\.event\.label\.name == '(?P<label>[^']+)'", self.text)
        self.assertIsNotNone(label, "ai-fix.yml no longer gates on a label name")
        self.assertIn(
            f"`{label.group('label')}`",
            self.row[1],
            f"{MAINTAINER_RELATIVE}'s ai-fix.yml row names a trigger label other than "
            f"the {label.group('label')} the job gates on",
        )

    def test_the_read_only_claim_holds(self) -> None:
        # The row is what a maintainer checks before granting the label. If the
        # job grew a write, the row would still read "Read-only".
        self.assertIn(
            "Read-only",
            self.row[2],
            f"{MAINTAINER_RELATIVE}'s ai-fix.yml row no longer claims the job is "
            "read-only",
        )
        self.assertIn(
            "contents: read",
            self.text,
            "ai-fix.yml no longer takes read-only contents permission, but the handbook "
            "says it is read-only",
        )
        self.assertNotIn(
            "gh pr create",
            self.text,
            "ai-fix.yml opens a pull request, but the handbook says it does not",
        )


class NightlyRowTests(unittest.TestCase):
    """The `nightly-compliance.yml` row against the build it describes."""

    def test_the_from_scratch_claim_is_the_build_flag(self) -> None:
        row = row_for("nightly-compliance.yml")
        self.assertIn(
            "from scratch",
            row[2],
            f"{MAINTAINER_RELATIVE}'s nightly-compliance.yml row no longer says the "
            "rebuild is from scratch",
        )
        text = (WORKFLOW_DIR / "nightly-compliance.yml").read_text()
        for flag in ("--no-cache", "--pull=always"):
            self.assertIn(
                flag,
                text,
                f"nightly-compliance.yml builds without {flag}, so the handbook's "
                "'from scratch' overstates what the job proves",
            )


if __name__ == "__main__":
    unittest.main()

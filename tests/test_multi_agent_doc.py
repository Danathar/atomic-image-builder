"""Join docs/multi-agent.md to the files that make its claims true.

Script: tests/test_multi_agent_doc.py
What: Reads the page as a SUBJECT. The roster's branch prefixes are compared
      with docs/agent-tasks/README.md's list, the ruleset sentence with
      .github/rulesets/main.json, the intake steps with the two workflows they
      name, and every repository path the page names with `git ls-files`.
Why: The page describes machinery it does not own. A ruleset made strict, a
     role added to the trace page, or a workflow renamed would each leave this
     page telling an agent something false, and nothing else reads it.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs/multi-agent.md").read_text()
AGENT_TASKS = (ROOT / "docs/agent-tasks/README.md").read_text()
RULESET = json.loads((ROOT / ".github/rulesets/main.json").read_text())
AI_FIX = (ROOT / ".github/workflows/ai-fix.yml").read_text()
TRIAGE = (ROOT / ".github/workflows/triage.yml").read_text()

ROSTER = "Who works here"
INTAKE = "How work reaches an agent"
OVERLAP = "Staying out of each other's way"
MERGES = "Who merges"
SHARED = "What every agent shares"
NOT_RUN = "What this repository does not run"
IN_FLIGHT = "What is in flight right now"
NAMED_SECTIONS = (ROSTER, INTAKE, OVERLAP, MERGES, SHARED, NOT_RUN, IN_FLIGHT)


def sections(text: str) -> dict[str, str]:
    """Each `## ` heading's title mapped to the body beneath it."""
    found: dict[str, str] = {}
    for match in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        found[match.group(1).strip()] = match.group(2)
    return found


SECTIONS = sections(DOC)


def flatten(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def table_rows(body: str) -> list[list[str]]:
    """The data rows of the first Markdown table in a section, cells stripped."""
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]


def roster_prefixes() -> set[str]:
    prefixes = set()
    for row in table_rows(SECTIONS[ROSTER]):
        branch = row[2]
        if branch != "none":
            prefixes.add(branch.strip("`"))
    return prefixes


def tracked_files() -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in proc.stdout.splitlines() if line}


class Structure(unittest.TestCase):
    def test_every_section_the_assertions_scope_to_exists(self) -> None:
        # Named rather than discovered: a renamed section must fail here
        # instead of making the assertions under it read an empty string.
        for title in NAMED_SECTIONS:
            with self.subTest(section=title):
                self.assertIn(title, SECTIONS)
                self.assertTrue(SECTIONS[title].strip())


class Roster(unittest.TestCase):
    def test_the_branch_prefixes_are_the_ones_the_trace_page_lists(self) -> None:
        # docs/agent-tasks/README.md tells a reader which prefixes mark agent
        # work; this table says which role owns each. A role added to one and
        # not the other leaves a branch nobody can attribute.
        listed = re.search(r"the role is the one in its signature: (.*?)\. The maintainer", flatten(AGENT_TASKS))
        self.assertIsNotNone(listed, "docs/agent-tasks/README.md no longer lists the role prefixes")
        trace_prefixes = set(re.findall(r"`([a-z-]+/)`", listed.group(1)))
        self.assertEqual(roster_prefixes(), trace_prefixes)

    def test_every_role_with_a_signature_names_it_as_the_signature_writes_it(self) -> None:
        for row in table_rows(SECTIONS[ROSTER]):
            with self.subTest(role=row[0]):
                self.assertRegex(row[1], r"^`agent=[a-z-]+`$|^no `agent=` field$|^none")

    def test_the_trace_page_points_back_here(self) -> None:
        # The page is reachable only through that link; without it nothing a
        # reader is already sent to leads here.
        self.assertIn("(../multi-agent.md)", AGENT_TASKS)


class Ruleset(unittest.TestCase):
    def rule(self, kind: str) -> dict:
        matches = [rule for rule in RULESET["rules"] if rule["type"] == kind]
        self.assertEqual(len(matches), 1, f"main.json has no single {kind} rule")
        return matches[0].get("parameters", {})

    def test_test_is_required_but_not_against_the_latest_main(self) -> None:
        # The page's warning that two green pull requests can be red together
        # is true only while the check is not strict. Turning strict on makes
        # the warning wrong, and this is what says so.
        checks = self.rule("required_status_checks")
        self.assertEqual([check["context"] for check in checks["required_status_checks"]], ["test"])
        self.assertIs(checks["strict_required_status_checks_policy"], False)
        self.assertIn("`strict_required_status_checks_policy` is `false`", flatten(SECTIONS[OVERLAP]))

    def test_no_approval_is_required_and_nobody_bypasses(self) -> None:
        self.assertEqual(self.rule("pull_request")["required_approving_review_count"], 0)
        self.assertEqual(RULESET["bypass_actors"], [])
        self.assertIn("The ruleset needs no approval and lets nobody bypass `test`", flatten(SECTIONS[MERGES]))


class Intake(unittest.TestCase):
    def test_triage_runs_when_an_issue_is_opened_and_only_adds(self) -> None:
        self.assertRegex(TRIAGE, r"issues:\n\s+types: \[opened\]")
        self.assertIn("--add-label", TRIAGE)
        self.assertNotIn("--remove-label", TRIAGE)
        body = flatten(SECTIONS[INTAKE])
        self.assertIn("on `opened`", body)
        self.assertIn("It only adds labels", body)

    def test_the_intake_comment_fires_on_the_label_the_page_names(self) -> None:
        self.assertRegex(AI_FIX, r"issues:\n\s+types: \[labeled\]")
        self.assertIn("github.event.label.name == 'ai-fix-requested'", AI_FIX)
        self.assertIn("`ai-fix-requested` label", flatten(SECTIONS[INTAKE]))

    def test_the_intake_workflow_still_writes_nothing_but_an_issue_comment(self) -> None:
        # "No workflow here ... opens a pull request" rests on this file's
        # permissions above all, since it is the one triggered by agent work.
        permissions = re.search(r"^permissions:\n((?:  .+\n)+)", AI_FIX, re.M)
        self.assertIsNotNone(permissions)
        self.assertEqual(permissions.group(1).split(), ["contents:", "read", "issues:", "write"])


class Paths(unittest.TestCase):
    def test_every_repository_path_the_page_names_exists(self) -> None:
        # Link targets are already resolved by the repo-wide link test; a code
        # span is not, and a span is how the page names a path it does not
        # link. Labels (`agent/quality`, `hive/covered-by-pr`) and branch
        # prefixes (`fix/`) are spans too, so only a span that starts in one
        # of the directories the page talks about, or is a root-level file,
        # is read as a path.
        tracked = tracked_files()
        checked = 0
        for span in sorted(set(re.findall(r"`([^`\s]+)`", DOC))):
            in_named_dir = span.startswith((".github/", ".claude/", "docs/"))
            root_file = "/" not in span and re.search(r"\.(md|yml|json)$", span)
            if not (in_named_dir or root_file):
                continue
            checked += 1
            with self.subTest(path=span):
                prefix = span.rstrip("/") + "/"
                self.assertTrue(
                    span in tracked or any(name.startswith(prefix) for name in tracked),
                    f"docs/multi-agent.md names {span}, which is not in the tree",
                )
        # The page names well over ten; a regex that stopped matching would
        # otherwise pass with nothing checked.
        self.assertGreater(checked, 10)


if __name__ == "__main__":
    unittest.main()

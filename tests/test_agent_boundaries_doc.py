"""Join docs/agent-boundaries.md to the settings, rulesets and workflows it maps.

Script: tests/test_agent_boundaries_doc.py
What: Reads the page's gate table and its prose against the files they
      describe: the `main` ruleset, the required check's job in ci.yml, the
      Claude Code settings and hook, the workflow-permissions policy, the
      agent audit's schedule and selection, AGENTS.md's consent gates, and the
      absence of a CODEOWNERS file. It also checks the page's links land.
Doing: Reuses tests/test_ai_ops_runbook.py's section and anchor parsers, so
       headings are slugged the same way in both documents.
Why: The page says which limits refuse an action whatever the agent decides.
     That is the claim a reviewer leans on when deciding how hard to look, so
     a gate that quietly stopped existing -- a bypass actor added to the
     ruleset, a deny rule dropped, a second hook nobody listed -- turns the
     page into false comfort. No coverage tier measures Markdown, so nothing
     else fails when the page and the files part.
Goal: Adding a ruleset bypass or rule, a hook, or a consent gate, dropping a
      deny or ask rule the page names, renaming the required check, adding a
      CODEOWNERS file, or moving the audit off its monthly schedule fails here,
      in the change that caused it.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_ai_ops_runbook import anchors, section  # noqa: E402

DOC = ROOT / "docs/agent-boundaries.md"
RULESET = ROOT / ".github/rulesets/main.json"
CI = ROOT / ".github/workflows/ci.yml"
SETTINGS = ROOT / ".claude/settings.json"
AGENT_AUDIT = ROOT / ".github/workflows/agent-audit.yml"
AGENTS = ROOT / "AGENTS.md"

# The places GitHub looks for a CODEOWNERS file.
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")

# What the ruleset row says it stops, keyed by the ruleset rule that stops it.
# A rule type missing from this map is one the row does not describe.
RULESET_RULES = {
    "deletion": "deleting",
    "non_fast_forward": "rewriting `main`",
    "pull_request": "except as a pull request",
    "required_status_checks": None,  # the row below this one, by check name
}

# What the settings row says it stops or asks about, and the rules behind it.
# Every listed rule has to be in settings.json, in the list named.
SETTINGS_CLAIMS = {
    "a signing key": ("deny", ["Read(./cosign.key)"]),
    "`.env`": ("deny", ["Read(./.env)", "Read(./.env.*)"]),
    "a PEM file": ("deny", ["Read(**/*.pem)"]),
    "an SSH private key": ("deny", ["Read(**/id_rsa)", "Read(**/id_ed25519)"]),
    "force-push": ("deny", ["Bash(git push --force:*)", "Bash(git push -f:*)"]),
    "hard reset": ("deny", ["Bash(git reset --hard:*)"]),
    "broad Podman or Buildah cleanup": (
        "deny",
        [
            "Bash(podman system prune:*)",
            "Bash(podman image prune:*)",
            "Bash(podman rmi -a:*)",
            "Bash(podman rm -a:*)",
            "Bash(buildah rm --all:*)",
            "Bash(buildah rmi --all:*)",
        ],
    ),
    "repository deletion": ("deny", ["Bash(gh repo delete:*)"]),
    "host rebase": ("deny", ["Bash(rpm-ostree reset:*)", "Bash(bootc switch:*)"]),
    "asks before anything outward-facing": (
        "ask",
        [
            "Bash(git push:*)",
            "Bash(gh pr create:*)",
            "Bash(gh pr merge:*)",
            "Bash(gh release create:*)",
            "Bash(gh workflow run:*)",
        ],
    ),
}

NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def gate_rows(text: str) -> list[dict[str, str]]:
    """Each body row of the gate table, keyed by its header cells."""
    lines = [line for line in section(text, "The gates").splitlines() if line.startswith("|")]
    split = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    header, body = split[0], split[2:]
    return [dict(zip(header, row)) for row in body]


def link_target(cell: str) -> str:
    match = re.search(r"\]\(([^)\s#]+)", cell)
    if match is None:
        raise AssertionError(f"gate cell links no file: {cell}")
    return match.group(1)


def row_for(rows: list[dict[str, str]], path: str) -> dict[str, str]:
    for row in rows:
        if (DOC.parent / link_target(row["Gate"])).resolve() == (ROOT / path).resolve():
            return row
    raise AssertionError(f"the gate table has no row for {path}")


def hook_scripts(settings: dict) -> set[str]:
    """Repo paths of every script a Claude Code hook in settings.json runs."""
    scripts = set()
    for entries in settings.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                for path in re.findall(r"\$CLAUDE_PROJECT_DIR/([^\s\"']+)", hook["command"]):
                    scripts.add(path)
    return scripts


class AgentBoundariesDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOC.read_text()
        self.rows = gate_rows(self.text)
        self.ruleset = json.loads(RULESET.read_text())
        self.settings = json.loads(SETTINGS.read_text())

    def test_links_resolve(self) -> None:
        targets = re.findall(r"\]\(([^)\s]+)\)", self.text)
        self.assertTrue(targets, "the page links nothing, so this checked nothing")
        for target in targets:
            if target.startswith("http"):
                continue
            with self.subTest(target=target):
                path_part, _, fragment = target.partition("#")
                path = (DOC.parent / path_part).resolve() if path_part else DOC
                self.assertTrue(path.exists(), f"{target} points at nothing")
                if fragment:
                    self.assertIn(fragment, anchors(path), f"{target} names no heading there")

    def test_holds_for_matches_where_the_gate_lives(self) -> None:
        # Files under .claude/ are Claude Code's format; every other gate is
        # on GitHub's side and binds whatever backend the agent runs on.
        for row in self.rows:
            target = (DOC.parent / link_target(row["Gate"])).resolve()
            claude_only = target.is_relative_to((ROOT / ".claude").resolve())
            with self.subTest(gate=row["Gate"]):
                self.assertEqual(
                    row["Holds for"],
                    "Claude Code only" if claude_only else "every agent",
                )

    def test_every_hook_is_a_gate(self) -> None:
        scripts = hook_scripts(self.settings)
        self.assertTrue(scripts, "settings.json runs no hook, so this checked nothing")
        for script in sorted(scripts):
            with self.subTest(script=script):
                row = row_for(self.rows, script)
                self.assertEqual(row["Holds for"], "Claude Code only")

    def test_the_ruleset_has_no_bypass(self) -> None:
        row = row_for(self.rows, ".github/rulesets/main.json")
        self.assertIn("It has no bypass", row["What it stops"])
        self.assertEqual(self.ruleset["bypass_actors"], [], "the page says nobody bypasses main")
        self.assertEqual(self.ruleset["enforcement"], "active")

    def test_the_ruleset_row_describes_every_rule(self) -> None:
        row = row_for(self.rows, ".github/rulesets/main.json")
        types = {rule["type"] for rule in self.ruleset["rules"]}
        self.assertEqual(
            sorted(types - RULESET_RULES.keys()),
            [],
            "the ruleset gained a rule the gate table does not describe",
        )
        for rule, phrase in RULESET_RULES.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, types, "the page describes a rule the ruleset dropped")
                if phrase:
                    self.assertIn(phrase, row["What it stops"])

    def test_the_required_check_is_cis_test_job(self) -> None:
        (rule,) = [r for r in self.ruleset["rules"] if r["type"] == "required_status_checks"]
        contexts = [check["context"] for check in rule["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, ["test"])
        row = row_for(self.rows, ".github/workflows/ci.yml")
        self.assertIn("The required `test` check", row["Gate"])
        self.assertRegex(CI.read_text(), r"(?m)^  test:\n", "ci.yml has no `test` job")

    def test_no_approval_and_no_code_owner_review(self) -> None:
        (rule,) = [r for r in self.ruleset["rules"] if r["type"] == "pull_request"]
        params = rule["parameters"]
        text = flat(self.text)
        self.assertIn("The ruleset needs no approval", text)
        self.assertEqual(params["required_approving_review_count"], 0)
        self.assertIn("sets `require_code_owner_review` to `false`", text)
        self.assertIs(params["require_code_owner_review"], False)

    def test_there_is_no_codeowners_file(self) -> None:
        self.assertIn("## Why there is no CODEOWNERS file", self.text)
        for path in CODEOWNERS_PATHS:
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists(), "drop the page's CODEOWNERS section")

    def test_settings_rules_the_page_names(self) -> None:
        row = row_for(self.rows, ".claude/settings.json")
        permissions = self.settings["permissions"]
        for claim, (kind, rules) in SETTINGS_CLAIMS.items():
            with self.subTest(claim=claim):
                self.assertIn(claim, row["What it stops"])
                missing = [rule for rule in rules if rule not in permissions[kind]]
                self.assertEqual(missing, [], f"settings.json no longer has these {kind} rules")

    def test_the_audit_is_monthly_and_reads_the_signature(self) -> None:
        row = row_for(self.rows, ".github/workflows/agent-audit.yml")
        self.assertIn("It runs monthly", row["What it stops"])
        self.assertIn("`— hive:` line", row["What it stops"])
        workflow = AGENT_AUDIT.read_text()
        crons = re.findall(r"cron:\s*'([^']+)'", workflow)
        self.assertEqual(len(crons), 1, "agent-audit.yml has one schedule")
        minute, hour, day, month, weekday = crons[0].split()
        self.assertTrue(day.isdigit() and month == "*" and weekday == "*", crons[0])
        self.assertIn('test("(^|\\n)— hive:")', workflow)

    def test_the_consent_gate_count_is_agents_mds(self) -> None:
        match = re.search(r"lists (\w+), each its own gate", flat(self.text))
        self.assertIsNotNone(match, "the page no longer counts AGENTS.md's consent gates")
        body = AGENTS.read_text().split("Treat these as separate consent gates:", 1)[1]
        gates = re.findall(r"^\d+\. ", body.split("\n\n", 2)[1], re.M)
        self.assertEqual(NUMBER_WORDS[match.group(1)], len(gates))


if __name__ == "__main__":
    unittest.main()
